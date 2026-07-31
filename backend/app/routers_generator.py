"""
routers_generator.py
Add to backend/app/ and register in main.py:
    from . import routers_generator
    app.include_router(routers_generator.router)
"""
import asyncio
import json
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .deps import get_current_user, is_admin, is_admin_username, require_admin
from .database import get_db
from . import models
from .generator_core import (
    GeneratorWorker, RunStats, Vault, TYPE_META, ACCOUNT_TYPES,
    load_config, save_config, load_usage, save_usage,
    DEFAULT_CONFIG,
    _get_keys, _secure_load, make_ssl_context, _http,
    bloxgen_daily_limit, bloxgen_stock,
    filter_accounts, load_accounts,
    check_warp_status,
)

router = APIRouter(prefix="/generator", tags=["generator"])


@router.get("/admin/users")
def list_users(_=Depends(require_admin), db: Session = Depends(get_db)):
    """List every registered user — admin only. Shows username and whether
    they're currently on the ADMIN_USERNAMES allowlist."""
    rows = db.query(models.User).all()
    out = []
    for u in rows:
        uname = str(getattr(u, "username", ""))
        entry = {
            "username": uname,
            "is_admin": is_admin_username(uname),
        }
        # Include created_at if the model happens to have it — optional field,
        # don't fail the whole request if it doesn't exist.
        created = getattr(u, "created_at", None)
        if created is not None:
            entry["created_at"] = str(created)
        out.append(entry)
    out.sort(key=lambda e: e["username"].lower())
    return out


@router.get("/admin/warp-status")
def warp_status(_=Depends(require_admin)):
    """Live check of whether WARP is usable RIGHT NOW — not whether it
    connected at container boot. A boot that failed can recover on its own
    later, and a boot that succeeded can degrade — this always reflects the
    current moment, which is what actually matters before relying on it."""
    connected, detail = check_warp_status()
    return {"connected": connected, "detail": detail}


def _format_vault_date(raw) -> str:
    """Convert whatever date format the vault returns into something readable.
    Falls back to the raw value if it doesn't parse as a known format."""
    if not raw:
        return "—"
    s = str(raw)
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            d = _dt.strptime(s, fmt)
            return d.strftime("%b %d, %Y %H:%M")
        except ValueError:
            continue
    # ISO with variable microsecond precision — try fromisoformat as last resort
    try:
        cleaned = s.replace("Z", "+00:00")
        d = _dt.fromisoformat(cleaned)
        return d.strftime("%b %d, %Y %H:%M")
    except Exception:
        return s

# ── Session state (single shared session, same as desktop) ────────────────────

class _Session:
    def __init__(self):
        self.worker: Optional[GeneratorWorker] = None
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self.log_q: deque = deque(maxlen=500)      # {msg, level} dicts
        self.stats: Optional[RunStats] = None
        # load_config() seeds _MEM_CONFIG from DEFAULT_CONFIG on first call,
        # so vault_api/vault_user/vault_pass are always present here.
        self.cfg: Dict[str, Any] = load_config()
        self.usage: Dict[str, Any] = load_usage()
        self.key_states: Dict[int, Dict] = {}       # api_num -> {status, detail}
        self.stock_data: Dict[str, Any] = {}
        self.limits_data: Optional[Dict] = None
        self._session_id: str = ""
        self.started_by: str = ""  # username of whoever last called /start
        self.current_account_type: str = ""  # the type actually running right now, if any

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def queue_log(self, msg: str, level: str = "info", replace=None):
        self.log_q.append({"msg": str(msg), "level": level})

    def queue_stats(self, stats: RunStats, payload: str):
        self.stats = stats
        if payload.startswith("stock:"):
            try:
                n = int(payload.split(":")[1])
                self.cfg["_vault_stock"] = n
            except Exception:
                pass

    def queue_key(self, api_num: int, status: str, detail: str):
        self.key_states[api_num] = {"status": status, "detail": detail}

    def queue_account(self, user, final_pw, old_pw, age, atype, pw_changed, vault_pushed, api_tail):
        from .generator_core import append_account, _flog
        try:
            entry = append_account(
                user, final_pw, old_pw, age, atype, pw_changed, vault_pushed, api_tail,
                session_id=self._session_id,
                generated_by=self.started_by,
            )
            _flog(f"queue_account: append_account OK for {user}, generated_by={self.started_by!r}, entry_keys={list(entry.keys()) if entry else 'EMPTY'}")
        except Exception as e:
            _flog(f"queue_account: append_account FAILED for {user}: {e!r}")

    def queue_usage(self, key: str, count: int):
        pass  # usage handled by bump_key_usage() in generator_core


