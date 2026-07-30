from __future__ import annotations
"""generator_core.py — DeltaCore engine for Railway. No UI deps."""
import os
import logging

logger = logging.getLogger("generator_core")

def _flog(msg: str) -> None:
    logger.debug(str(msg))



import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from urllib.parse import quote as _url_quote
import uuid
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from copy import copy as _copy
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


def _run_nowin(args, **kwargs):
    """Run a subprocess without spawning a console window on Windows."""
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs.setdefault("startupinfo", si)
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.run(args, **kwargs)

# ========== in-memory store (no disk files) ==========
APP_DIR = Path(__file__).resolve().parent
CONFIG_VERSION = 2

_lock_config = threading.Lock()
_lock_usage = threading.Lock()
_lock_accounts = threading.Lock()

# live state held only in RAM for this run
_MEM_CONFIG: Dict[str, Any] = {}
_MEM_USAGE: Dict[str, Any] = {}
_MEM_ACCOUNTS: List[Dict[str, Any]] = []
_ACCOUNT_USERS: Set[str] = set()
_USAGE_PATH    = Path.home() / ".deltacore_usage.json"
_ACCOUNTS_PATH = Path.home() / ".deltacore_accounts.json"

# type -> (cooldown_sec, daily_limit_per_key)
TYPE_META: Dict[str, Tuple[float, int]] = {
    "+30 days old": (30.0, 30),
    "+1 year old": (600.0, 15),
    "5+ years old": (1800.0, 10),
    "dump": (2700.0, 5),
}
ACCOUNT_TYPES = list(TYPE_META.keys())

DEFAULT_CONFIG: Dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "bloxgen_keys": [
        "BLOX-Z8M9R1XA6EZDD1B9",
        "BLOX-ESKBR5QF6W1VSQYH",
        "BLOX-BVFCF7XVRXI0VMK2",
        "BLOX-20FIDYUWMQHMRGWB",
        "BLOX-L6KHNWRTTSULP8FS",
        "BLOX-BYTINVTIEFDHNTC3",
        "BLOX-PCTXK5UCTB7DLTQW",
    ],
    "account_type": "+30 days old",
    "new_password": "",
    "vault_enabled": True,
    "vault_api": "https://bot-production-7427.up.railway.app",
    "vault_user": "Trickzz",
    "vault_pass": "acc-gen123",
    "ssl_verify": True,
    "target_count": 0,
}

def _get_keys(cfg_keys: list) -> List[str]:
    """Return configured keys, or DEFAULT if empty/placeholder only."""
    keys = [str(k).strip() for k in (cfg_keys or []) if str(k).strip()]
    keys = [k for k in keys if k and k != "YOUR_BLOXGEN_KEY_HERE"]
    if keys:
        return keys
    return [str(k).strip() for k in DEFAULT_CONFIG.get("bloxgen_keys") or [] if str(k).strip()]

# ========== config (RAM only — never writes JSON files) ==========
def load_config() -> Dict[str, Any]:
    with _lock_config:
        if not _MEM_CONFIG:
            base = dict(DEFAULT_CONFIG)
            base["bloxgen_keys"] = list(DEFAULT_CONFIG["bloxgen_keys"])
            _MEM_CONFIG.update(base)
        cfg = dict(_MEM_CONFIG)
        cfg["bloxgen_keys"] = list(_MEM_CONFIG.get("bloxgen_keys") or DEFAULT_CONFIG["bloxgen_keys"])
    if cfg.get("account_type") not in TYPE_META:
        cfg["account_type"] = "+30 days old"
    return cfg

def save_config(cfg: Dict[str, Any]) -> None:
    """Keep settings in RAM only — never writes to disk."""
    with _lock_config:
        stored = dict(cfg)
        stored["config_version"] = CONFIG_VERSION
        keys = stored.get("bloxgen_keys") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.replace(",", "\n").splitlines() if k.strip()]
        stored["bloxgen_keys"] = [str(k).strip() for k in keys if str(k).strip()]
        _MEM_CONFIG.clear()
        _MEM_CONFIG.update(stored)

# ========== usage (RAM + file persistence) ==========
_today_key_cache: str = ""
_today_key_ts: float = 0.0
_lock_today_key = threading.Lock()

def _today_key() -> str:
    """Day key resets at midnight UTC. Cached with 60s TTL."""
    global _today_key_cache, _today_key_ts
    t = time.time()
    if t - _today_key_ts >= 60.0:
        with _lock_today_key:
            if t - _today_key_ts >= 60.0:  # double-check after acquiring lock
                _today_key_cache = datetime.now(timezone.utc).date().isoformat()
                _today_key_ts = t
    return _today_key_cache

def load_usage() -> Dict[str, Any]:
    with _lock_usage:
        # Try loading from disk first
        if not _MEM_USAGE:
            try:
                if _USAGE_PATH.exists():
                    raw = _USAGE_PATH.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else {}
                    if isinstance(data, dict):
                        _MEM_USAGE.update(data)
            except Exception:
                pass
        if not _MEM_USAGE or _MEM_USAGE.get("day") != _today_key():
            _MEM_USAGE.clear()
            _MEM_USAGE.update({"day": _today_key(), "keys": {}})
        if not isinstance(_MEM_USAGE.get("keys"), dict):
            _MEM_USAGE["keys"] = {}
        return {"day": _MEM_USAGE["day"], "keys": dict(_MEM_USAGE["keys"])}

def save_usage(usage: Dict[str, Any]) -> None:
    with _lock_usage:
        _MEM_USAGE.clear()
        day = usage.get("day") or _today_key()
        keys = usage.get("keys") if isinstance(usage.get("keys"), dict) else {}
        _MEM_USAGE.update({"day": day, "keys": dict(keys)})
        try:
            tmp = _USAGE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": day, "keys": dict(keys)}), encoding="utf-8")
            tmp.replace(_USAGE_PATH)
        except Exception:
            pass

def get_key_usage(usage: Dict[str, Any], key: str) -> int:
    today = usage.get("day")
    if not today or today != _today_key():
        usage["day"] = _today_key()
        usage["keys"] = {}
        save_usage(usage)
    return int(usage.get("keys", {}).get(key, 0))

_last_usage_flush: float = 0.0
_USAGE_FLUSH_INTERVAL = 60.0  # flush usage to disk at most every 60s
_lock_usage_flush = threading.Lock()

def bump_key_usage(usage: Dict[str, Any], key: str) -> int:
    """Only call on successful generate."""
    global _last_usage_flush
    if usage.get("day") != _today_key():
        usage["day"] = _today_key()
        usage["keys"] = {}
    keys = usage.setdefault("keys", {})
    keys[key] = int(keys.get(key, 0)) + 1
    # throttle disk writes — flush at most every 60s
    with _lock_usage_flush:
        if time.time() - _last_usage_flush >= _USAGE_FLUSH_INTERVAL:
            save_usage(usage)
            _last_usage_flush = time.time()
    return int(keys[key])

