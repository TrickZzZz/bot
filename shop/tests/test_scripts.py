"""Tests for hosted scripts: the unlisted /l/<token> link, the /admin page and the upload routes
(app/main.py). Same stand-ins as test_shop.py (see stubs.py); nothing here touches a real database.

Run from the shop folder:  python3 -m unittest discover -s tests -v
"""
import asyncio
import base64
import hashlib
import html
import json
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from test_shop import ShopCase, main, stubs  # noqa: E402

PASSWORD = "correct-horse-battery-staple-0123456789"      # 39 characters
WRONG = "wrong-password-wrong-password-1"
SCRIPT = "print('hello from V4')\n"
LINK_RE = re.compile(r'/l/([A-Za-z0-9_-]+)"\)\)\(\)$')
STAGE_RE = re.compile(r'local BASE, TOKEN, NONCE, SECRET = "([^"]*)", "([^"]*)", "([^"]*)", "([^"]*)"')
APP_DIR = HERE.parent / "app"


def basic(password, user="anyone"):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def bearer(password):
    return "Bearer " + password


class ScriptCase(ShopCase):
    def setUp(self):
        super().setUp()
        with main.engine.begin() as conn:
            conn.execute("DELETE FROM hosted_scripts")
        self.patch(main, "SCRIPT_ADMIN_TOKEN", PASSWORD)
        self.patch(main, "SITE_URL", "https://shop.example")

    # ── requests ───────────────────────────────────────────────────────────
    def call(self, route, path, method="GET", body=b"", auth=None, headers=None,
             ip="203.0.113.9", query=b"", **route_args):
        sent = dict(headers or {})
        if auth:
            sent["authorization"] = auth
        request = stubs.make_request(path, method, query=query, body=body, headers=sent, client=(ip, 5000))
        return asyncio.run(route(request, **route_args))

    def upload(self, name, body=SCRIPT, auth=None, headers=None, ip="203.0.113.9"):
        data = body if isinstance(body, bytes) else body.encode()
        return self.call(main.admin_upload, f"/admin/scripts/{name}", "PUT", data,
                         auth=auth or bearer(PASSWORD), headers=headers, ip=ip, name=name)

    def rotate(self, name, auth=None, headers=None):
        return self.call(main.admin_rotate, f"/admin/scripts/{name}/rotate", "POST",
                         auth=auth or bearer(PASSWORD), headers=headers, name=name)

    def delete(self, name, auth=None, headers=None):
        return self.call(main.admin_delete, f"/admin/scripts/{name}", "DELETE",
                         auth=auth or bearer(PASSWORD), headers=headers, name=name)

    def page(self, auth=None, headers=None, ip="203.0.113.9"):
        return self.call(main.admin_page, "/admin", auth=auth or bearer(PASSWORD), headers=headers, ip=ip)

    def stage_one(self, token, ip="198.51.100.7", headers=None):
        """What /l/<token> answers: the loader."""
        return self.call(main.script_file, f"/l/{token}", ip=ip, headers=headers, token=token)

    def parse_stage(self, resp):
        """(base, token, nonce, secret) the loader carries."""
        found = STAGE_RE.search(resp.body.decode())
        self.assertIsNotNone(found, resp.body[:200])
        return found.groups()

    def payload(self, token, nonce, sig, ip="198.51.100.7", headers=None):
        return self.call(main.script_payload, f"/p/{token}", ip=ip, headers=headers,
                         query=f"n={nonce}&s={sig}".encode(), token=token)

    def fetch(self, token, ip="198.51.100.7", headers=None):
        """What an executor ends up with: the loader is fetched, its request is signed and sent and the
        payload is decrypted. A failure at either step comes back as it was answered."""
        first = self.stage_one(token, ip, headers)
        if first.status_code != 200:
            return first
        _base, _token, nonce, secret = self.parse_stage(first)
        second = self.payload(token, nonce, main.loader.sign(secret, token, nonce), ip, headers)
        if second.status_code != 200:
            return second
        plain = main.loader.decrypt(secret, nonce, second.body)
        self.assertIsNotNone(plain, "the payload did not decrypt")
        return types.SimpleNamespace(status_code=200, body=plain, headers=second.headers)

    def data(self, resp):
        return json.loads(resp.body)

    def token_of(self, line):
        return LINK_RE.search(line).group(1)

    def add(self, name="v4", body=SCRIPT):
        """Upload a script and return (token, the upload's JSON answer)."""
        resp = self.upload(name, body)
        self.assertEqual(resp.status_code, 200, resp.body)
        answer = self.data(resp)
        return self.token_of(answer["loadstring"]), answer

    def script_rows(self):
        with main.engine.begin() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM hosted_scripts ORDER BY id").fetchall()]

    def page_text(self):
        return html.unescape(self.page().body.decode())