_session = _Session()

# ── Per-user preferences ────────────────────────────────────────────────────
# _session.cfg holds SHARED infrastructure only: vault credentials, Bloxgen
# keys, vault_enabled. These are admin-controlled and the same for everyone.
# account_type/new_password/target_count/consecutive_empty_stop/ssl_verify
# are personal run preferences — each logged-in user gets their own copy so
# one person's settings never silently overwrite another's.
PREF_FIELDS = ("account_type", "new_password", "target_count", "consecutive_empty_stop", "ssl_verify")
_user_prefs: Dict[str, Dict[str, Any]] = {}
_user_prefs_lock = threading.Lock()


def _get_user_prefs(username: str) -> Dict[str, Any]:
    key = str(username or "").strip().lower()
    with _user_prefs_lock:
        return dict(_user_prefs.get(key, {}))


def _set_user_prefs(username: str, prefs: Dict[str, Any]) -> None:
    key = str(username or "").strip().lower()
    with _user_prefs_lock:
        existing = _user_prefs.get(key, {})
        existing.update(prefs)
        _user_prefs[key] = existing


def _build_user_cfg(username: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Combine shared infra config (vault creds, Bloxgen keys — admin owned)
    with this specific user's own saved preferences, then apply any fresh
    values from the current request on top. Nothing personal ever leaks
    between different users' Config tabs."""
    cfg = dict(_session.cfg)
    cfg.update(_get_user_prefs(username))
    if overrides:
        for field in PREF_FIELDS:
            if field in overrides:
                cfg[field] = overrides[field]
    return cfg

# ── Schemas ───────────────────────────────────────────────────────────────────

class GeneratorConfig(BaseModel):
    account_type: str = "+30 days old"
    new_password: str = ""
    target_count: int = 0
    vault_enabled: bool = True
    ssl_verify: bool = True
    bloxgen_keys: list = []
    consecutive_empty_stop: int = 5
    discord_webhook: str = ""

class StatusResponse(BaseModel):
    running: bool
    session_id: str
    done: int
    stock_empty: int
    fails: int
    vault_stock: int
    key_states: Dict[int, Dict]
    account_type: str

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=StatusResponse)
def get_status(user=Depends(get_current_user)):
    s = _session.stats
    return StatusResponse(
        running=_session.is_running(),
        session_id=_session._session_id,
        done=s.done if s else 0,
        stock_empty=s.stock_empty if s else 0,
        fails=s.fails if s else 0,
        vault_stock=_session.cfg.get("_vault_stock", -1) if is_admin(user) else -1,
        key_states=_session.key_states,
        account_type=_session.current_account_type or "+30 days old",
    )


@router.post("/start")
def start_session(config: GeneratorConfig, user=Depends(get_current_user)):
    if _session.is_running():
        raise HTTPException(400, "Session already running")

    username = str(getattr(user, "username", "")).strip().lower()
    overrides = config.model_dump()

    # Save this user's own personal preferences for next time — never touches
    # anyone else's saved settings, and never touches shared infra config.
    _set_user_prefs(username, {k: overrides[k] for k in PREF_FIELDS if k in overrides})

    if is_admin(user):
        # Only an admin's bloxgen_keys/vault_enabled submission updates the
        # SHARED infra config — those aren't personal preferences.
        infra = {k: overrides[k] for k in ("bloxgen_keys", "vault_enabled") if k in overrides}
        if infra:
            _session.cfg.update(infra)
            save_config(_session.cfg)

    cfg = _build_user_cfg(username, overrides)
    if not is_admin(user):
        # Server-enforced regardless of what was submitted — not just a
        # hidden UI checkbox.
        cfg["vault_enabled"] = False
        cfg["bloxgen_keys"] = list(_session.cfg.get("bloxgen_keys") or [])
    cfg["_dry_run"] = False

    _session._stop.clear()
    _session._pause.clear()
    _session.key_states = {}
    _session.log_q.clear()
    _session.current_account_type = cfg.get("account_type", "+30 days old")

    import uuid as _uuid
    _session._session_id = _uuid.uuid4().hex[:12]
    _session.started_by = username
    _session.usage = load_usage()

    worker = GeneratorWorker(
        cfg=cfg,
        usage=_session.usage,
        log=_session.queue_log,
        stats_cb=_session.queue_stats,
        key_cb=_session.queue_key,
        account_cb=_session.queue_account,
        usage_cb=_session.queue_usage,
        should_stop=_session._stop.is_set,
        is_paused=_session._pause.is_set,
        session_id=_session._session_id,
    )
    _session.worker = worker

    def run():
        try:
            _session.stats = worker.run()
            _session.queue_log("Session complete", "ok")
        except Exception as e:
            _session.queue_log(f"Worker error: {e}", "error")
        finally:
            _session.queue_log("__done__", "__done__")

    _session.thread = threading.Thread(target=run, daemon=True)
    _session.thread.start()
    return {"started": True, "session_id": _session._session_id}


