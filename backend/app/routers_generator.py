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

from .deps import get_current_user
from .generator_core import (
    GeneratorWorker, RunStats, Vault, TYPE_META, ACCOUNT_TYPES,
    load_config, save_config, load_usage, save_usage,
    DEFAULT_CONFIG,
    _get_keys, _secure_load, make_ssl_context, _http,
    bloxgen_daily_limit, bloxgen_stock,
    filter_accounts, load_accounts,
)

router = APIRouter(prefix="/generator", tags=["generator"])

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

    def queue_account(self, *args):
        pass  # accounts saved to disk by append_account() in generator_core

    def queue_usage(self, key: str, count: int):
        pass  # usage handled by bump_key_usage() in generator_core

    def build_run_cfg(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Merge frontend-editable fields into the persisted session config.

        CRITICAL: never replace self.cfg outright with a frontend payload —
        the GeneratorConfig schema deliberately excludes vault_api/vault_user/
        vault_pass (so the browser can't see or override them). A naive
        `self.cfg = overrides` wipes those keys permanently once save_config()
        clears and rewrites _MEM_CONFIG from the (now incomplete) dict.
        """
        merged = dict(self.cfg)
        merged.update(overrides)
        return merged


_session = _Session()

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
def get_status(_=Depends(get_current_user)):
    s = _session.stats
    return StatusResponse(
        running=_session.is_running(),
        session_id=_session._session_id,
        done=s.done if s else 0,
        stock_empty=s.stock_empty if s else 0,
        fails=s.fails if s else 0,
        vault_stock=_session.cfg.get("_vault_stock", -1),
        key_states=_session.key_states,
        account_type=_session.cfg.get("account_type", "+30 days old"),
    )


@router.post("/start")
def start_session(config: GeneratorConfig, _=Depends(get_current_user)):
    if _session.is_running():
        raise HTTPException(400, "Session already running")

    # Merge — NEVER replace. Preserves vault_api/vault_user/vault_pass,
    # which the GeneratorConfig schema deliberately omits.
    cfg = _session.build_run_cfg(config.model_dump())
    cfg["_dry_run"] = False
    _session.cfg = cfg
    save_config(cfg)

    _session._stop.clear()
    _session._pause.clear()
    _session.key_states = {}
    _session.log_q.clear()

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
def dry_run(_=Depends(get_current_user)):
    if _session.is_running():
        raise HTTPException(400, "Session already running")

    cfg = dict(_session.cfg)
    cfg["_dry_run"] = True
    _session._stop.clear()
    _session.log_q.clear()
    _session.key_states = {}
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
def update_config(config: GeneratorConfig, _=Depends(get_current_user)):
    if _session.is_running():
        raise HTTPException(400, "Cannot update config while running")
    # Merge only — same rule as /start. Vault credentials untouched.
    merged = _session.build_run_cfg(config.model_dump())
    _session.cfg = merged
    save_config(merged)
    return {"saved": True}


@router.get("/config")
def get_config(_=Depends(get_current_user)):
    cfg = dict(_session.cfg)
    # Never send vault credentials to frontend
    for k in ("vault_pass", "vault_api", "vault_user", "_dry_run"):
        cfg.pop(k, None)
    return cfg


@router.get("/accounts")
def get_accounts(search: str = "", account_type: str = "", limit: int = 1000, _=Depends(get_current_user)):
    """Fetch accounts from vault (source of truth — Railway has no persistent disk
    for the local .deltacore_accounts.json fallback)."""
    s = (search or "").strip().lower()
    t = (account_type or "").strip().lower()
    limit = max(1, min(limit, 5000))  # sane bounds — vault could theoretically have thousands
    try:
        ssl_ctx = make_ssl_context(bool(_session.cfg.get("ssl_verify", True)))
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

        accounts = []
        for a in raw:
            if not isinstance(a, dict):
                continue
            user = str(a.get("username") or a.get("user") or "")
            pw = str(a.get("password") or a.get("pass") or "")
            a_type = str(a.get("type") or a.get("account_type") or "")
            if s and s not in user.lower() and s not in pw.lower():
                continue
            if t and a_type.strip().lower() != t:
                continue
            accounts.append({
                "user": user,
                "pass": pw[:2] + "***" + pw[-2:] if len(pw) > 4 else "***",
                "date": a.get("date") or a.get("created_at") or "—",
                "age": a.get("age") or "—",
                "type": a_type or "—",
                "pw_changed": bool(a.get("pw_changed") or a.get("password_changed")),
                "vault_pushed": True,
            })
        return accounts[:limit]
    except Exception as e:
        # Fallback to local file (only useful if a disk-backed run wrote it)
        accounts = filter_accounts(search=search, account_type=account_type or "")
        return [
            {**a, "pass": a["pass"][:2] + "***" + a["pass"][-2:] if len(a.get("pass", "")) > 4 else "***"}
            for a in accounts[:limit]
        ]


@router.post("/accounts/change-passwords")
def change_unchanged_passwords(_=Depends(get_current_user)):
    """Bulk password change for vault accounts that haven't been changed yet.

    Each account needs a fresh Roblox login (no cookie is stored in the vault
    for old accounts). Roblox challenges (Arkose/FunCaptcha) programmatic
    logins from datacenter IPs like Railway's, so a meaningful fraction of
    accounts will be skipped — that's expected, not a bug. Progress streams
    through the same /generator/stream feed as Start/Stats.
    """
    if _session.is_running():
        raise HTTPException(400, "Another job is already running")

    new_password = str(_session.cfg.get("new_password") or "").strip()
    if len(new_password) < 8:
        raise HTTPException(400, "Set a new password (8+ chars) in Config first")

    _session._stop.clear()
    _session.log_q.clear()
    import uuid as _uuid
    _session._session_id = _uuid.uuid4().hex[:12]

    def run():
        from .generator_core import roblox_login, provider_change_password
        try:
            ssl_ctx = make_ssl_context(bool(_session.cfg.get("ssl_verify", True)))
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
                already = bool(a.get("pw_changed") or a.get("password_changed"))
                if already:
                    continue
                user = str(a.get("username") or a.get("user") or "")
                pw = str(a.get("password") or a.get("pass") or "")
                if user and pw:
                    targets.append((user, pw))

            _session.queue_log(f"=== BULK PASSWORD CHANGE ===", "ok")
            _session.queue_log(f"{len(targets)} unchanged account(s) found", "info")

            changed = skipped = 0
            for user, old_pw in targets:
                if _session._stop.is_set():
                    _session.queue_log("Stopped by user", "warn")
                    break
                if old_pw == new_password:
                    _session.queue_log(f"Skip {user}: already using target password", "muted")
                    skipped += 1
                    continue
                try:
                    login_ok, login_result = roblox_login(user, old_pw, ssl_ctx)
                except Exception as e:
                    login_ok, login_result = False, str(e)
                if not login_ok:
                    _session.queue_log(f"Skip {user}: login failed ({login_result})", "warn")
                    skipped += 1
                    time.sleep(0.5)
                    continue
                ok, reason = provider_change_password(login_result, old_pw, new_password, ssl_ctx)
                if not ok:
                    _session.queue_log(f"Skip {user}: PW change failed ({reason})", "warn")
                    skipped += 1
                    time.sleep(0.5)
                    continue
                pushed, detail = v.update_password(user, new_password)
                if pushed:
                    _session.queue_log(f"Changed {user} (vault updated)", "ok")
                    changed += 1
                else:
                    _session.queue_log(f"Changed {user} but vault update failed ({detail})", "warn")
                    changed += 1
                time.sleep(0.5)

            _session.queue_log(f"=== DONE — {changed} changed, {skipped} skipped ===", "ok")
        except Exception as e:
            _session.queue_log(f"Bulk change error: {e}", "error")
        finally:
            _session.queue_log("__done__", "__done__")

    _session.thread = threading.Thread(target=run, daemon=True)
    _session.thread.start()
    return {"started": True}


@router.get("/limits")
def get_limits(_=Depends(get_current_user)):
    """Fetch fresh limits from Bloxgen for all keys."""
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

    return {
        "limits": results,
        "stock": stock,
        "types": ACCOUNT_TYPES,
        "stock_error": stock_error if not stock else None,
        "quota": type_agg,       # per-type remaining/dailyLimit/generationsToday, summed across keys
        "reset_time": reset_time,
    }


@router.get("/stream")
async def stream_logs(token: str = ""):
    """Server-Sent Events — streams live feed to browser.
    Uses ?token= query param because EventSource cannot set Authorization headers.
    """
    # Validate JWT manually (same logic as get_current_user in deps.py)
    from jose import jwt, JWTError
    import os
    try:
        secret = os.environ.get("JWT_SECRET_KEY", "")
        algo   = os.environ.get("JWT_ALGORITHM", "HS256")
        if not secret or not token:
            raise ValueError("missing token or secret")
        jwt.decode(token, secret, algorithms=[algo])
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