# ═══════════════════════════════════════════════════════════════════════════
class ServingTests(ScriptCase):
    def test_the_uploaded_script_is_served_exactly_as_plain_text(self):
        token, _ = self.add("v4", SCRIPT)
        resp = self.fetch(token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.body, SCRIPT.encode())
        self.assertTrue(resp.headers["content-type"].startswith("text/plain"))
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(resp.headers["x-robots-tag"], "noindex")

    def test_nobody_needs_to_sign_in_to_run_it(self):
        token, _ = self.add()
        resp = self.fetch(token, ip="198.51.100.77")
        self.assertEqual((resp.status_code, resp.body), (200, SCRIPT.encode()))

    def test_a_large_script_with_windows_line_endings_and_non_ascii_text_comes_back_byte_for_byte(self):
        raw = ("-- big\r\n" + "local x = 1\r\n" * 60000 + "print('é✓')\n").encode()
        self.assertGreater(len(raw), 780_000)
        token, answer = self.add("big", raw)
        self.assertEqual(self.fetch(token).body, raw)
        self.assertEqual((answer["bytes"], answer["sha256"]), (len(raw), hashlib.sha256(raw).hexdigest()))
        row = self.script_rows()[0]          # bytes, not characters: the file has 3-byte and 2-byte characters
        self.assertEqual((row["size"], row["sha256"]), (len(raw), hashlib.sha256(raw).hexdigest()))
        self.assertNotEqual(len(raw), len(raw.decode()))

    def test_an_unknown_link_is_a_plain_404_not_a_shop_page(self):
        self.add()
        resp = self.fetch("A" * 32)
        self.assertEqual((resp.status_code, resp.body), (404, b"Not found"))
        self.assertNotIn(b"<html", resp.body)

    def test_a_link_in_the_wrong_shape_never_reaches_the_database(self):
        self.patch(main, "LINK_MISSES_PER_MIN", 100)         # the wrong-link limit has its own tests
        self.add()
        with mock.patch.object(main, "serve_script") as lookup:
            for bad in ("short", "x" * 65, "../etc/passwd", "a b c d e f g h i j k l", "%00" * 10, "é" * 20,
                        "A" * 32 + "\n"):
                with self.subTest(token=bad):
                    self.assertEqual(self.fetch(bad).status_code, 404)
            lookup.assert_not_called()
        self.assertEqual(self.script_rows()[0]["hits"], 0)

    def test_the_404_handler_keeps_hosted_links_plain_and_other_pages_branded(self):
        plain = asyncio.run(main.http_error(stubs.make_request("/l/abc"), StarletteHTTPException(404)))
        self.assertNotIn(b"<html", plain.body)
        branded = asyncio.run(main.http_error(stubs.make_request("/nope"), StarletteHTTPException(404)))
        self.assertIn(b"<html", branded.body)

    def test_each_open_is_counted_and_the_time_of_the_last_one_saved(self):
        token, _ = self.add()
        row = self.script_rows()[0]
        self.assertEqual((row["hits"], row["last_hit_at"]), (0, None))
        for _ in range(3):
            self.fetch(token)
        row = self.script_rows()[0]
        self.assertEqual(row["hits"], 3)
        self.assertIsNotNone(row["last_hit_at"])

    def test_one_address_is_slowed_after_too_many_opens_and_others_are_not(self):
        self.patch(main, "SCRIPT_FETCHES_PER_MIN", 3)
        token, _ = self.add()
        self.assertEqual([self.fetch(token, ip="198.51.100.1").status_code for _ in range(3)], [200] * 3)
        slow = self.fetch(token, ip="198.51.100.1")
        self.assertEqual(slow.status_code, 429)
        self.assertEqual(slow.headers["retry-after"], "60")
        self.assertEqual(self.fetch(token, ip="198.51.100.2").status_code, 200)

    def test_guessing_links_counts_toward_the_limit_too(self):
        self.patch(main, "SCRIPT_FETCHES_PER_MIN", 3)
        statuses = [self.fetch("B" * 32, ip="198.51.100.3").status_code for _ in range(5)]
        self.assertEqual(statuses, [404, 404, 404, 429, 429])