@router.post("/stop")
def stop_session(_=Depends(get_current_user)):
    if not _session.is_running():
        raise HTTPException(400, "No session running")
    _session._stop.set()
    return {"stopped": True}


@router.post("/pause")
def pause_session(_=Depends(get_current_user)):
    if _session._pause.is_set():
        _session._pause.clear()
        return {"paused": False}
    _session._pause.set()
    return {"paused": True}


@router.post("/dry-run")
def dry_run(user=Depends(get_current_user)):
    if _session.is_running():
        raise HTTPException(400, "Session already running")

    username = str(getattr(user, "username", "")).strip().lower()
    cfg = _build_user_cfg(username)
    if not is_admin(user):
        cfg["vault_enabled"] = False
    cfg["_dry_run"] = True
    _session._stop.clear()
    _session.log_q.clear()
    _session.key_states = {}
    _session.current_account_type = cfg.get("account_type", "+30 days old")
    import uuid as _uuid
    _session._session_id = _uuid.uuid4().hex[:12]
    _session.usage = load_usage()

    worker = GeneratorWorker(
        cfg=cfg,
        usage=_session.usage,
        log=_session.queue_log,
        stats_cb=_session.queue_stats,
        key_cb=_session.queue_key,
        account_cb=_session.queue_account,
        usage_cb=_session.queue_usage,
        should_stop=_session._stop.is_set,
        is_paused=_session._pause.is_set,
        session_id=_session._session_id,
    )
    _session.worker = worker

    def run():
        try:
            worker.run()
        except Exception as e:
            _session.queue_log(f"Dry run error: {e}", "error")
        finally:
            _session.queue_log("__done__", "__done__")

    _session.thread = threading.Thread(target=run, daemon=True)
    _session.thread.start()
    return {"started": True, "dry_run": True}


@router.post("/config")
def update_config(config: GeneratorConfig, user=Depends(get_current_user)):
    if _session.is_running():
        raise HTTPException(400, "Cannot update config while running")

    username = str(getattr(user, "username", "")).strip().lower()
    overrides = config.model_dump()

    # Personal fields — saved per-user, never touches anyone else's settings.
    _set_user_prefs(username, {k: overrides[k] for k in PREF_FIELDS if k in overrides})

    # Shared infra fields — admin only, saved to the single shared config.
    if is_admin(user):
        infra = {k: overrides[k] for k in ("bloxgen_keys", "vault_enabled") if k in overrides}
        if infra:
            _session.cfg.update(infra)
            save_config(_session.cfg)

    return {"saved": True}


@router.get("/config")
def get_config(user=Depends(get_current_user)):
    username = str(getattr(user, "username", "")).strip().lower()
    cfg = _build_user_cfg(username)
    # Never send vault credentials to frontend
    for k in ("vault_pass", "vault_api", "vault_user", "_dry_run"):
        cfg.pop(k, None)
    admin = is_admin(user)
    cfg["is_admin"] = admin
    if not admin:
        cfg["bloxgen_keys"] = []       # keys stay invisible to non-admins
        cfg["vault_enabled"] = False   # reflect the server-enforced state
    return cfg


