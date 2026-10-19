"""Luarmor API client (standard library only, so it adds no dependency).

The shop uses it to make the licence key a buyer pays for, and to cancel that key if
the payment is disputed. Every call needs the Luarmor API key and the project id, and
Luarmor only accepts calls from IP addresses whitelisted in its dashboard.

Docs: https://docs.luarmor.net/  (POST /v3/projects/:project_id/users and friends).
"""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("shop.luarmor")

DEFAULT_API_URL = "https://api.luarmor.net/v3"
PROJECT_ID_RE   = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KEY_RE          = re.compile(r"^[A-Za-z0-9_-]{16,64}$")   # Luarmor keys are 32 letters and digits
USER_AGENT      = "tracker-shop/1.0"
MAX_REPLY_BYTES = 65536


class LuarmorError(Exception):
    """Luarmor refused the call, couldn't be reached, or answered with something unusable.
    The message never contains the API key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirected POST would silently turn into a GET, so a redirect is an error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _snippet(raw: bytes, limit: int = 300) -> str:
    return raw[:limit].decode("utf-8", "replace").replace("\n", " ").strip()


class Luarmor:
    def __init__(self, api_key: str, project_id: str, api_url: str = DEFAULT_API_URL,
                 timeout: float = 15.0):
        self.api_key    = (api_key or "").strip()
        self.project_id = (project_id or "").strip()
        self.api_url    = (api_url or DEFAULT_API_URL).strip().rstrip("/")
        self.timeout    = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(PROJECT_ID_RE.match(self.project_id))

    def _call(self, method: str, path: str, body: dict | None = None,
              query: dict | None = None) -> dict:
        """One request. Returns the decoded JSON object. A reply that isn't a JSON object is
        an error, except an empty 200 which counts as {} (some endpoints send no body)."""
        if not self.configured:
            raise LuarmorError("Luarmor isn't configured (LUARMOR_API_KEY / LUARMOR_PROJECT_ID)")
        url = f"{self.api_url}/projects/{self.project_id}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": self.api_key, "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(MAX_REPLY_BYTES)
        except urllib.error.HTTPError as e:
            raw = e.read(MAX_REPLY_BYTES) if hasattr(e, "read") else b""
            detail = _snippet(raw)
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("message"):
                    detail = str(parsed["message"])[:300]
            except ValueError:
                pass
            err = LuarmorError(f"Luarmor answered HTTP {e.code} for {method} {path}: {detail}")
            err.status = e.code
            raise err from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LuarmorError(f"Couldn't reach Luarmor for {method} {path}: {e}") from None

        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise LuarmorError(f"Luarmor sent something that isn't JSON for {method} {path}: "
                               f"{_snippet(raw)}") from None
        if not isinstance(parsed, dict):
            raise LuarmorError(f"Luarmor sent an unexpected reply for {method} {path}: {_snippet(raw)}")
        return parsed

    def create_key(self, days: int, note: str) -> str:
        """Make a key that lasts `days` days from the first time it is redeemed or run
        (Luarmor's key_days), and return it."""
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise LuarmorError(f"Refusing to create a key for {days!r} days")
        reply = self._call("POST", "users", {"key_days": days, "note": str(note)[:100]})
        if reply.get("success") is not True:
            raise LuarmorError(f"Luarmor wouldn't create the key: {reply.get('message') or reply}")
        key = reply.get("user_key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise LuarmorError("Luarmor said it made a key but didn't send a usable one")
        return key

    def blacklist_key(self, key: str, reason: str) -> None:
        """Ban a key for good. Raises LuarmorError unless Luarmor accepts it."""
        reply = self._call("POST", "users/blacklist",
                           {"user_key": key, "ban_reason": str(reason)[:100]})
        if reply.get("success") is False:
            raise LuarmorError(f"Luarmor wouldn't blacklist the key: {reply.get('message') or reply}")

    def delete_key(self, key: str) -> None:
        """Remove a key. A key Luarmor doesn't have (404) counts as already gone."""
        try:
            reply = self._call("DELETE", "users", query={"user_key": key})
        except LuarmorError as e:
            if getattr(e, "status", None) == 404:
                return
            raise
        if reply.get("success") is False:
            raise LuarmorError(f"Luarmor wouldn't delete the key: {reply.get('message') or reply}")