# ═══════════════════════════════════════════════════════════════════════════
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/130.0.0.0 Safari/537.36")
ACCEPT_PAGE = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"

# What real browsers send when a person opens the address (typed, clicked or pasted into a tab).
BROWSER_PAGE_LOADS = {
    "chrome tab": {"user-agent": UA_CHROME, "accept": ACCEPT_PAGE,
                   "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "none"},
    "firefox tab": {"user-agent": "Mozilla/5.0 (Windows NT 10.0; rv:131.0) Gecko/20100101 Firefox/131.0",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
    "older safari, no sec-fetch headers": {
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/15.6 Safari/605.1.15",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    "page inside a frame": {"user-agent": UA_CHROME, "accept": ACCEPT_PAGE,
                            "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"},
    "sec-fetch headers alone": {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
    "navigation mode alone": {"sec-fetch-mode": "navigate"},
    "document destination alone": {"sec-fetch-dest": "document"},
    "iframe destination alone": {"sec-fetch-dest": "iframe"},
    "frame destination alone": {"sec-fetch-dest": "frame"},
    "embed destination alone": {"sec-fetch-dest": "embed"},
    "object destination alone": {"sec-fetch-dest": "object"},
    "accept and user agent in capitals": {"user-agent": "MOZILLA/5.0 (X11)", "accept": "TEXT/HTML,*/*;q=0.8"},
    "navigation mode in capitals": {"sec-fetch-mode": "NAVIGATE"},
    "document destination in capitals": {"sec-fetch-dest": "DOCUMENT"},
}

# What a game executor's HttpGet (curl, Python, a script) sends: no page headers.
SCRIPT_CLIENTS = {
    "no headers at all": {},
    "accept anything": {"accept": "*/*"},
    "roblox user agent": {"user-agent": "Roblox/WinInet", "accept": "*/*"},
    "curl": {"user-agent": "curl/8.5.0", "accept": "*/*"},
    "python requests": {"user-agent": "python-requests/2.32.3", "accept-encoding": "gzip, deflate",
                        "accept": "*/*"},
    # An unknown executor must never be mistaken for a person: html in Accept alone is not a browser.
    "html accepted but not a browser": {"user-agent": "SomeExecutor/1.0", "accept": "text/html"},
    "html accepted and no user agent": {"accept": "text/html"},
    "browser user agent but not asking for a page": {"user-agent": UA_CHROME, "accept": "*/*"},
    "page script fetch()": {"user-agent": UA_CHROME, "accept": "*/*", "sec-fetch-mode": "cors",
                            "sec-fetch-dest": "empty", "sec-fetch-site": "cross-site"},
    "page <script src>": {"user-agent": UA_CHROME, "accept": "*/*", "sec-fetch-mode": "no-cors",
                          "sec-fetch-dest": "script"},
}


class BrowserVisitTests(ScriptCase):
    """A person who opens the link in a browser lands on the home page and never sees the script;
    the executor that runs the loadstring still gets it."""

    def test_a_browser_opening_the_link_is_sent_to_the_home_page(self):
        token, _ = self.add()
        for label, headers in BROWSER_PAGE_LOADS.items():
            with self.subTest(client=label):
                resp = self.fetch(token, headers=headers)
                self.assertEqual((resp.status_code, resp.headers["location"]), (302, "/"))
                self.assertEqual(resp.body, b"")
                self.assertNotIn(SCRIPT.strip().encode(), resp.body)

    def test_the_redirect_is_temporary_and_never_cached(self):
        token, _ = self.add()
        resp = self.fetch(token, headers=BROWSER_PAGE_LOADS["chrome tab"])
        self.assertEqual(resp.status_code, 302)           # not 301: a browser would remember it for good
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(resp.headers["x-robots-tag"], "noindex")

    def test_the_executor_and_other_scripts_still_get_the_script(self):
        token, _ = self.add()
        for label, headers in SCRIPT_CLIENTS.items():
            with self.subTest(client=label):
                resp = self.fetch(token, headers=headers)
                self.assertEqual((resp.status_code, resp.body), (200, SCRIPT.encode()))
                self.assertTrue(resp.headers["content-type"].startswith("text/plain"))

    def test_a_browser_gets_the_same_answer_for_a_real_link_and_a_made_up_one(self):
        token, _ = self.add()
        real = self.fetch(token, headers=BROWSER_PAGE_LOADS["chrome tab"])
        fake = self.fetch("Z" * 32, headers=BROWSER_PAGE_LOADS["chrome tab"])
        junk = self.fetch("short", headers=BROWSER_PAGE_LOADS["chrome tab"])
        for resp in (real, fake, junk):
            self.assertEqual((resp.status_code, resp.headers["location"]), (302, "/"))
        # while a script client still learns that a made-up link is wrong
        self.assertEqual(self.fetch("Z" * 32, headers=SCRIPT_CLIENTS["curl"]).status_code, 404)

    def test_a_browser_visit_is_not_counted_and_does_not_touch_the_database(self):
        token, _ = self.add()
        with mock.patch.object(main, "serve_script") as lookup:
            for _ in range(3):
                self.fetch(token, headers=BROWSER_PAGE_LOADS["chrome tab"])
            lookup.assert_not_called()
        row = self.script_rows()[0]
        self.assertEqual((row["hits"], row["last_hit_at"]), (0, None))
        self.fetch(token, headers=SCRIPT_CLIENTS["roblox user agent"])
        self.assertEqual(self.script_rows()[0]["hits"], 1)

    def test_browser_visits_neither_use_up_nor_are_stopped_by_the_fetch_limit(self):
        self.patch(main, "SCRIPT_FETCHES_PER_MIN", 2)
        token, _ = self.add()
        page = BROWSER_PAGE_LOADS["chrome tab"]
        ip = "198.51.100.40"
        self.assertEqual([self.fetch(token, ip=ip, headers=page).status_code for _ in range(5)], [302] * 5)
        self.assertEqual([self.fetch(token, ip=ip).status_code for _ in range(2)], [200, 200])   # quota untouched
        self.assertEqual(self.fetch(token, ip=ip).status_code, 429)                              # now spent
        self.assertEqual(self.fetch(token, ip=ip, headers=page).status_code, 302)                # still sent home

    def test_a_browser_is_sent_home_even_with_a_hosted_script_that_does_not_exist_yet(self):
        resp = self.fetch("Q" * 32, headers=BROWSER_PAGE_LOADS["firefox tab"])
        self.assertEqual((resp.status_code, resp.headers["location"]), (302, "/"))


# ═══════════════════════════════════════════════════════════════════════════
class UploadTests(ScriptCase):
    def test_a_new_name_gets_a_random_link_the_loadstring_points_at(self):
        resp = self.upload("v4")
        answer = self.data(resp)
        self.assertEqual((resp.status_code, answer["created"], answer["name"]), (200, True, "v4"))
        token = self.token_of(answer["loadstring"])
        self.assertEqual(answer["loadstring"], f'loadstring(game:HttpGet("https://shop.example/l/{token}"))()')
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(self.script_rows()[0]["token"], token)

    def test_the_site_address_comes_from_the_request_when_no_site_url_is_set(self):
        self.patch(main, "SITE_URL", "")
        line = self.data(self.upload("v4"))["loadstring"]
        self.assertTrue(line.startswith('loadstring(game:HttpGet("https://shop.test/l/'), line)

    def test_two_scripts_get_two_different_links_and_each_serves_its_own_text(self):
        a, _ = self.add("one", "print('one')")
        b, _ = self.add("two", "print('two')")
        self.assertNotEqual(a, b)
        self.assertEqual((self.fetch(a).body, self.fetch(b).body), (b"print('one')", b"print('two')"))

    def test_replacing_a_script_keeps_its_link_and_serves_the_new_text(self):
        token, _ = self.add("v4", "print('old')")
        again = self.data(self.upload("v4", "print('new')"))
        self.assertFalse(again["created"])
        self.assertEqual(self.token_of(again["loadstring"]), token)
        self.assertEqual(self.fetch(token).body, b"print('new')")
        self.assertEqual(len(self.script_rows()), 1)

    def test_replacing_keeps_the_count_of_opens(self):
        token, _ = self.add("v4", "print('old')")
        self.fetch(token)
        self.upload("v4", "print('new')")
        self.assertEqual(self.script_rows()[0]["hits"], 1)

    def test_size_and_hash_are_saved_for_the_uploaded_bytes(self):
        self.add("v4", SCRIPT)
        row = self.script_rows()[0]
        self.assertEqual((row["name"], row["size"], row["hits"]), ("v4", len(SCRIPT.encode()), 0))
        self.assertEqual(row["sha256"], hashlib.sha256(SCRIPT.encode()).hexdigest())
        self.assertEqual(row["body"], SCRIPT)

    def test_good_names_are_accepted(self):
        for name in ("v4", "my-script_2", "a", "0day", "x" * 32):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name).status_code, 200)

    def test_bad_names_are_refused_and_nothing_is_saved(self):
        for name in ("V4", "-v4", "_v4", "a b", "x" * 33, "v4.lua", "v4/../x", "é", "v4\n", ""):
            with self.subTest(name=name):
                resp = self.upload(name)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("error", self.data(resp))
        self.assertEqual(self.script_rows(), [])

    def test_empty_binary_and_non_utf8_files_are_refused(self):
        for label, body in (("empty", b""), ("blank", b" \n\t "), ("nul", b"print(1)\x00"),
                            ("latin-1", b"print('caf\xe9')")):
            with self.subTest(file=label):
                self.assertEqual(self.upload("v4", body).status_code, 400)
        self.assertEqual(self.script_rows(), [])

    def test_a_refused_replacement_leaves_the_old_script_alone(self):
        token, _ = self.add("v4", "print('keep me')")
        self.assertEqual(self.upload("v4", b"").status_code, 400)
        self.assertEqual(self.fetch(token).body, b"print('keep me')")

    def test_a_file_over_the_limit_is_refused_by_its_size_or_by_counting_what_arrives(self):
        self.patch(main, "SCRIPT_MAX_BYTES", 100)
        self.assertEqual(self.upload("v4", b"x" * 100).status_code, 200)
        self.assertEqual(self.upload("v5", b"x" * 101).status_code, 413)
        declared = self.upload("v6", b"", headers={"content-length": "5000"})
        self.assertEqual(declared.status_code, 413)
        junk = self.upload("v7", b"x", headers={"content-length": "not a number"})
        self.assertEqual(junk.status_code, 200)
        self.assertEqual({r["name"] for r in self.script_rows()}, {"v4", "v7"})

    def test_uploads_are_logged_without_the_link_or_the_script(self):
        with self.logged("INFO") as cm:
            token, _ = self.add("v4", "print('secret source')")
        out = "\n".join(cm.output)
        self.assertIn("v4", out)
        self.assertIn(hashlib.sha256(b"print('secret source')").hexdigest()[:12], out)
        self.assertNotIn(token, out)
        self.assertNotIn("secret source", out)


# ═══════════════════════════════════════════════════════════════════════════
class LinkManagementTests(ScriptCase):
    def test_a_new_link_replaces_the_old_one_at_once(self):
        old, _ = self.add()
        resp = self.rotate("v4")
        self.assertEqual(resp.status_code, 200)
        new = self.token_of(self.data(resp)["loadstring"])
        self.assertNotEqual(new, old)
        self.assertEqual(self.fetch(old).status_code, 404)
        self.assertEqual(self.fetch(new).body, SCRIPT.encode())

    def test_a_new_link_leaves_other_scripts_alone(self):
        keep, _ = self.add("keep", "print('keep')")
        self.add("move")
        self.rotate("move")
        self.assertEqual(self.fetch(keep).body, b"print('keep')")

    def test_removing_a_script_ends_its_link(self):
        token, _ = self.add()
        resp = self.delete("v4")
        self.assertEqual((resp.status_code, self.data(resp)["removed"]), (200, True))
        self.assertEqual(self.fetch(token).status_code, 404)
        self.assertEqual(self.script_rows(), [])

    def test_removing_one_script_leaves_the_others(self):
        keep, _ = self.add("keep", "print('keep')")
        self.add("drop")
        self.delete("drop")
        self.assertEqual(self.fetch(keep).body, b"print('keep')")

    def test_removing_or_renewing_a_name_that_does_not_exist_is_a_404(self):
        for name in ("nope", "BAD NAME", "v4\n"):
            with self.subTest(name=name):
                self.assertEqual(self.delete(name).status_code, 404)
                self.assertEqual(self.rotate(name).status_code, 404)

    def test_a_name_in_the_wrong_shape_never_reaches_the_database(self):
        with mock.patch.object(main, "rotate_script") as renew, mock.patch.object(main, "delete_script") as remove:
            for name in ("BAD NAME", "v4\n", "../x", "x" * 33):
                with self.subTest(name=name):
                    self.assertEqual(self.delete(name).status_code, 404)
                    self.assertEqual(self.rotate(name).status_code, 404)
            renew.assert_not_called()
            remove.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
class SignInTests(ScriptCase):
    def routes(self, auth=None, headers=None):
        """Every admin route called with the same credentials, as (label, function to run)."""
        return [("page", lambda: self.page(auth=auth, headers=headers)),
                ("upload", lambda: self.upload("v4", auth=auth, headers=headers)),
                ("rotate", lambda: self.rotate("v4", auth=auth, headers=headers)),
                ("delete", lambda: self.delete("v4", auth=auth, headers=headers))]

    def test_with_no_password_set_the_admin_routes_do_not_exist(self):
        for value in ("", "x" * 23):
            self.patch(main, "SCRIPT_ADMIN_TOKEN", value)
            for label, run in self.routes(auth=bearer(value or "anything")):
                with self.subTest(password_length=len(value), route=label):
                    with self.assertRaises(StarletteHTTPException) as ctx:
                        run()
                    self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.script_rows(), [])

    def test_a_password_of_24_characters_is_enough(self):
        value = "abcdefghijklmnopqrstuvwx"        # 24 different characters; test_hardening has the variety rule
        self.patch(main, "SCRIPT_ADMIN_TOKEN", value)
        self.assertEqual(self.page(auth=bearer(value)).status_code, 200)

    def test_no_password_gets_the_browsers_sign_in_box(self):
        calls = [("page", main.admin_page, "/admin", "GET", {}),
                 ("upload", main.admin_upload, "/admin/scripts/v4", "PUT", {"name": "v4"}),
                 ("rotate", main.admin_rotate, "/admin/scripts/v4/rotate", "POST", {"name": "v4"}),
                 ("delete", main.admin_delete, "/admin/scripts/v4", "DELETE", {"name": "v4"})]
        for label, route, path, method, args in calls:
            with self.subTest(route=label):
                resp = self.call(route, path, method, b"print(1)", **args)
                self.assertEqual(resp.status_code, 401)
                self.assertTrue(resp.headers["www-authenticate"].startswith("Basic realm="))
                self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(self.script_rows(), [])

    def test_a_wrong_password_is_refused_everywhere_and_changes_nothing(self):
        for auth in (basic(WRONG), bearer(WRONG)):
            for label, run in self.routes(auth=auth, headers={"x-admin-request": "1"}):
                with self.subTest(route=label, scheme=auth.split()[0]):
                    self.assertEqual(run().status_code, 401)
        self.assertEqual(self.script_rows(), [])

    def test_the_right_password_works_with_any_user_name_and_either_scheme(self):
        for auth in (basic(PASSWORD), basic(PASSWORD, user=""), basic(PASSWORD, user="someone else"),
                     bearer(PASSWORD), "bearer " + PASSWORD, "BASIC " + basic(PASSWORD).split(" ")[1]):
            with self.subTest(auth=auth.split()[0]):
                self.assertEqual(self.page(auth=auth).status_code, 200)

    def test_a_password_that_contains_colons_works_with_basic_sign_in(self):
        self.patch(main, "SCRIPT_ADMIN_TOKEN", "pass:with:colons-in-it-1234567890")
        self.assertEqual(self.page(auth=basic("pass:with:colons-in-it-1234567890")).status_code, 200)

    def test_garbled_headers_are_refused_without_a_crash(self):
        for auth in ("Basic !!!not-base64!!!", "Basic " + base64.b64encode(b"\xff\xfe:pw").decode(),
                     "Basic ", "Bearer ", "Bearer", "Digest abc", "garbage",
                     "Basic " + base64.b64encode(b"nocolon").decode()):
            with self.subTest(auth=auth):
                self.assertEqual(self.page(auth=auth).status_code, 401)

    def test_ten_wrong_passwords_lock_that_address_out_even_for_the_right_one(self):
        self.assertEqual([self.page(auth=basic(WRONG), ip="192.0.2.10").status_code for _ in range(10)], [401] * 10)
        locked = self.page(auth=basic(PASSWORD), ip="192.0.2.10")
        self.assertEqual((locked.status_code, locked.headers["retry-after"]), (429, "60"))
        self.assertEqual(self.page(auth=basic(WRONG), ip="192.0.2.10").status_code, 429)
        self.assertEqual(self.page(auth=basic(PASSWORD), ip="192.0.2.11").status_code, 200)

    def test_visits_without_a_password_and_right_passwords_never_count_toward_the_lockout(self):
        for _ in range(25):
            self.assertEqual(self.call(main.admin_page, "/admin", ip="192.0.2.20").status_code, 401)
        for _ in range(25):
            self.assertEqual(self.page(auth=basic(PASSWORD), ip="192.0.2.20").status_code, 200)

    def test_a_wrong_password_is_logged_without_either_password(self):
        with self.logged("WARNING") as cm:
            self.page(auth=basic(WRONG))
        out = "\n".join(cm.output)
        self.assertIn("203.0.113.9", out)
        self.assertNotIn("wrong-password", out)
        self.assertNotIn(PASSWORD, out)

    def test_changes_from_a_browser_sign_in_need_the_pages_own_header(self):
        for label, run in self.routes(auth=basic(PASSWORD)):
            if label != "page":
                with self.subTest(route=label):
                    self.assertEqual(run().status_code, 403)
        for value in ("0", "yes", ""):
            with self.subTest(header=value):
                resp = self.upload("v4", auth=basic(PASSWORD), headers={"x-admin-request": value})
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.script_rows(), [])
        header = {"x-admin-request": "1"}
        self.assertEqual(self.upload("v4", auth=basic(PASSWORD), headers=header).status_code, 200)
        self.assertEqual(self.rotate("v4", auth=basic(PASSWORD), headers=header).status_code, 200)
        self.assertEqual(self.delete("v4", auth=basic(PASSWORD), headers=header).status_code, 200)

    def test_viewing_the_page_with_a_browser_sign_in_needs_no_extra_header(self):
        self.assertEqual(self.page(auth=basic(PASSWORD)).status_code, 200)

    def test_a_script_using_the_bearer_password_needs_no_extra_header(self):
        self.assertEqual(self.upload("v4", auth=bearer(PASSWORD)).status_code, 200)

    def test_the_rate_limiter_can_look_without_counting(self):
        for _ in range(5):
            self.assertFalse(main.rate_limited("probe", 2, record=False))
        self.assertFalse(main.rate_limited("probe", 2))
        self.assertFalse(main.rate_limited("probe", 2))
        self.assertTrue(main.rate_limited("probe", 2))
        self.assertTrue(main.rate_limited("probe", 2, record=False))