@router.get("/accounts")
def get_accounts(search: str = "", account_type: str = "", limit: int = 1000, user=Depends(get_current_user)):
    """Admins: fetch the full vault (source of truth). Non-admins: fetch only
    accounts they personally generated, from the local store — filtered by
    the generated_by field stored directly on each account record. The
    shared vault is never touched or exposed for a non-admin request."""
    s = (search or "").strip().lower()
    t = (account_type or "").strip().lower()
    limit = max(1, min(limit, 5000))

    if not is_admin(user):
        my_username = str(getattr(user, "username", "")).strip().lower()
        accounts = filter_accounts(search=search, account_type=account_type or "")
        mine = [
            a for a in accounts
            if str(a.get("generated_by") or "").strip().lower() == my_username
        ]
        return [
            {**a, "pass": a.get("pass", ""), "date": _format_vault_date(a.get("date")),
             "vault_pushed": False}  # non-admins can never push to vault — always accurate
            for a in mine[:limit]
        ]

    admin_username = str(getattr(user, "username", "")).strip().lower()
    admin_prefs = _get_user_prefs(admin_username)

    try:
        ssl_ctx = make_ssl_context(bool(admin_prefs.get("ssl_verify", True)))
        v = Vault(
            str(_session.cfg["vault_api"]),
            str(_session.cfg["vault_user"]),
            _secure_load("vault_pass", _session.cfg),
            ssl_ctx,
        )
        v.login()
        code, body, _hdrs = _http(
            "GET", f"{v.base}/accounts",
            headers=v._hdr(), ssl_ctx=ssl_ctx,
        )
        if code < 200 or code >= 300:
            raise RuntimeError(f"vault /accounts HTTP {code}")
        raw = []
        if isinstance(body, list):
            raw = body
        elif isinstance(body, dict):
            raw = body.get("accounts", [])

        # The vault doesn't reliably track a pw_changed flag, so determine it
        # deterministically: does this account's current password already
        # match the configured target? If new_password isn't set, fall back
        # to the vault's own flag (if it happens to exist) as a soft signal.
        configured_new_pw = str(admin_prefs.get("new_password") or "").strip()

        accounts = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            acc_user = str(a.get("username") or a.get("user") or "")
            pw = str(a.get("password") or a.get("pass") or "")
            a_type = str(a.get("type") or a.get("account_type") or "")
            if s and s not in acc_user.lower() and s not in pw.lower():
                continue
            if t and a_type.strip().lower() != t:
                continue
            if configured_new_pw:
                pw_changed_flag = (pw == configured_new_pw)
            else:
                pw_changed_flag = bool(a.get("pw_changed") or a.get("password_changed"))
            accounts.append({
                "user": acc_user,
                "pass": pw,  # full password — private single-user tool, not exposed publicly
                "date": _format_vault_date(a.get("date") or a.get("created_at") or a.get("createdAt")),
                "age": a.get("age") or "—",
                "type": a_type or "—",
                "pw_changed": pw_changed_flag,
                "vault_pushed": True,
            })
        return accounts[:limit]
    except Exception as e:
        # Fallback to local file (only useful if a disk-backed run wrote it)
        accounts = filter_accounts(search=search, account_type=account_type or "")
        return [
            {**a, "pass": a.get("pass", ""), "date": _format_vault_date(a.get("date"))}
            for a in accounts[:limit]
        ]