# ========== accounts (RAM only) ==========
def load_accounts() -> List[Dict[str, Any]]:
    with _lock_accounts:
        if not _MEM_ACCOUNTS:
            try:
                if _ACCOUNTS_PATH.exists():
                    raw = _ACCOUNTS_PATH.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else []
                    if isinstance(data, list):
                        for a in data:
                            if isinstance(a, dict):
                                _MEM_ACCOUNTS.append(dict(a))
                        _rebuild_user_index_locked()
            except Exception:
                pass
        # Return shallow copy of list — dicts are read-only in UI paths
        return list(_MEM_ACCOUNTS)

def _rebuild_user_index_locked() -> None:
    _ACCOUNT_USERS.clear()
    for a in _MEM_ACCOUNTS:
        if isinstance(a, dict):
            u = a.get("user")
            if u:
                _ACCOUNT_USERS.add(str(u))

def save_accounts(accounts: List[Dict[str, Any]]) -> None:
    with _lock_accounts:
        _MEM_ACCOUNTS.clear()
        for a in accounts or []:
            if isinstance(a, dict):
                _MEM_ACCOUNTS.append(dict(a))
        _rebuild_user_index_locked()
    with _lock_accounts:
        _snap = list(_MEM_ACCOUNTS)
    _flush_accounts_to_disk(_snap)  # serializes outside the lock

_AGE_RE_D = re.compile(r"\+?\s*(\d+)\s*d(?:ay)?s?")
_AGE_RE_Y = re.compile(r"\+?\s*(\d+)\s*y(?:ear)?s?")
_AGE_RE_M = re.compile(r"\+?\s*(\d+)\s*m(?:onth)?s?")

def normalize_age(raw: Any) -> str:
    """Normalize Bloxgen age strings into consistent labels."""
    s = str(raw).strip().lower() if raw is not None else "?"
    if s in ("?", "", "none"):
        return "?"
    m = _AGE_RE_D.match(s)
    if m:
        return f"{m.group(1)}d"
    m = _AGE_RE_Y.match(s)
    if m:
        return f"{m.group(1)}y"
    m = _AGE_RE_M.match(s)
    if m:
        return f"{m.group(1)}m"
    return s[:32]

_last_accounts_flush: float = 0.0
_ACCOUNTS_FLUSH_INTERVAL = 30.0  # seconds

def _flush_accounts_to_disk(snapshot: list) -> None:
    global _last_accounts_flush
    try:
        data = json.dumps(snapshot)
        tmp = _ACCOUNTS_PATH.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(_ACCOUNTS_PATH)
        _last_accounts_flush = time.time()
    except Exception:
        pass

def append_account(
    username: str,
    password: str,
    password_original: str,
    age_label: str,
    account_type: str,
    pw_changed: bool,
    vault_pushed: bool,
    api_key_tail: str,
    session_id: str,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "user": username,
        "pass": password,
        "pass_original": password_original,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "age": normalize_age(age_label),
        "type": account_type or "?",
        "pw_changed": pw_changed,
        "vault_pushed": vault_pushed,
        "api_key_tail": api_key_tail or "",
        "session_id": session_id or "",
    }
    if not username:
        logger.debug("append_account: empty username — skipping")
        return {}
    logger.debug(f"append_account {username}")
    snapshot = None
    with _lock_accounts:
        if username in _ACCOUNT_USERS:
            _MEM_ACCOUNTS[:] = [
                a for a in _MEM_ACCOUNTS
                if not (isinstance(a, dict) and a.get("user") == username)
            ]
        _MEM_ACCOUNTS.insert(0, dict(entry))
        if len(_MEM_ACCOUNTS) > 2000:
            trimmed = _MEM_ACCOUNTS[1000:]
            _MEM_ACCOUNTS[1000:] = []
            # Remove trimmed users from set only if not in kept slice
            kept = {a["user"] for a in _MEM_ACCOUNTS if isinstance(a, dict) and "user" in a}
            _ACCOUNT_USERS.intersection_update(kept)
        _ACCOUNT_USERS.add(username)
        # take snapshot inside lock, flush outside — never re-acquire lock
        if time.time() - _last_accounts_flush >= _ACCOUNTS_FLUSH_INTERVAL:
            snapshot = list(_MEM_ACCOUNTS)
    if snapshot is not None:
        logger.debug("flushing accounts to disk")
        _flush_accounts_to_disk(snapshot)
    logger.debug("append_account done")
    return entry

def is_duplicate(username: str) -> bool:
    with _lock_accounts:
        return username in _ACCOUNT_USERS

def filter_accounts(
    accounts: Optional[List[Dict[str, Any]]] = None,
    *,
    search: str = "",
    account_type: str = "",
    date_from: str = "",
    date_to: str = "",
) -> List[Dict[str, Any]]:
    """Filter accounts by search string, type, and date range — single pass."""
    if accounts is None:
        accounts = load_accounts()
    s = (search or "").strip().lower()
    t = (account_type or "").strip().lower()
    df = date_from or ""
    dt = date_to or ""
    if not s and not t and not df and not dt:
        return list(accounts)
    results = []
    for a in accounts:
        if s and s not in str(a.get("user", "")).lower() and s not in str(a.get("pass", "")).lower():
            continue
        if t and str(a.get("type", "")).lower() != t:
            continue
        d = str(a.get("date", ""))
        if df and d < df:
            continue
        if dt and d > dt:
            continue
        results.append(a)
    return results