# ═══════════════════════════════════════════════════════════════════════════
class AdminPageTests(ScriptCase):
    def test_an_empty_page_says_nothing_is_hosted_and_offers_the_upload(self):
        text = self.page_text()
        self.assertIn("Nothing is hosted yet", text)
        self.assertIn('id="upload-form"', text)
        self.assertIn('value="v4"', text)

    def test_each_script_shows_its_loadstring_with_a_copy_button(self):
        _, answer = self.add("v4", SCRIPT)
        text = self.page_text()
        self.assertIn(answer["loadstring"], text)
        self.assertIn(f'data-copy="{answer["loadstring"]}"', text)
        self.assertIn("Copy loadstring", text)
        self.assertIn('data-admin="rotate" data-name="v4"', text)
        self.assertIn('data-admin="remove" data-name="v4"', text)
        self.assertIn(f"{len(SCRIPT.encode())} bytes", text)
        self.assertIn(hashlib.sha256(SCRIPT.encode()).hexdigest()[:12], text)

    def test_the_page_shows_how_often_it_was_opened_and_when(self):
        token, _ = self.add()
        self.assertIn("Opened 0 times, last never", self.page_text())
        self.fetch(token)
        self.fetch(token)
        text = self.page_text()
        self.assertIn("Opened 2 times", text)
        self.assertNotIn("last never", text)
        self.assertIn("UTC", text)

    def test_every_script_gets_its_own_box(self):
        self.add("one")
        self.add("two")
        text = self.page_text()
        self.assertEqual(text.count('class="loadline"'), 2)
        self.assertIn('id="line-1"', text)
        self.assertIn('id="line-2"', text)

    def test_the_page_is_not_kept_or_indexed_and_loads_its_own_script(self):
        resp = self.page()
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(resp.headers["x-robots-tag"], "noindex")
        text = resp.body.decode()
        self.assertIn('<meta name="robots" content="noindex">', text)
        self.assertIn("/static/admin.js", text)

    def test_the_page_script_and_styles_exist(self):
        js = (APP_DIR / "static" / "admin.js").read_text()
        for needle in ("HEADERS = { 'X-Admin-Request': '1' }", "/admin/scripts/", "/rotate", "'PUT'", "'DELETE'"):
            self.assertIn(needle, js)
        css = (APP_DIR / "static" / "style.css").read_text()
        for cls in (".hosted", ".loadline", ".upload", ".field"):
            self.assertIn(cls, css)


# ═══════════════════════════════════════════════════════════════════════════
class NothingOnTheShopFrontTests(ScriptCase):
    def test_the_storefront_never_mentions_hosted_scripts_or_the_admin_page(self):
        self.add()
        page = main.index(stubs.make_request("/")).body.decode()
        for needle in ("/admin", "/l/", "Hosted", "hosted", "loadstring"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, page)

    def test_no_shop_template_links_to_it(self):
        for path in (APP_DIR / "templates").glob("*.html"):
            if path.name != "admin.html":
                with self.subTest(template=path.name):
                    text = path.read_text()
                    self.assertNotIn("/admin", text)
                    self.assertNotIn("/l/", text)

    def test_robots_and_health_say_nothing_about_it(self):
        self.add()
        self.assertNotIn(b"admin", main.robots().body)
        self.assertNotIn(b"/l/", main.robots().body)
        self.assertNotIn("script", json.dumps(main.health()).lower())


if __name__ == "__main__":
    unittest.main()