@router.post("/accounts/change-passwords")
def change_unchanged_passwords(user=Depends(require_admin)):
    """Bulk password change for vault accounts that haven't been changed yet.

    Each account needs a fresh Roblox login (no cookie is stored in the vault
    for old accounts). Roblox challenges (Arkose/FunCaptcha) programmatic
    logins from datacenter IPs like Railway's, so a meaningful fraction of
    accounts will be skipped — that's expected, not a bug. Progress streams
    through the same /generator/stream feed as Start/Stats.
    """
    if _session.is_running():
        raise HTTPException(400, "Another job is already running")

    username = str(getattr(user, "username", "")).strip().lower()
    new_password = str(_get_user_prefs(username).get("new_password") or "").strip()
    if len(new_password) < 8:
        raise HTTPException(400, "Set a new password (8+ chars) in Config first")

    _session._stop.clear()
    _session.log_q.clear()
    import uuid as _uuid
    _session._session_id = _uuid.uuid4().hex[:12]

    def run():
        from .generator_core import roblox_login, provider_change_password
        try:
            ssl_ctx = make_ssl_context(bool(_get_user_prefs(username).get("ssl_verify", True)))
            v = Vault(
                str(_session.cfg["vault_api"]),
                str(_session.cfg["vault_user"]),
                _secure_load("vault_pass", _session.cfg),
                ssl_ctx,
            )
            v.login()
            code, body, _hdrs = _http("GET", f"{v.base}/accounts", headers=v._hdr(), ssl_ctx=ssl_ctx)
            if code < 200 or code >= 300:
                _session.queue_log(f"Could not load vault accounts (HTTP {code})", "error")
                return
            raw = body if isinstance(body, list) else (body.get("accounts", []) if isinstance(body, dict) else [])

            targets = []
            for a in raw:
                if not isinstance(a, dict):
                    continue
                user = str(a.get("username") or a.get("user") or "")
                pw = str(a.get("password") or a.get("pass") or "")
                if not user or not pw:
                    continue
                # The vault doesn't reliably track a pw_changed flag — trust
                # the actual stored password instead. If it already matches
                # the target, there's genuinely nothing to do for this account.
                if pw == new_password:
                    continue
                targets.append((user, pw))

            _session.queue_log(f"=== BULK PASSWORD CHANGE ===", "ok")
            _session.queue_log(f"{len(targets)} account(s) not yet on the target password", "info")
            if not targets:
                _session.queue_log("Nothing to do — every vault account already has the target password", "info")

            changed = 0
            skip_reasons: Dict[str, int] = {}

            def _bump(category: str):
                skip_reasons[category] = skip_reasons.get(category, 0) + 1

            for user, old_pw in targets:
                if _session._stop.is_set():
                    _session.queue_log("Stopped by user", "warn")
                    break
                if old_pw == new_password:
                    _session.queue_log(f"Skip {user}: already using target password", "muted")
                    _bump("already-target-password")
                    continue
                try:
                    login_ok, login_result = roblox_login(user, old_pw, ssl_ctx, use_proxy=True)
                except Exception as e:
                    login_ok, login_result = False, str(e)
                if not login_ok:
                    result_str = str(login_result).lower()
                    if "no 2captcha key configured" in result_str:
                        _session.queue_log(f"Skip {user}: Roblox wants a captcha here (solving disabled)", "muted")
                        _bump("2captcha-not-configured")
                    elif "captcha solve failed" in result_str:
                        _session.queue_log(f"Skip {user}: 2Captcha could not solve it ({login_result})", "warn")
                        _bump("2captcha-solve-failed")
                    elif "challenge" in result_str:
                        _session.queue_log(f"Skip {user}: Roblox still rejected it after captcha solving ({login_result})", "warn")
                        _bump("roblox-challenge-after-solve")
                    else:
                        _session.queue_log(f"Skip {user}: login failed ({login_result})", "warn")
                        _bump("login-other")
                    time.sleep(0.5)
                    continue
                ok, reason = provider_change_password(login_result, old_pw, new_password, ssl_ctx, use_proxy=True)
                if not ok and str(reason).startswith("net:"):
                    # Transient network hiccup (WARP's connection quality can
                    # vary moment to moment) rather than a real rejection —
                    # one retry before counting this as a genuine failure.
                    ok, reason = provider_change_password(login_result, old_pw, new_password, ssl_ctx, use_proxy=True)
                if not ok:
                    _session.queue_log(f"Skip {user}: PW change failed ({reason})", "warn")
                    _bump("pw-change-failed")
                    time.sleep(0.5)
                    continue
                pushed, detail = v.update_password(user, new_password)
                if pushed:
                    _session.queue_log(f"Changed {user} (vault updated)", "ok")
                    changed += 1
                else:
                    _session.queue_log(f"Changed {user} but vault update failed ({detail})", "warn")
                    changed += 1
                    _bump("vault-update-failed-but-pw-changed")
                time.sleep(0.5)

            total_skipped = sum(v for k, v in skip_reasons.items() if k != "vault-update-failed-but-pw-changed")
            _session.queue_log(f"=== DONE — {changed} changed, {total_skipped} skipped ===", "ok")
            if skip_reasons:
                for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
                    label = {
                        "2captcha-not-configured": "Roblox required a captcha (solving disabled)",
                        "2captcha-solve-failed": "2Captcha couldn't solve the challenge",
                        "roblox-challenge-after-solve": "Roblox rejected it even after a solved captcha",
                        "login-other": "login failed (other reason)",
                        "pw-change-failed": "password change rejected",
                        "already-target-password": "already using target password",
                        "vault-update-failed-but-pw-changed": "password changed but vault write failed",
                    }.get(reason, reason)
                    _session.queue_log(f"  {count}× {label}", "muted")
        except Exception as e:
            _session.queue_log(f"Bulk change error: {e}", "error")
        finally:
            _session.queue_log("__done__", "__done__")

    _session.thread = threading.Thread(target=run, daemon=True)
    _session.thread.start()
    return {"started": True}


_limits_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_limits_cache_lock = threading.Lock()
_LIMITS_CACHE_TTL = 45.0  # seconds — shared across every connected user, not per-request