def export_accounts(
    fmt: str = "userpass",
    accounts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Export accounts as user:pass (text), CSV, or JSON."""
    if accounts is None:
        accounts = load_accounts()
    if fmt == "userpass":
        return "\n".join(f"{a.get('user','')}:{a.get('pass','')}" for a in accounts)
    elif fmt == "csv":
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["user", "pass", "date", "age", "type", "pw_changed", "vault_pushed"])
        for a in accounts:
            w.writerow([
                a.get("user", ""), a.get("pass", ""), a.get("date", ""),
                a.get("age", ""), a.get("type", ""),
                a.get("pw_changed", False), a.get("vault_pushed", False),
            ])
        return buf.getvalue()
    elif fmt == "json":
        return json.dumps(accounts, indent=2)
    return ""

# ========== key helpers ==========
def key_tail(key: str) -> str:
    """Masked identifier for logs/tables: **** + last 4."""
    k = str(key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return "****" + k[-4:]

def key_short(key: str) -> str:
    """UI-safe masked form: first 4 + **** + last 4."""
    k = str(key or "").strip()
    if not k:
        return ""
    if len(k) <= 12:
        return k[:4] + "****"
    return k[:4] + "****" + k[-4:]

# ====================================================================
#  worker module (inlined)
# ====================================================================

BLOXGEN_BASE  = "https://core.bloxgen.net"
BLOXGEN_GEN   = f"{BLOXGEN_BASE}/api/generate"
BLOXGEN_DAILY = f"{BLOXGEN_BASE}/api/daily-limit"
BLOXGEN_STOCK = f"{BLOXGEN_BASE}/api/stock"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ========== SSL ==========
_SSL_CTX_CACHE: Dict[bool, ssl.SSLContext] = {}
_SSL_CTX_LOCK = threading.Lock()

def make_ssl_context(verify: bool) -> ssl.SSLContext:
    with _SSL_CTX_LOCK:
        ctx = _SSL_CTX_CACHE.get(verify)
        if ctx is None:
            ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
            _SSL_CTX_CACHE[verify] = ctx
        return ctx

# ========== HTTP ==========
PROXY_URL = os.environ.get("ROBLOX_PROXY_URL", "").strip()
_proxy_opener: Optional["urllib.request.OpenerDirector"] = None
_proxy_opener_lock = threading.Lock()

def _get_proxy_opener():
    """Build (once) and cache a urllib opener routed through the residential
    proxy. Only used for Roblox auth calls — Bloxgen and vault traffic stay
    direct since only Roblox's login endpoint challenges datacenter IPs."""
    global _proxy_opener
    if _proxy_opener is not None:
        return _proxy_opener
    with _proxy_opener_lock:
        if _proxy_opener is None:
            handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
            _proxy_opener = urllib.request.build_opener(handler)
    return _proxy_opener

def _http(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body: Any = None,
    ssl_ctx: Optional[ssl.SSLContext] = None,
    timeout: float = 30.0,
    use_proxy: bool = False,
) -> Tuple[int, Any, Dict[str, str]]:
    hdrs = {"User-Agent": UA, "Connection": "keep-alive"}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        if use_proxy and PROXY_URL:
            opener = _get_proxy_opener()
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                out_headers = {k.lower(): v for k, v in resp.getheaders()}
                try:
                    parsed = json.loads(text) if text else None
                except json.JSONDecodeError:
                    parsed = text
                return resp.status, parsed, out_headers
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            out_headers = {k.lower(): v for k, v in resp.getheaders()}
            try:
                parsed = json.loads(text) if text else None
            except json.JSONDecodeError:
                parsed = text
            return resp.status, parsed, out_headers
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        text = raw.decode("utf-8", errors="replace")
        out_headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError:
            parsed = text
        return e.code, parsed, out_headers
    except Exception as e:
        return 0, str(e), {}

# ========== vault ==========
class Vault:
    TOKEN_TTL = 3300  # refresh token 5 min before typical 1hr expiry

    def __init__(self, base: str, user: str, password: str, ssl_ctx: ssl.SSLContext):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.ssl_ctx = ssl_ctx
        self.token: Optional[str] = None
        self._token_time: float = 0.0

    def _token_fresh(self) -> bool:
        return bool(self.token) and (time.time() - self._token_time) < self.TOKEN_TTL

    def _hdr(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def login(self) -> str:
        last_exc = None
        for attempt in range(2):  # one retry on transient failure
            try:
                code, body, _ = _http(
                    "POST",
                    f"{self.base}/auth/login",
                    body={"username": self.user, "password": self.password},
                    ssl_ctx=self.ssl_ctx,
                )
                if code < 200 or code >= 300:
                    raise RuntimeError(f"vault login HTTP {code}: {body}")
                token = None
                if isinstance(body, dict):
                    token = (
                        body.get("access_token")
                        or body.get("token")
                        or body.get("accessToken")
                    )
                    if not token and isinstance(body.get("data"), dict):
                        token = body["data"].get("access_token")
                if not token:
                    raise RuntimeError(f"vault login: no token in {body}")
                self.token = str(token)
                self._token_time = time.time()
                try:
                    code2, me, _ = _http(
                        "GET", f"{self.base}/auth/me",
                        headers=self._hdr(), ssl_ctx=self.ssl_ctx,
                    )
                    if 200 <= code2 < 300 and isinstance(me, dict):
                        return str(me.get("username") or me.get("user") or self.user)
                except Exception:
                    pass
                return self.user
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    time.sleep(1.0)  # brief pause before retry
        raise RuntimeError(f"vault login failed after 2 attempts: {last_exc}")

    def get_account_count(self) -> int:
        if not self.token or not self._token_fresh():
            self.login()
        code, body, _ = _http(
            "GET", f"{self.base}/accounts",
            headers=self._hdr(), ssl_ctx=self.ssl_ctx,
        )
        if code < 200 or code >= 300:
            return -1
        if isinstance(body, list):
            return len(body)
        if isinstance(body, dict):
            if isinstance(body.get("accounts"), list):
                return len(body["accounts"])
            for k in ("count", "total", "stock", "length"):
                if isinstance(body.get(k), int):
                    return int(body[k])
        return -1

    def add_account(self, username: str, password: str, idempotent: bool = True) -> bool:
        """Push account to vault. Returns True if pushed, False if skipped (duplicate).

        idempotent=True relies on the vault returning HTTP 409 on duplicate — avoids
        the full GET /accounts round-trip that _has_account() required.
        """
        if not self.token:
            self.login()
        code, body, _ = _http(
            "POST", f"{self.base}/accounts",
            headers=self._hdr(),
            body={"username": username, "password": password},
            ssl_ctx=self.ssl_ctx,
        )
        if code == 401:
            self.login()
            code, body, _ = _http(
                "POST", f"{self.base}/accounts",
                headers=self._hdr(),
                body={"username": username, "password": password},
                ssl_ctx=self.ssl_ctx,
            )
        if code == 409:
            # vault already has it — skip cleanly
            return False
        if code < 200 or code >= 300:
            raise RuntimeError(f"vault add HTTP {code}: {body}")
        return True

    def update_password(self, username: str, new_password: str) -> Tuple[bool, str]:
        """Update a stored account's password in the vault.

        No update endpoint is confirmed for this vault API, so this tries the
        common REST shapes in order and reports which one (if any) succeeded —
        useful for diagnosing the actual endpoint from the response text if
        every attempt fails.
        """
        if not self.token or not self._token_fresh():
            self.login()
        attempts = [
            ("PATCH", f"{self.base}/accounts/{username}", {"password": new_password}),
            ("PUT",   f"{self.base}/accounts/{username}", {"password": new_password}),
            ("POST",  f"{self.base}/accounts/{username}/password", {"password": new_password}),
        ]
        last_err = ""
        for method, url, body in attempts:
            try:
                code, resp_body, _ = _http(method, url, headers=self._hdr(), body=body, ssl_ctx=self.ssl_ctx)
                if 200 <= code < 300:
                    return True, f"{method} {url}"
                last_err = f"{method} {url} -> HTTP {code}: {resp_body}"
            except Exception as e:
                last_err = f"{method} {url} -> {e}"
        return False, last_err

# ========== bloxgen ==========
class BloxgenError(RuntimeError):
    """Categorised Bloxgen error."""
    def __init__(self, message: str, category: str = "unknown", extra: Dict[str, Any] = None):
        super().__init__(message)
        self.category = category  # stock_empty, auth, rate_limit, server, timeout, unknown
        self.extra = extra or {}

def bloxgen_generate(
    api_key: str,
    account_type: str,
    ssl_ctx: ssl.SSLContext,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    def _do_request():
        return _http(
            "POST",
            BLOXGEN_GEN,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            body={"apiKey": api_key, "type": account_type},
            ssl_ctx=ssl_ctx,
            timeout=timeout,
        )

    try:
        code, body, _ = _do_request()
    except Exception as e:
        err_str = str(e).lower()
        if "10054" in err_str or "connection reset" in err_str or "forcibly closed" in err_str:
            # Remote host killed the connection — wait briefly and retry once
            time.sleep(0.5)
            try:
                code, body, _ = _do_request()
            except Exception as e2:
                raise BloxgenError(f"connection reset after retry: {e2}", "timeout")
        else:
            raise

    # stock-empty check BEFORE generic HTTP error
    if isinstance(body, dict):
        msg = str(body.get("message", "")).lower()
        if any(w in msg for w in ("no account", "no stock", "none available", "empty")):
            raise BloxgenError(f"stock empty: {body.get('message', msg)}", "stock_empty")

    if code == 0 and isinstance(body, str) and "time" in body.lower():
        raise BloxgenError(f"timeout: {body}", "timeout")

    # Retry once on auth errors before permanently killing the key —
    # a transient 401/403 (server blip, brief suspension) shouldn't end the session.
    if code in (401, 403):
        time.sleep(0.5)
        code, body, _ = _do_request()
        if isinstance(body, dict):
            msg = str(body.get("message", "")).lower()
            if any(w in msg for w in ("no account", "no stock", "none available", "empty")):
                raise BloxgenError(f"stock empty: {body.get('message', msg)}", "stock_empty")
        if code == 401:
            raise BloxgenError("Bloxgen 401 unauthorized", "auth")
        if code == 403:
            raise BloxgenError("Bloxgen 403 forbidden", "auth")
    if code == 429:
        wait_ms = body.get("timeRemaining") if isinstance(body, dict) else None
        extra: Dict[str, Any] = {}
        if isinstance(wait_ms, (int, float)):
            extra["timeRemaining"] = wait_ms
        raise BloxgenError("Bloxgen rate limited", "rate_limit", extra)
    if 500 <= code < 600:
        raise BloxgenError(f"Bloxgen server error HTTP {code}", "server")
    if code < 200 or code >= 300:
        raise BloxgenError(f"Bloxgen HTTP {code}: {body}", "unknown")

    if not isinstance(body, dict):
        raise BloxgenError(f"Bloxgen bad response: {body}", "unknown")

    # 200 with error payload
    if body.get("errors") or (body.get("success") is False):
        err_msg = str(body.get("errors") or body.get("message") or body)
        raise BloxgenError(f"Bloxgen API error: {err_msg}", "unknown")

    data = body.get("data", body)
    if isinstance(data, dict):
        account = data.get("account", data)
    else:
        account = data

    username = account.get("username") or account.get("user") or body.get("username")
    password = account.get("password") or account.get("pass") or body.get("password")
    cookie = (
        account.get("cookie")
        or account.get(".ROBLOSECURITY")
        or account.get("robloxSecurity")
        or body.get("cookie")
    )
    age = account.get("age") or account.get("accountAge") or body.get("age") or "?"

    if not username or not password:
        msg = str(body.get("message") or body.get("error") or body)
        low = msg.lower()
        if any(w in low for w in ("empty", "no account", "stock", "none")):
            raise BloxgenError(f"stock empty: {msg}", "stock_empty")
        raise BloxgenError(f"missing user/pass: {body}", "unknown")

    return {
        "username": str(username),
        "password": str(password),
        "cookie": str(cookie) if cookie else None,
        "age": str(age),
    }

_daily_limit_url_cache: Dict[str, str] = {}

def bloxgen_daily_limit(
    api_key: str,
    ssl_ctx: ssl.SSLContext,
    account_type: Optional[str] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    """Live Bloxgen daily usage / remaining / reset window (account-level)."""
    if account_type:
        url = _daily_limit_url_cache.get(account_type)
        if url is None:
            url = f"{BLOXGEN_DAILY}?type={_url_quote(account_type)}"
            _daily_limit_url_cache[account_type] = url
    else:
        url = BLOXGEN_DAILY
    for attempt in range(2):
        code, body, _ = _http(
            "GET",
            url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            ssl_ctx=ssl_ctx,
            timeout=timeout,
        )
        if code == 429:
            if attempt == 0:
                time.sleep(2.0)
                continue
            raise BloxgenError("daily-limit rate limited (429)", "rate_limit")
        break
    if code < 200 or code >= 300 or not isinstance(body, dict):
        raise BloxgenError(f"daily-limit HTTP {code}: {body}", "unknown")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        raise BloxgenError(f"daily-limit bad body: {body}", "unknown")
    return data

def bloxgen_stock(
    api_key: str,
    ssl_ctx: ssl.SSLContext,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    code, body, _ = _http(
        "GET",
        BLOXGEN_STOCK,
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        ssl_ctx=ssl_ctx,
        timeout=timeout,
    )
    if code < 200 or code >= 300 or not isinstance(body, dict):
        raise BloxgenError(f"stock HTTP {code}: {body}", "unknown")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        raise BloxgenError(f"stock bad body: {body}", "unknown")
    return data

TWOCAPTCHA_API_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()

# Roblox's Arkose (FunCaptcha) public key and challenge subdomain used on the
# login endpoint. These are values embedded in Roblox's own login page JS —
# not secret, but Roblox can rotate them; if solving stops working, this is
# the first thing to re-check against the current login page source.
ROBLOX_ARKOSE_PUBLIC_KEY = "476068BF-9607-4799-B53D-966BE98E2B81"
ROBLOX_ARKOSE_SURL = "https://roblox-api.arkoselabs.com"

def _solve_arkose_captcha(
    ssl_ctx: ssl.SSLContext,
    site_key: str = ROBLOX_ARKOSE_PUBLIC_KEY,
    surl: str = ROBLOX_ARKOSE_SURL,
    page_url: str = "https://www.roblox.com/login",
    max_wait: float = 90.0,
) -> Tuple[bool, str]:
    """Solve a Roblox Arkose/FunCaptcha challenge via 2Captcha.
    Returns (success, token_or_error). Blocks for 15-60s typically —
    call from a worker thread, never from a request handler directly."""
    if not TWOCAPTCHA_API_KEY:
        return False, "no-2captcha-key"

    create_body = {
        "clientKey": TWOCAPTCHA_API_KEY,
        "task": {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websitePublicKey": site_key,
            "funcaptchaApiJSSubdomain": surl,
        },
    }
    try:
        code, body, _ = _http(
            "POST", "https://api.2captcha.com/createTask",
            body=create_body, ssl_ctx=ssl_ctx, timeout=20.0,
        )
    except Exception as e:
        return False, f"2captcha create request failed: {e}"
    if not isinstance(body, dict) or body.get("errorId"):
        err = body.get("errorDescription") if isinstance(body, dict) else str(body)
        return False, f"2captcha create error: {err}"
    task_id = body.get("taskId")
    if not task_id:
        return False, f"2captcha: no taskId in response: {body}"

    # Poll for the result — solving genuinely takes 15-60+ seconds
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(5.0)
        try:
            code, result, _ = _http(
                "POST", "https://api.2captcha.com/getTaskResult",
                body={"clientKey": TWOCAPTCHA_API_KEY, "taskId": task_id},
                ssl_ctx=ssl_ctx, timeout=15.0,
            )
        except Exception as e:
            continue  # transient poll failure — keep trying until max_wait
        if not isinstance(result, dict):
            continue
        status = result.get("status")
        if status == "ready":
            sol = result.get("solution") or {}
            token = sol.get("token")
            if token:
                return True, token
            return False, f"2captcha ready but no token: {result}"
        if status == "processing":
            continue
        if result.get("errorId"):
            return False, f"2captcha solve error: {result.get('errorDescription')}"
    return False, "2captcha timeout — solve took too long"


def roblox_login(
    username: str,
    password: str,
    ssl_ctx: ssl.SSLContext,
    timeout: float = 20.0,
    use_proxy: bool = False,
) -> Tuple[bool, str]:
    """Log in to Roblox and return (success, .ROBLOSECURITY cookie or error reason).

    If Roblox demands a captcha and TWOCAPTCHA_API_KEY is configured, solves
    it via 2Captcha and retries once with the solved token."""
    login_url = "https://auth.roblox.com/v2/login"

    # Step 1: fetch CSRF token
    try:
        _, __, hdrs = _http("POST", login_url,
                            headers={"Content-Type": "application/json"},
                            body={}, ssl_ctx=ssl_ctx, timeout=timeout, use_proxy=use_proxy)
        csrf = hdrs.get("x-csrf-token", "")
    except Exception as e:
        return False, f"csrf-fetch: {e}"

    if not csrf:
        return False, "csrf-empty"

    def _attempt_login(captcha_token: str) -> Tuple[bool, str, Optional[dict]]:
        """Returns (success, cookie_or_reason, error_body_if_any)."""
        login_body = json.dumps({
            "ctype": "Username", "cvalue": username, "password": password,
            "captchaId": "", "captchaToken": captcha_token,
            "captchaProvider": "PROVIDER_ARKOSE_LABS",
        }).encode("utf-8")
        req = urllib.request.Request(
            login_url,
            data=login_body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-TOKEN": csrf,
                "User-Agent": UA,
            },
            method="POST",
        )
        try:
            opener = _get_proxy_opener() if (use_proxy and PROXY_URL) else None
            opened = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx)
            with opened as resp:
                all_headers = resp.getheaders()
                for hdr_name, hdr_val in all_headers:
                    if hdr_name.lower() == "set-cookie" and ".ROBLOSECURITY=" in hdr_val:
                        for part in hdr_val.split(";"):
                            part = part.strip()
                            if part.startswith(".ROBLOSECURITY="):
                                return True, part.split("=", 1)[1], None
                return False, "no-cookie", None
        except urllib.error.HTTPError as e:
            raw = e.read() if e.fp else b""
            try:
                err_body = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                err_body = {}
            errs = err_body.get("errors") or []
            msg = errs[0].get("message", "") if errs else ""
            return False, msg or f"http-{e.code}", err_body
        except Exception as e:
            return False, f"net:{e}", None

    ok, reason, err_body = _attempt_login("")
    if ok:
        return True, reason

    needs_captcha = bool(err_body) and any(
        "captcha" in str(v).lower() or "challenge" in str(v).lower()
        for v in [reason] + [str(e) for e in (err_body.get("errors") or [])]
    )
    if not needs_captcha:
        return False, reason

    if not TWOCAPTCHA_API_KEY:
        return False, f"{reason} (captcha required, no 2captcha key configured)"

    solved_ok, solved_token = _solve_arkose_captcha(ssl_ctx)
    if not solved_ok:
        return False, f"{reason} | captcha solve failed: {solved_token}"

    ok2, reason2, _ = _attempt_login(solved_token)
    return ok2, reason2


def provider_change_password(
    cookie: str,
    current_password: str,
    new_password: str,
    ssl_ctx: ssl.SSLContext,
    timeout: float = 30.0,
    use_proxy: bool = False,
) -> Tuple[bool, str]:
    """Change Roblox account password via .ROBLOSECURITY cookie.

    Returns (success, reason).  reason is empty on success, else a
    short diagnostic string (e.g. "csrf-fetch", "bad-password",
    "http-400", "timeout").

    use_proxy should only be True when the cookie itself was obtained
    through the same proxy (e.g. via roblox_login(use_proxy=True)) —
    mixing a Bloxgen-issued cookie with a different exit IP than whatever
    context it was created under reads as session hijacking to Roblox
    and gets rejected (csrf-rejected / 9002), even though the cookie
    itself would have worked fine over a consistent connection.
    """
    # Roblox rejects same-password changes with a specific error
    if current_password == new_password:
        return False, "same-password"
    url = "https://auth.roblox.com/v2/user/passwords/change"
    body_json = json.dumps({
        "currentPassword": current_password,
        "newPassword": new_password,
    }).encode("utf-8")

    def _try(csrf: str, attempt: int) -> Tuple[bool, str, Optional[str]]:
        """Returns (done, reason, new_csrf)."""
        hdrs: Dict[str, str] = {
            "Content-Type": "application/json",
            "Cookie": f".ROBLOSECURITY={cookie}",
            "User-Agent": UA,
        }
        if csrf:
            hdrs["X-CSRF-TOKEN"] = csrf
        req = urllib.request.Request(url, data=body_json, headers=hdrs, method="POST")
        try:
            opener = _get_proxy_opener() if (use_proxy and PROXY_URL) else None
            opened = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx)
            with opened as resp:
                return (True, "" if resp.status in (200, 204) else f"http-{resp.status}", None)
        except urllib.error.HTTPError as e:
            # Extract CSRF token from 403 response headers
            if e.code == 403 and not csrf and attempt == 0:
                h = {k.lower(): v for k, v in (e.headers or {}).items()}
                tok = h.get("x-csrf-token") or ""
                if tok:
                    return (False, "", tok)
            if e.code == 403 and csrf:
                return (True, "csrf-rejected", None)
            err = f"http-{e.code}"
            try:
                raw = e.read() if e.fp else b""
                body = json.loads(raw.decode("utf-8", errors="replace"))
                errors = body.get("errors") or []
                if errors:
                    first = errors[0]
                    # Roblox code 0 = success even on non-200 status
                    if isinstance(first, dict) and first.get("code", -1) == 0:
                        return (True, "", None)
                    msg = first.get("message", "") if isinstance(first, dict) else str(first)
                    code_val = first.get("code", "") if isinstance(first, dict) else ""
                    err = f"code {code_val}: {msg}".strip(": ") if msg or code_val else err
            except Exception:
                pass
            return (True, err, None)
        except Exception as ex:
            return (True, f"net:{ex}", None)

    # Attempt 1: without CSRF (Roblox returns 403 + token on first try)
    done, reason, tok = _try("", 0)
    if done:
        return (reason == "", reason)
    # Attempt 2: with extracted CSRF token
    if tok:
        done2, reason2, _ = _try(tok, 1)
        return (reason2 == "", reason2)
    return (False, "csrf-fetch")

# ========== key selector (round-robin) ==========
# Simple ordered cycling: 1->2->3->4->5->1->2...
# Skips dead keys and keys at daily limit.

# ========== stats ==========
class RunStats:
    def __init__(self):
        self.done = 0
        self.stock_empty = 0
        self.fails = 0
        self.stopped_manually = False
        self.start_time = time.time()

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

# ========== worker ==========

class GeneratorWorker:
    def __init__(
        self,
        cfg: Dict[str, Any],
        usage: Dict[str, Any],
        log: Callable[[str, str], None],
        stats_cb: Callable[[RunStats, str], None],
        key_cb: Callable[[int, str, str], None],
        account_cb: Callable[[str, str, str, str, bool, bool, str], None],
        usage_cb: Callable[[str, int], None],
        should_stop: Callable[[], bool],
        is_paused: Callable[[], bool],
        session_id: str = "",
    ):
        self.cfg_snapshot = dict(cfg)  # frozen at start
        self.usage = usage
        self.log = log
        self.stats_cb = stats_cb
        self.key_cb = key_cb
        self.account_cb = account_cb
        self.usage_cb = usage_cb
        self.should_stop = should_stop
        self.is_paused = is_paused
        self.session_id = session_id
        self.stats = RunStats()

    def _gap(self, secs: float) -> None:
        if secs <= 0:
            return
        end = time.time() + secs
        while time.time() < end:
            if self.should_stop():
                break
            while self.is_paused() and not self.should_stop():
                time.sleep(0.05)
            rem = end - time.time()
            if rem <= 0:
                break
            time.sleep(min(0.1, rem))

    def _wait_unpause(self) -> bool:
        """Wait while paused, return True if should stop."""
        while self.is_paused():
            if self.should_stop():
                return True
            time.sleep(0.05)
        return False

    def run(self) -> RunStats:
        logger.debug("worker.run() START")
        keys = _get_keys(self.cfg_snapshot.get("bloxgen_keys") or [])
        if not keys:
            logger.debug("NO KEYS — returning")
            self.log("No API keys", "error")
            return self.stats

        account_type = str(self.cfg_snapshot.get("account_type") or "+30 days old")
        cooldown, daily_limit = TYPE_META.get(account_type, (30.0, 30))

        ssl_verify = bool(self.cfg_snapshot.get("ssl_verify", True))
        ssl_ctx = make_ssl_context(ssl_verify)
        target = int(self.cfg_snapshot.get("target_count") or 0)
        dry_run = bool(self.cfg_snapshot.get("_dry_run", False))

        # per-key state (session only)
        dead_keys: set = set()  # keys disabled this session
        key_ready: Dict[str, float] = {}  # earliest time each key may be used again
        n_keys = len(keys)
        ready_set: Set[Tuple[int, str]] = set()

        vault = None
        vault_stock_n: int = -1
        if self.cfg_snapshot.get("vault_enabled") and not dry_run:
            try:
                vault = Vault(
                    str(self.cfg_snapshot["vault_api"]),
                    str(self.cfg_snapshot["vault_user"]),
                    _secure_load("vault_pass", self.cfg_snapshot),
                    ssl_ctx,
                )
                who = vault.login()
                n = vault.get_account_count()
                vault_stock_n = n  # track locally to avoid a round-trip per account
                msg = f"Vault OK - {who}" + (f" - {n} stock" if n >= 0 else "")
                self.log(msg, "ok")
                logger.debug(f"vault OK who={who} stock={n}")
                if n >= 0:
                    self.stats_cb(self.stats, f"stock:{n}")
            except Exception as e:
                logger.debug(f"vault FAIL: {e}")
                self.log(f"Vault failed (local only): {e}", "warn")
                vault = None

        self.log(
            f"Type={account_type} CD={cooldown:.0f}s limit={daily_limit}/key/day "
            f"keys={len(keys)} "f"vault={'on' if vault else 'off'} ssl={'ok' if ssl_verify else 'off'} "
            f"{'(DRY RUN)' if dry_run else ''}",
            "info",
        )

        if dry_run:
            logger.debug("dry_run — calling _run_dry")
            self._run_dry(keys, account_type, cooldown, daily_limit, vault)
            logger.debug("_run_dry complete")
            return self.stats
        logger.debug(f"main loop START — {len(keys)} keys cooldown={cooldown}")
        key_i = 0
        _consec_empty = 0  # round-robin index

        while not self.should_stop():
            if self._wait_unpause():
                break

            if target > 0 and self.stats.done >= target:
                self.log(f"Target {target} reached", "ok")
                break

            # check day rollover
            if self.usage.get("day") != _today_key():
                self.usage["day"] = _today_key()
                self.usage["keys"] = {}
                save_usage(self.usage)
                with _lock_usage_flush:
                    _last_usage_flush = time.time()  # reset throttle after day rollover
                self.log("Daily counters reset (midnight UTC)", "muted")

            # pick next ready key (round-robin among keys not dead, not at-limit, and past cooldown)
            now = time.time()
            usable = [
                (i, k) for i, k in enumerate(keys)
                if k not in dead_keys and get_key_usage(self.usage, k) < daily_limit
            ]
            if not usable:
                dead_count = len(dead_keys)
                msg = "All keys exhausted: "
                if dead_count > 0:
                    msg += f"{dead_count} dead, "
                msg += f"{n_keys} keys at daily limit ({daily_limit})"
                self.log(msg, "error")
                break

            ready_set.clear()
            ready = []
            for pair in usable:
                if now >= key_ready.get(pair[1], 0):
                    ready.append(pair)
                    ready_set.add(pair)
            if not ready:
                soon = min(key_ready.get(k, 0) for _, k in usable)
                wait = max(0.0, soon - time.time())
                if wait >= 1.0:
                    self.log(f"All keys on cooldown, waiting {wait:.1f}s...", "muted")
                self._gap(wait if wait > 0 else 0.05)
                continue

            picked_idx, picked_key = None, None
            for offset in range(n_keys):
                idx = (key_i + offset) % n_keys
                key = keys[idx]
                if (idx, key) in ready_set:
                    picked_idx = idx
                    picked_key = key
                    key_i = idx + 1
                    break
            if picked_key is None:
                picked_idx, picked_key = ready[0]
                key_i = picked_idx + 1
            api_num = picked_idx + 1
            used_before = get_key_usage(self.usage, picked_key)
            self.key_cb(api_num, "busy", f"{used_before}/{daily_limit}")
            try:
                result = bloxgen_generate(picked_key, account_type, ssl_ctx)
            except BloxgenError as e:
                cat = e.category
                if cat == "rate_limit":
                    self.key_cb(api_num, "warn", "rate")
                    _consec_empty = 0  # rate limit ≠ empty stock
                    continue

                self.stats.fails += 1
                if cat == "stock_empty":
                    self.stats.stock_empty += 1
                    # quiet — roster shows "retrying", no feed spam
                    self.key_cb(api_num, "warn", "retrying")
                    _consec_empty += 1
                    _stop_on_empty = int(self.cfg_snapshot.get("consecutive_empty_stop") or 0)
                    if _stop_on_empty and _consec_empty >= _stop_on_empty:
                        self.log(f"Stock empty {_consec_empty}× in a row — stopping", "warn")
                        break
                elif cat == "auth":
                    dead_keys.add(picked_key)
                    key_ready.pop(picked_key, None)  # remove from cooldown tracker
                    self.key_cb(api_num, "error", "dead")
                    self.log(f"API{api_num} auth fail - disabled for session", "error")
                    _consec_empty = 0
                elif cat == "server":
                    self.key_cb(api_num, "warn", "5xx")
                    self.log(f"API{api_num} server error: {e}", "warn")
                    _consec_empty = 0
                else:
                    self.key_cb(api_num, "error", "fail")
                    self.log(f"API{api_num} gen fail: {e}", "error")

                self.stats_cb(self.stats, "gen fail")
                if cat == "stock_empty":
                    self._gap(0.1)
                continue
            except Exception as e:
                self.stats.fails += 1
                self.key_cb(api_num, "error", "fail")
                self.log(f"API{api_num} unexpected error: {e}", "error")
                self.stats_cb(self.stats, "gen fail")
                continue

            # BUMP USAGE ONLY ON SUCCESS
            new_count = bump_key_usage(self.usage, picked_key)
            self.usage_cb(picked_key, new_count)
            key_ready[picked_key] = time.time() + cooldown

            user = result["username"]
            old_pw = result["password"]
            cookie = result.get("cookie")
            age_label = result.get("age") or "?"
            final_pw = old_pw
            pw_changed = False
            logger.debug(f"generate OK user={user}")
            _consec_empty = 0  # reset consecutive empty counter on success

            new_password = (self.cfg_snapshot.get("new_password") or "").strip()
            if len(new_password) > 0 and len(new_password) < 8:
                self.log(f"PW skip {user}: password too short ({len(new_password)} chars, need 8+)", "warn")
                new_password = ""
            if new_password and not cookie:
                # No cookie supplied by Bloxgen for this account. A fresh username/password
                # login is not attempted here: Roblox challenges (Arkose/FunCaptcha)
                # programmatic logins from datacenter IPs, so it would very likely fail
                # anyway — better to skip fast with a clear reason than burn a slow,
                # near-certain-failure login attempt on every no-cookie account.
                self.log(f"PW skip {user}: no cookie supplied", "warn")

            if new_password and cookie:
                logger.debug(f"pw change for {user}")
                try:
                    ok, reason = provider_change_password(cookie, old_pw, new_password, ssl_ctx)
                    logger.debug(f"pw change ok={ok} reason={reason}")
                    # If Railway's own IP got the cookie rejected on auth grounds, retry the
                    # SAME cookie through the residential proxy before giving up. This is not
                    # a fresh login (which would introduce a new IP context and risk a
                    # csrf-rejected mismatch) — it's the identical request, just a cleaner
                    # exit IP, in case Roblox specifically distrusts Railway's address rather
                    # than the cookie itself being stale.
                    if not ok and reason and PROXY_URL and ("9002" in reason or "authenticat" in reason.lower()):
                        logger.debug(f"pw change auth failure — retrying via proxy for {user}")
                        ok2, reason2 = provider_change_password(cookie, old_pw, new_password, ssl_ctx, use_proxy=True)
                        logger.debug(f"pw change proxy retry ok={ok2} reason={reason2}")
                        if ok2:
                            ok, reason = ok2, reason2
                            self.log(f"PW retry OK: {user} (via proxy)", "muted")
                        else:
                            reason = f"{reason} | proxy retry: {reason2}"
                    if ok:
                        final_pw = new_password
                        pw_changed = True
                        self.log(f"PW changed: {user}", "ok")
                    else:
                        self.log(f"PW change failed: {user} ({reason})", "warn")
                except Exception as exc:
                    logger.debug(f"pw change EXCEPTION: {exc}")
                    self.log(f"PW change error: {exc}", "warn")

            self.key_cb(api_num, "ok", f"{new_count}/{daily_limit}")

            already_local = is_duplicate(user)
            if already_local:
                self.log(f"DUP skip {user} - already in accounts", "muted")

            vault_pushed = False
            if vault and not already_local:
                logger.debug(f"vault.add_account for {user}")
                try:
                    pushed = vault.add_account(user, final_pw, idempotent=False)  # dup already checked locally
                    logger.debug(f"vault.add_account pushed={pushed}")
                    vault_pushed = pushed
                    if pushed:
                        if vault_stock_n >= 0:
                            vault_stock_n += 1
                        self.log(f"Vault + {user}" + (f" - stock {vault_stock_n}" if vault_stock_n >= 0 else ""), "muted")
                        if vault_stock_n >= 0:
                            self.stats_cb(self.stats, f"stock:{vault_stock_n}")
                    else:
                        self.log(f"Vault skip {user} - already exists", "muted")
                except Exception as e:
                    logger.debug(f"vault.add_account EXCEPTION: {e}")
                    self.log(f"Vault add fail {user}: {e}", "warn")

            logger.debug(f"account_cb for {user}")
            api_tail = key_tail(picked_key)
            self.account_cb(
                user, final_pw, old_pw, age_label, account_type,
                pw_changed, vault_pushed, api_tail,
            )
            logger.debug("account_cb done")

            self.stats.done += 1
            self.stats_cb(self.stats, "running")
            logger.debug(f"stats done={self.stats.done} — key API{api_num} ready in {cooldown:.0f}s")
            # No gap here — key_ready[picked_key] already set to time.time()+cooldown above.
            # The loop top picks the next ready key immediately, or waits only as long as needed.
            self._gap(0.05)  # tiny yield so UI pump can drain between generates

        logger.debug(f"main loop EXIT stopped={self.should_stop()}")
        self.stats.stopped_manually = self.should_stop()
        self.stats_cb(self.stats, "stopped")
        return self.stats

    def _run_dry(
        self,
        keys: List[str],
        account_type: str,
        cooldown: float,
        daily_limit: int,
        vault: Optional[Vault],
    ) -> None:
        """Dry-run: validate keys, vault, show planned pacing without generating."""
        logger.debug(f"_run_dry START keys={len(keys)} type={account_type}")
        ssl_ctx = make_ssl_context(bool(self.cfg_snapshot.get("ssl_verify", True)))
        self.log("=== DRY RUN ===", "ok")
        self.log(f"Type: {account_type} | CD: {cooldown:.0f}s | Limit: {daily_limit}/key/day", "info")
        self.log(f"Checking {len(keys)} keys against Bloxgen API...", "info")

        def _check_key(i_key):
            i, key = i_key
            short = key_short(key)
            try:
                code, body, _ = _http(
                    "GET",
                    BLOXGEN_DAILY,
                    headers={"X-API-Key": key, "Accept": "application/json"},
                    ssl_ctx=ssl_ctx,
                    timeout=10.0,
                )
                if code in (401, 403):
                    return (i, short, "error", f"DEAD (HTTP {code})")
                if code == 429:
                    return (i, short, "warn", f"rate-limited (alive, HTTP 429)")
                if code == 0:
                    return (i, short, "warn", f"no response (timeout/network): {body}")
                return (i, short, "ok", f"OK (HTTP {code})")
            except Exception as e:
                return (i, short, "warn", f"exception: {e}")

        results = []
        try:
            with ThreadPoolExecutor(max_workers=min(len(keys), 6)) as pool:
                futs = {pool.submit(_check_key, item): item for item in enumerate(keys, start=1)}
                for fut in as_completed(futs, timeout=30):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        self.log(f"key future error: {e}", "warn")
        except Exception as e:
            self.log(f"key check pool error: {e}", "error")

        self.log(f"Key check complete — {len(results)}/{len(keys)} responded", "info")
        for i, short, level, msg in sorted(results, key=lambda x: x[0]):
            self.log(f"  API{i} {short}: {msg}", level)

        if self.cfg_snapshot.get("vault_enabled"):
            try:
                v = Vault(
                    str(self.cfg_snapshot["vault_api"]),
                    str(self.cfg_snapshot["vault_user"]),
                    _secure_load("vault_pass", self.cfg_snapshot),
                    ssl_ctx,
                )
                who = v.login()
                n = v.get_account_count()
                self.log(f"Vault: {who} — {n} stock" if n >= 0 else f"Vault: {who} connected", "ok")
            except Exception as e:
                self.log(f"Vault: {e}", "warn")

        if keys:
            gap = cooldown / max(len(keys), 1)
            rate = 60.0 / gap if gap > 0 else 0
            limit_total = daily_limit * len(keys)
            self.log(f"Est. throughput: {rate:.1f}/min | Max today: {limit_total}", "muted")

        self.log("=== DRY RUN COMPLETE ===", "ok")
        logger.debug("_run_dry COMPLETE")

# Precompiled log-sanitize patterns — applied once per message via _sanitize_log()
_RE_ROBLOSEC = re.compile(
    r'\.[Rr][Oo][Bb][Ll][Oo][Ss][Ee][Cc][Uu][Rr][Ii][Tt][Yy]=[^;\s]{20,}'
)
_RE_BLOX_KEY = re.compile(r'BLOX-[A-Za-z0-9]+', re.IGNORECASE)
# Only mask hex-like or base64-like tokens (high entropy) — not plain usernames
_RE_LONG_TOK = re.compile(r'([A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})')

def _sanitize_log(msg: str) -> str:
    """Strip cookies, Bloxgen keys, and long tokens from log output."""
    msg = _RE_ROBLOSEC.sub('.ROBLOSECURITY=****', msg)
    msg = _RE_BLOX_KEY.sub(lambda m: key_short(m.group(0)), msg)
    # Only scan for long tokens if the message is long enough to contain one
    if len(msg) >= 40:
        msg = _RE_LONG_TOK.sub(lambda m: _safe_secret(m.group(1), 4), msg)
    return msg

def _send_webhook(url: str, content: str) -> None:
    """Fire-and-forget Discord webhook POST."""
    if not url or not url.startswith("https://discord"):
        return
    try:
        _http("POST", url, body={"content": content}, timeout=8.0)
    except Exception:
        pass

_last_log_ts: str = ""
_last_log_t: int = 0

def _log_timestamp() -> str:
    global _last_log_ts, _last_log_t
    t = int(time.time())
    if t != _last_log_t:
        _last_log_ts = time.strftime("%H:%M:%S", time.localtime(t))
        _last_log_t = t
    return _last_log_ts

C = {
    "bg":        "#080a0f",   # deepest — window background
    "panel":     "#0f1218",   # header, tab container
    "card":      "#141920",   # tab content panels
    "card2":     "#1a2030",   # nested cards (lim_card, key rows)
    "border":    "#1e2535",   # disabled buttons, dividers
    "border2":   "#2a3347",   # subtle separators
    "text":      "#eef1f7",   # primary text
    "muted":     "#7a8799",   # labels, secondary text
    "dim":       "#4a566a",   # placeholders, very secondary
    "accent":    "#4f80e1",   # brand blue
    "accent2":   "#2d5cc4",   # darker blue for hover
    "ok":        "#2ec27e",   # green — success
    "ok_dim":    "#1a4a33",   # green dim — stat box bg
    "warn":      "#e8951a",   # amber — warning
    "warn_dim":  "#3d2a08",   # amber dim — stat box bg
    "error":     "#e8294a",   # red — error
    "error_dim": "#3d0f1a",   # red dim — stat box bg
    "input":     "#0b0e14",   # entry/textbox backgrounds
    "input2":    "#10141c",   # slightly lighter input variant
    "tag_ok":    "#1c3d2c",   # badge background — ok state
    "tag_warn":  "#3a2208",   # badge background — warn state
    "tag_err":   "#3a0e18",   # badge background — error state
}

STOCK_POLL_SEC = 90
LIMITS_POLL_SEC = 120  # live Bloxgen daily-limit / balance refresh
CLIPBOARD_CLEAR_SEC = 30  # auto-clear clipboard after N seconds

# ========== DPAPI helper (Windows) ==========
def _dpapi_encrypt(plain: str) -> Optional[bytes]:
    """Encrypt with Windows DPAPI if available. Returns base64 bytes or None."""
    if os.name != "nt":
        return None
    try:
        # Escape single quotes so an apostrophe in the password doesn't break the PS string
        safe_plain = plain.replace("'", "''")
        p = _run_nowin(
            ["powershell", "-NoProfile", "-Command",
             f"[Convert]::ToBase64String([System.Security.Cryptography.ProtectedData]::Protect("
             f"[System.Text.Encoding]::UTF8.GetBytes('{safe_plain}'), $null, 'CurrentUser'))"],
            capture_output=True, timeout=10, text=True,
        )
        out = p.stdout.strip()
        if out and p.returncode == 0:
            return out.encode()
    except Exception:
        pass
    return None

def _dpapi_decrypt(encrypted: bytes) -> Optional[str]:
    """Decrypt with Windows DPAPI. Returns plaintext or None."""
    if os.name != "nt":
        return None
    try:
        b64 = encrypted.decode("utf-8", errors="replace").strip()
        safe_b64 = b64.replace("'", "''")
        p = _run_nowin(
            ["powershell", "-NoProfile", "-Command",
             f"[System.Text.Encoding]::UTF8.GetString([System.Security.Cryptography.ProtectedData]::Unprotect("
             f"[Convert]::FromBase64String('{safe_b64}'), $null, 'CurrentUser'))"],
            capture_output=True, timeout=10, text=True,
        )
        out = p.stdout.strip()
        if out and p.returncode == 0:
            return out
    except Exception:
        pass
    return None

def _secure_load(key: str, cfg: Dict[str, Any]) -> str:
    """Load a secret - try DPAPI first, then plain."""
    dp_key = "_dpapi_" + key
    if dp_key in cfg and cfg.get(key) == "******":
        dec = _dpapi_decrypt(cfg[dp_key].encode("utf-8", errors="replace"))
        if dec is not None:
            return dec
    return str(cfg.get(key, ""))

# ========== secret truncation for UI ==========
def _safe_secret(value: str, show: int = 0) -> str:
    """Truncate secrets for display: show only last `show` chars, rest masked."""
    if not value:
        return ""
    v = str(value)
    if len(v) <= show + 4:
        return v
    return "*" * (len(v) - show) + v[-show:]

# ========== helpers ==========
def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m}m"

# ====================================================================
#  DeltaCoreApp
# ====================================================================
