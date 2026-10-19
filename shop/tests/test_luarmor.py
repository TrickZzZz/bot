"""Tests for app/luarmor.py against a fake Luarmor server running on localhost.

Run from the shop folder:  python3 -m unittest discover -s tests -v
Nothing here talks to the real Luarmor.
"""
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.luarmor import Luarmor, LuarmorError  # noqa: E402

API_KEY = "test-api-key-DO-NOT-LEAK-12345"
PROJECT = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
GOOD_KEY = "awIUiHenZzfScOsqkXwGHyRAOsTTcPMR"


class FakeLuarmor:
    """Records every request and answers from `script`, a function (request) -> (status, body, headers)."""

    def __init__(self):
        self.requests = []
        self.script = lambda req: (200, {"success": True, "message": "Success!", "user_key": GOOD_KEY}, {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                req = {"method": self.command, "path": self.path, "headers": dict(self.headers),
                       "body": json.loads(raw) if raw else None}
                owner.requests.append(req)
                status, body, headers = owner.script(req)
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass    # the client gave up first (the timeout tests do this on purpose)

            do_GET = do_POST = do_DELETE = _handle

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v3"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LuarmorClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLuarmor()
        self.addCleanup(self.fake.close)
        self.client = Luarmor(API_KEY, PROJECT, self.fake.url, timeout=2)

    # ── creating a key ─────────────────────────────────────────────────────
    def test_create_key_sends_the_right_request_and_returns_the_key(self):
        key = self.client.create_key(7, "shop-week-AbCdEf1234")
        self.assertEqual(key, GOOD_KEY)
        self.assertEqual(len(self.fake.requests), 1)
        req = self.fake.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], f"/v3/projects/{PROJECT}/users")
        self.assertEqual(req["headers"]["Authorization"], API_KEY)
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertEqual(req["body"], {"key_days": 7, "note": "shop-week-AbCdEf1234"})

    def test_user_agent_is_not_pythons_default(self):
        # Cloudflare-style firewalls commonly block "Python-urllib/3.x".
        self.client.create_key(1, "n")
        agent = self.fake.requests[0]["headers"]["User-Agent"]
        self.assertNotIn("urllib", agent.lower())
        self.assertTrue(agent)

    def test_each_plan_length_reaches_luarmor_as_key_days(self):
        for days in (1, 7, 30):
            self.client.create_key(days, "n")
        self.assertEqual([r["body"]["key_days"] for r in self.fake.requests], [1, 7, 30])

    def test_note_is_cut_to_100_characters(self):
        self.client.create_key(7, "x" * 500)
        self.assertEqual(len(self.fake.requests[0]["body"]["note"]), 100)

    def test_bad_day_counts_are_refused_without_calling_luarmor(self):
        for bad in (0, -1, 3651, True, "7", 7.0, None):
            with self.subTest(days=bad):
                with self.assertRaises(LuarmorError):
                    self.client.create_key(bad, "n")
        self.assertEqual(self.fake.requests, [])

    def test_unconfigured_client_refuses_without_calling_luarmor(self):
        for api_key, project in (("", PROJECT), (API_KEY, ""), ("  ", PROJECT),
                                 (API_KEY, "a/b"), (API_KEY, "../x"), (API_KEY, "a b"), (API_KEY, "a" * 65)):
            with self.subTest(api_key=bool(api_key.strip()), project=project):
                c = Luarmor(api_key, project, self.fake.url)
                self.assertFalse(c.configured)
                with self.assertRaises(LuarmorError):
                    c.create_key(7, "n")
        self.assertEqual(self.fake.requests, [])

    def test_configured_when_both_present(self):
        self.assertTrue(self.client.configured)

    # ── Luarmor saying no, or saying something odd ─────────────────────────
    def assert_create_fails(self, status, body, headers=None, contains=None):
        self.fake.script = lambda req: (status, body, headers or {})
        with self.assertRaises(LuarmorError) as ctx:
            self.client.create_key(7, "n")
        self.assertNotIn(API_KEY, str(ctx.exception))
        if contains:
            self.assertIn(contains, str(ctx.exception))
        return ctx.exception

    def test_wrong_api_key_401_reports_the_message_and_status(self):
        err = self.assert_create_fails(401, {"success": False, "message": "Invalid API key"},
                                       contains="Invalid API key")
        self.assertEqual(err.status, 401)
        self.assertIn("401", str(err))

    def test_ip_not_whitelisted_403_html_page(self):
        err = self.assert_create_fails(403, b"<html><body>Access denied. Your IP is not whitelisted</body></html>",
                                       {"Content-Type": "text/html"}, contains="403")
        self.assertIn("whitelisted", str(err))

    def test_server_error_500(self):
        self.assert_create_fails(500, b"Internal Server Error", contains="500")

    def test_rate_limited_429(self):
        err = self.assert_create_fails(429, {"success": False, "message": "slow down"}, contains="slow down")
        self.assertEqual(err.status, 429)

    def test_success_false_with_http_200(self):
        self.assert_create_fails(200, {"success": False, "message": "Project is locked"},
                                 contains="Project is locked")

    def test_success_missing_is_not_success(self):
        self.assert_create_fails(200, {"user_key": GOOD_KEY})

    def test_reply_that_is_not_json(self):
        self.assert_create_fails(200, b"<html>maintenance</html>", contains="isn't JSON")

    def test_reply_that_is_a_json_list(self):
        self.assert_create_fails(200, [GOOD_KEY], contains="unexpected reply")

    def test_success_without_a_key(self):
        self.assert_create_fails(200, {"success": True, "message": "ok"}, contains="usable")

    def test_unusable_keys_are_never_stored(self):
        for bad in ("", "short", None, 12345, "has space in the middle of this key 123", "x" * 200,
                    "<script>alert(1)</script>", "key\nwith\nnewlines" * 3):
            with self.subTest(user_key=bad):
                self.assert_create_fails(200, {"success": True, "user_key": bad})

    def test_redirect_is_an_error_and_is_not_followed(self):
        target = FakeLuarmor()
        self.addCleanup(target.close)
        self.fake.script = lambda req: (302, b"", {"Location": target.url + "/elsewhere"})
        with self.assertRaises(LuarmorError) as ctx:
            self.client.create_key(7, "n")
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(target.requests, [])

    def test_slow_luarmor_times_out_as_an_error(self):
        self.fake.script = lambda req: (time.sleep(1.5), (200, {"success": True, "user_key": GOOD_KEY}, {}))[1]
        slow = Luarmor(API_KEY, PROJECT, self.fake.url, timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(LuarmorError) as ctx:
            slow.create_key(7, "n")
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertIn("reach", str(ctx.exception))

    def test_unreachable_luarmor_is_an_error(self):
        dead = Luarmor(API_KEY, PROJECT, "http://127.0.0.1:1/v3", timeout=1)
        with self.assertRaises(LuarmorError) as ctx:
            dead.create_key(7, "n")
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_trailing_slash_on_the_url_is_fine(self):
        c = Luarmor(API_KEY, PROJECT, self.fake.url + "/", timeout=2)
        c.create_key(7, "n")
        self.assertEqual(self.fake.requests[0]["path"], f"/v3/projects/{PROJECT}/users")

    # ── blacklisting ───────────────────────────────────────────────────────
    def test_blacklist_sends_key_and_reason(self):
        self.fake.script = lambda req: (200, {"success": True, "message": "Blacklisted"}, {})
        self.client.blacklist_key(GOOD_KEY, "Payment dispute")
        req = self.fake.requests[0]
        self.assertEqual((req["method"], req["path"]), ("POST", f"/v3/projects/{PROJECT}/users/blacklist"))
        self.assertEqual(req["headers"]["Authorization"], API_KEY)
        self.assertEqual(req["body"], {"user_key": GOOD_KEY, "ban_reason": "Payment dispute"})
        self.assertNotIn("ban_expire", req["body"])     # left out = banned for good

    def test_blacklist_accepts_an_empty_200(self):
        self.fake.script = lambda req: (200, b"", {})
        self.client.blacklist_key(GOOD_KEY, "r")

    def test_blacklist_refusals_raise(self):
        for status, body in ((200, {"success": False, "message": "no such key"}),
                             (404, {"success": False, "message": "Key not found"}),
                             (500, b"boom"), (200, b"not json")):
            with self.subTest(status=status):
                self.fake.script = lambda req, s=status, b=body: (s, b, {})
                with self.assertRaises(LuarmorError):
                    self.client.blacklist_key(GOOD_KEY, "r")

    # ── deleting ───────────────────────────────────────────────────────────
    def test_delete_sends_the_key_in_the_query(self):
        self.fake.script = lambda req: (200, {"success": True, "message": "User has been deleted!"}, {})
        self.client.delete_key(GOOD_KEY)
        req = self.fake.requests[0]
        self.assertEqual((req["method"], req["path"]),
                         ("DELETE", f"/v3/projects/{PROJECT}/users?user_key={GOOD_KEY}"))

    def test_delete_of_a_missing_key_counts_as_gone(self):
        self.fake.script = lambda req: (404, {"success": False, "message": "Key not found"}, {})
        self.client.delete_key(GOOD_KEY)

    def test_delete_failures_raise(self):
        self.fake.script = lambda req: (500, b"boom", {})
        with self.assertRaises(LuarmorError):
            self.client.delete_key(GOOD_KEY)


if __name__ == "__main__":
    unittest.main()