@router.get("/limits")
def get_limits(_=Depends(get_current_user)):
    """Fetch limits from Bloxgen for all keys, cached briefly and shared across
    every connected user. Without this, N people with the Limits tab open each
    poll independently, multiplying real Bloxgen API calls by N and risking
    Bloxgen's own rate limit — one fetch should serve everyone."""
    with _limits_cache_lock:
        cached = _limits_cache["data"]
        age = time.time() - _limits_cache["ts"]
        if cached is not None and age < _LIMITS_CACHE_TTL:
            return cached

    keys = _get_keys(_session.cfg.get("bloxgen_keys") or [])
    if not keys:
        raise HTTPException(400, "No API keys configured")
    ssl_ctx = make_ssl_context(bool(_session.cfg.get("ssl_verify", True)))
    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    def _lim(k):
        return bloxgen_daily_limit(k, ssl_ctx)
    with ThreadPoolExecutor(max_workers=min(len(keys), 6)) as pool:
        futs = {pool.submit(_lim, k): k for k in keys}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:
                pass

    # Aggregate remainingGenerations/dailyLimit/generationsToday per account
    # type across all keys — matches the desktop app's _merge_daily_limits.
    type_agg: Dict[str, Dict[str, Any]] = {}
    reset_time = ""
    for lim in results:
        if not isinstance(lim, dict):
            continue
        rt = str(lim.get("resetTime") or "")
        if rt and (not reset_time or rt < reset_time):
            reset_time = rt
        for item in lim.get("accountTypes") or []:
            if not isinstance(item, dict):
                continue
            at = item.get("accountType")
            if not at:
                continue
            at = str(at)
            bucket = type_agg.setdefault(at, {
                "generationsToday": 0,
                "dailyLimit": 0,
                "remainingGenerations": 0,
                "canGenerate": False,
            })
            tu = int(item.get("generationsToday") or 0)
            td = int(item.get("dailyLimit") or 0)
            tl = item.get("remainingGenerations")
            tl = int(tl) if tl is not None else max(0, td - tu)
            bucket["generationsToday"] += tu
            bucket["dailyLimit"] += td
            bucket["remainingGenerations"] += tl
            if bool(item.get("canGenerate", tl > 0)):
                bucket["canGenerate"] = True

    stock: Dict[str, Any] = {}
    stock_error = None
    for k in keys:
        try:
            raw = bloxgen_stock(k, ssl_ctx)
            stock = {t: v for t, v in raw.items() if t in TYPE_META}
            stock_error = None
            break
        except Exception as e:
            stock_error = str(e)
            continue

    response = {
        "limits": results,
        "stock": stock,
        "types": ACCOUNT_TYPES,
        "stock_error": stock_error if not stock else None,
        "quota": type_agg,       # per-type remaining/dailyLimit/generationsToday, summed across keys
        "reset_time": reset_time,
    }

    with _limits_cache_lock:
        _limits_cache["data"] = response
        _limits_cache["ts"] = time.time()

    return response


@router.get("/stream")
async def stream_logs(token: str = ""):
    """Server-Sent Events — streams live feed to browser.
    Uses ?token= query param because EventSource cannot set Authorization headers.
    """
    from jose import jwt
    import os
    is_admin_viewer = True
    try:
        secret = os.environ.get("JWT_SECRET_KEY", "")
        algo   = os.environ.get("JWT_ALGORITHM", "HS256")
        if not secret or not token:
            raise ValueError("missing token or secret")
        payload = jwt.decode(token, secret, algorithms=[algo])
        # Standard FastAPI/OAuth2 convention stores the username under "sub".
        # If this project's security.py uses a different claim name, this
        # check silently falls back to treating the viewer as non-admin
        # (fails closed — safer than accidentally exposing vault lines).
        viewer_username = str(payload.get("sub", "")).strip().lower()
        is_admin_viewer = is_admin_username(viewer_username)
    except Exception:
        from fastapi.responses import Response
        return Response(status_code=401)

    async def event_generator():
        last_len = 0
        heartbeat = 0
        while True:
            current = list(_session.log_q)
            new_items = current[last_len:]
            for item in new_items:
                if item["level"] == "__done__":
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return
                # This feed is a single shared broadcast — if an admin is
                # running a vault-enabled session while a non-admin happens
                # to be watching, redact vault-related lines rather than
                # leak them through the shared stream.
                if not is_admin_viewer and "vault" in item.get("msg", "").lower():
                    continue
                yield f"data: {json.dumps(item)}\n\n"
            last_len = len(current)
            heartbeat += 1
            if heartbeat % 25 == 0:  # heartbeat every ~2.5s
                yield f"data: {json.dumps({'heartbeat': True})}\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
