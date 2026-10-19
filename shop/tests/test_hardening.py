"""Tests for the extra protection around hosted scripts and /admin (app/main.py): the link kept out
of the platform's access log, the rules for the admin password, the hourly lockout behind the
per-minute one, the limit on wrong links, which address the limits go by, and the note when the
admin is used from a new address. Same stand-ins as test_shop.py (see stubs.py).

Run from the shop folder:  python3 -m unittest discover -s tests -v
"""
import ast
import asyncio
import contextlib
import importlib.util
import io
import logging
import sys
import time as real_time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from test_scripts import APP_DIR, BROWSER_PAGE_LOADS, PASSWORD, WRONG, ScriptCase, basic, bearer  # noqa: E402
from test_shop import main, stubs  # noqa: E402

HEX48 = "9f2c4be1d07a5836ce91ab40f2d7c385e6190ab7d4c2f813"     # what `openssl rand -hex 24` prints


class StrictHandler(logging.StreamHandler):
    """A handler that raises when a record can't be formatted, where logging normally just prints
    "--- Logging error ---" and carries on: a filter that breaks a formatter must fail a test."""

    def handleError(self, record):
        raise


class FakeTime:
    """Stands in for main.time so a test can move the clock the limiters read."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


class HardeningCase(ScriptCase):
    def setUp(self):
        super().setUp()
        main._slow_hits.clear()
        main._admin_seen_addresses.clear()
        self.patch(main, "_address_note_done", False)
        self.clock = FakeTime()
        self.patch(main, "time", self.clock)

    @contextlib.contextmanager
    def no_logs(self, level="INFO"):
        """Check the shop logs nothing (the suite normally silences logging, see ShopCase.logged)."""
        logging.disable(logging.NOTSET)
        try:
            with self.assertNoLogs("shop", level):
                yield
        finally:
            logging.disable(logging.CRITICAL)

    def admin_routes(self, auth):
        return [("page", lambda: self.page(auth=auth)),
                ("upload", lambda: self.upload("v4", auth=auth)),
                ("rotate", lambda: self.rotate("v4", auth=auth)),
                ("delete", lambda: self.delete("v4", auth=auth))]


# ═══════════════════════════════════════════════════════════════════════════
class LinkInLogTests(unittest.TestCase):
    """uvicorn's access log writes the whole request line, so a hosted script's link would sit in
    the platform's logs for anyone who can read them."""
    TOKEN = "q7Zk3-Vx_9mB2nL5pR8tY1wC4eH6jD0a"

    def setUp(self):
        main.hide_links_in_logs()
        logging.disable(logging.NOTSET)                       # the suite normally silences logging
        self.addCleanup(logging.disable, logging.CRITICAL)
        self.stream = io.StringIO()
        handler = StrictHandler(self.stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.getLogger("uvicorn.access")
        self.handler = handler
        self.logger.addHandler(handler)
        self.addCleanup(self.logger.removeHandler, handler)
        old_level, old_propagate = self.logger.level, self.logger.propagate
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.addCleanup(self.logger.setLevel, old_level)
        self.addCleanup(setattr, self.logger, "propagate", old_propagate)

    def access(self, path, status=200):
        """One line the way uvicorn writes it."""
        self.logger.info('%s - "%s %s HTTP/%s" %d', "198.51.100.7:0", "GET", path, "1.1", status)
        return self.stream.getvalue()

    def test_the_link_is_hidden_in_an_access_line(self):
        out = self.access(f"/l/{self.TOKEN}")
        self.assertNotIn(self.TOKEN, out)
        self.assertEqual(out.strip(), '198.51.100.7:0 - "GET /l/<hidden> HTTP/1.1" 200')

    def test_the_rest_of_the_line_survives(self):
        for path, shown in ((f"/l/{self.TOKEN}?x=1", "/l/<hidden>?x=1"),
                            (f"/l/{self.TOKEN}/extra", "/l/<hidden>/extra"),
                            ("/l/short", "/l/<hidden>")):
            with self.subTest(path=path):
                self.stream.truncate(0)
                self.stream.seek(0)
                out = self.access(path, 404)
                self.assertEqual(out.strip(), f'198.51.100.7:0 - "GET {shown} HTTP/1.1" 404')

    def test_other_paths_are_left_alone(self):
        for path in ("/", "/health", "/static/app.js?v=3f2a", "/admin", "/admin/scripts/v4/rotate",
                     "/terms", "/legal/terms", "/success?session_id=cs_1"):
            with self.subTest(path=path):
                self.stream.truncate(0)
                self.stream.seek(0)
                self.assertIn(f'"GET {path} HTTP/1.1"', self.access(path))

    @unittest.skipUnless(importlib.util.find_spec("uvicorn"), "uvicorn is not installed here")
    def test_uvicorns_own_access_formatter_still_works_on_a_hidden_line(self):
        """It unpacks five arguments from the record, so the filter must not change their shape."""
        from uvicorn.logging import AccessFormatter
        self.handler.setFormatter(AccessFormatter(
            '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
        out = self.access(f"/l/{self.TOKEN}?x=1")
        self.assertNotIn(self.TOKEN, out)
        self.assertEqual(out.strip(), 'INFO:     198.51.100.7:0 - "GET /l/<hidden>?x=1 HTTP/1.1" 200 OK')

    def test_the_link_is_hidden_when_it_is_in_the_message_text_instead_of_the_arguments(self):
        self.logger.info(f"fetched /l/{self.TOKEN} just now")
        self.assertNotIn(self.TOKEN, self.stream.getvalue())

    def test_every_uvicorn_logger_is_covered(self):
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            with self.subTest(logger=name):
                self.assertTrue(any(isinstance(f, main.HideLinks) for f in logging.getLogger(name).filters))

    def test_the_filter_is_installed_when_the_app_loads(self):
        tree = ast.parse((APP_DIR / "main.py").read_text(encoding="utf-8"))
        calls = [node for node in tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and getattr(node.value.func, "id", "") == "hide_links_in_logs"]
        self.assertEqual(len(calls), 1)

    def test_installing_it_again_does_not_stack_filters(self):
        for _ in range(3):
            main.hide_links_in_logs()
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            with self.subTest(logger=name):
                found = [f for f in logging.getLogger(name).filters if isinstance(f, main.HideLinks)]
                self.assertEqual(len(found), 1)

    def test_odd_records_never_break_logging(self):
        hide = main.HideLinks()
        for label, record in (
                ("too few arguments", logging.LogRecord("x", logging.INFO, "p", 1, "%s %s", ("one",), None)),
                ("dict arguments", logging.LogRecord("x", logging.INFO, "p", 1, "%(a)s", ({"a": "/l/abc"},), None)),
                ("no arguments", logging.LogRecord("x", logging.INFO, "p", 1, "plain", None, None)),
                ("number as message", logging.LogRecord("x", logging.INFO, "p", 1, 42, None, None))):
            with self.subTest(case=label):
                self.assertTrue(hide.filter(record))
        record = logging.LogRecord("x", logging.INFO, "p", 1, "%(a)s", ({"a": f"/l/{self.TOKEN}"},), None)
        hide.filter(record)
        self.assertNotIn(self.TOKEN, record.getMessage())

    def test_a_filter_that_meets_something_unexpected_lets_the_record_through_instead_of_raising(self):
        class Exploding:
            msg = "GET /l/abc"

            @property
            def args(self):
                raise RuntimeError("boom")

        self.assertTrue(main.HideLinks().filter(Exploding()))


# ═══════════════════════════════════════════════════════════════════════════
class AdminPasswordTests(HardeningCase):
    def test_what_is_accepted(self):
        for value in (PASSWORD, HEX48, "abcdefghijklmnopqrstuvwx", "abcdefghij" * 3,
                      "Tr0ub4dor&3-Tr0ub4dor&3-xyz"):
            with self.subTest(length=len(value)):
                self.assertIsNone(main.admin_password_problem(value))

    def test_what_is_refused_and_why(self):
        too_short = ("", "x", "abcdefghijklmnopqrstuvw")
        no_variety = ("p" * 24, "ab" * 20, "abc" * 12, "abcdefghi" * 3, "password" * 4, "0" * 100)
        for value in too_short:
            with self.subTest(value=value):
                self.assertIn("24 characters", main.admin_password_problem(value))
        for value in no_variety:
            with self.subTest(value=value[:12]):
                self.assertIn("10 different characters", main.admin_password_problem(value))

    def test_the_edges_of_both_rules(self):
        self.assertIsNone(main.admin_password_problem("abcdefghijklmnopqrstuvwx"))        # 24 long, 24 different
        self.assertIsNotNone(main.admin_password_problem("abcdefghijklmnopqrstuvw"))      # 23 long
        self.assertIsNone(main.admin_password_problem("abcdefghij" * 3))                  # 10 different
        self.assertIsNotNone(main.admin_password_problem("abcdefghi" * 3))                # 9 different
        self.assertEqual((main.ADMIN_TOKEN_MIN, main.ADMIN_TOKEN_DISTINCT_MIN), (24, 10))

    def test_a_long_password_with_no_variety_leaves_the_admin_switched_off(self):
        for value in ("p" * 40, "ab" * 20):
            self.patch(main, "SCRIPT_ADMIN_TOKEN", value)
            self.assertFalse(main.admin_enabled())
            for label, run in self.admin_routes(bearer(value)):
                with self.subTest(password=value[:4], route=label):
                    with self.assertRaises(StarletteHTTPException) as ctx:
                        run()
                    self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.script_rows(), [])

    def test_a_password_of_the_usual_kind_still_works_in_every_route(self):
        self.patch(main, "SCRIPT_ADMIN_TOKEN", HEX48)
        self.assertTrue(main.admin_enabled())
        self.assertEqual(self.page(auth=bearer(HEX48)).status_code, 200)
        self.assertEqual(self.upload("v4", auth=bearer(HEX48)).status_code, 200)

    def test_the_start_up_note_says_what_is_wrong_without_repeating_the_password(self):
        for value, reason in (("p" * 24, "10 different characters"), ("x" * 23, "24 characters")):
            with self.subTest(password=value[:3]):
                self.patch(main, "SCRIPT_ADMIN_TOKEN", value)
                note = main.admin_setup_note()
                self.assertIn("SCRIPT_ADMIN_TOKEN", note)
                self.assertIn(reason, note)
                self.assertIn("switched off", note)
                self.assertNotIn(value, note)

    def test_there_is_no_note_when_the_password_is_fine_or_not_set(self):
        for value in ("", PASSWORD, HEX48):
            with self.subTest(length=len(value)):
                self.patch(main, "SCRIPT_ADMIN_TOKEN", value)
                self.assertIsNone(main.admin_setup_note())

    def start_up(self):
        async def run():
            async with main.lifespan(None):
                pass
        asyncio.run(run())

    def test_start_up_prints_the_note(self):
        self.patch(main, "SCRIPT_ADMIN_TOKEN", "p" * 30)
        with self.logged("WARNING") as cm:
            self.start_up()
        notes = [line for line in cm.output if "SCRIPT_ADMIN_TOKEN" in line]
        self.assertEqual(len(notes), 1)
        self.assertNotIn("p" * 30, notes[0])

    def test_start_up_is_quiet_about_a_good_or_missing_password(self):
        for value in ("", PASSWORD):
            self.patch(main, "SCRIPT_ADMIN_TOKEN", value)
            with self.logged("INFO") as cm:
                self.start_up()
            self.assertFalse([line for line in cm.output if "SCRIPT_ADMIN_TOKEN" in line])


# ═══════════════════════════════════════════════════════════════════════════
class HourlyLockoutTests(HardeningCase):
    def test_a_slow_guesser_is_stopped_by_the_hourly_limit(self):
        self.patch(main, "ADMIN_FAILS_PER_MIN", 3)
        self.patch(main, "ADMIN_FAILS_PER_HOUR", 5)
        ip = "192.0.2.20"
        self.assertEqual([self.page(auth=basic(WRONG), ip=ip).status_code for _ in range(3)], [401] * 3)
        self.assertEqual(self.page(auth=basic(WRONG), ip=ip).status_code, 429)        # the minute limit
        self.clock.advance(61)                                                         # minute limit is over
        self.assertEqual([self.page(auth=basic(WRONG), ip=ip).status_code for _ in range(2)], [401, 401])
        self.clock.advance(61)
        locked = self.page(auth=basic(PASSWORD), ip=ip)                                # five wrong in the hour
        self.assertEqual((locked.status_code, locked.headers["retry-after"]), (429, "3600"))
        self.assertEqual(locked.headers["cache-control"], "no-store")
        self.clock.advance(3600)
        self.assertEqual(self.page(auth=basic(PASSWORD), ip=ip).status_code, 200)

    def test_the_hourly_limit_is_thirty_wrong_passwords(self):
        ip = "192.0.2.21"
        statuses = []
        for i in range(31):
            if i and i % 9 == 0:
                self.clock.advance(61)           # stay under the ten-a-minute limit
            statuses.append(self.page(auth=basic(WRONG), ip=ip).status_code)
        self.assertEqual(statuses, [401] * 30 + [429])
        self.assertEqual(main.ADMIN_FAILS_PER_HOUR, 30)

    def test_the_minute_lockout_still_says_wait_a_minute(self):
        ip = "192.0.2.22"
        for _ in range(10):
            self.page(auth=basic(WRONG), ip=ip)
        self.assertEqual(self.page(auth=basic(PASSWORD), ip=ip).headers["retry-after"], "60")

    def test_one_address_locked_for_the_hour_does_not_lock_the_others(self):
        self.patch(main, "ADMIN_FAILS_PER_HOUR", 2)
        for _ in range(2):
            self.page(auth=basic(WRONG), ip="192.0.2.23")
        self.assertEqual(self.page(auth=basic(PASSWORD), ip="192.0.2.23").status_code, 429)
        self.assertEqual(self.page(auth=basic(PASSWORD), ip="192.0.2.24").status_code, 200)

    def test_right_passwords_and_visits_without_one_never_count_toward_the_hour(self):
        self.patch(main, "ADMIN_FAILS_PER_HOUR", 3)
        ip = "192.0.2.25"
        for _ in range(20):
            self.assertEqual(self.call(main.admin_page, "/admin", ip=ip).status_code, 401)
        for _ in range(20):
            self.assertEqual(self.page(auth=basic(PASSWORD), ip=ip).status_code, 200)
        self.assertEqual(self.page(auth=basic(WRONG), ip=ip).status_code, 401)

    def test_cleaning_up_minute_counts_does_not_forget_the_hourly_ones(self):
        self.assertFalse(main.hourly_limited("victim", 2))
        self.assertFalse(main.hourly_limited("victim", 2))
        self.clock.advance(120)
        for i in range(5100):                                  # enough to make the minute store tidy itself
            main.rate_limited(f"flood-{i}", 8)
        self.assertTrue(main.hourly_limited("victim", 2, record=False))

    def test_the_hourly_store_cannot_grow_without_limit(self):
        self.patch(main, "RATE_TIDY_AT", 10)
        self.patch(main, "RATE_MAX_TRACKED", 20)
        for i in range(500):
            main.hourly_limited(f"spoofed-{i}", 5)
        self.assertLessEqual(len(main._slow_hits), 20)
        main._slow_hits.clear()
        for i in range(20):
            main.hourly_limited(f"spoofed-{i}", 5)
        self.assertEqual(len(main._slow_hits), 20)                            # at the limit, nothing is dropped yet
        self.assertFalse(main.hourly_limited("newest", 1))                    # one past it: the older half goes
        self.assertEqual(len(main._slow_hits), 11)
        self.assertNotIn("spoofed-0", main._slow_hits)
        self.assertIn("spoofed-19", main._slow_hits)
        self.assertTrue(main.hourly_limited("newest", 1, record=False))       # and the newest is still tracked

    def test_expired_counts_are_dropped_when_the_store_is_tidied(self):
        self.patch(main, "RATE_TIDY_AT", 10)
        for i in range(10):
            main.hourly_limited(f"old-{i}", 5)
        self.clock.advance(3601)
        main.hourly_limited("new-1", 5)
        main.hourly_limited("new-2", 5)
        self.assertEqual(sorted(main._slow_hits), ["new-1", "new-2"])

    def test_the_hourly_counter_can_look_without_counting(self):
        for _ in range(5):
            self.assertFalse(main.hourly_limited("probe", 2, record=False))
        self.assertFalse(main.hourly_limited("probe", 2))
        self.assertFalse(main.hourly_limited("probe", 2))
        self.assertTrue(main.hourly_limited("probe", 2))
        self.clock.advance(3599)
        self.assertTrue(main.hourly_limited("probe", 2, record=False))
        self.clock.advance(2)
        self.assertFalse(main.hourly_limited("probe", 2, record=False))


# ═══════════════════════════════════════════════════════════════════════════
class WrongLinkTests(HardeningCase):
    """Guessing links: a wrong one costs the guesser much more than a right one."""

    def misses(self, ip, count, headers=None):
        return [self.fetch("Z" * 32, ip=ip, headers=headers).status_code for _ in range(count)]

    def test_five_wrong_links_in_a_minute_and_that_address_waits_even_for_the_right_one(self):
        token, _ = self.add()
        ip = "198.51.100.50"
        self.assertEqual(self.misses(ip, 5), [404] * 5)
        blocked = self.fetch(token, ip=ip)
        self.assertEqual((blocked.status_code, blocked.headers["retry-after"]), (429, "60"))
        self.assertEqual(blocked.headers["cache-control"], "no-store")
        self.assertEqual(self.fetch(token, ip="198.51.100.51").status_code, 200)       # others unaffected
        self.clock.advance(61)
        self.assertEqual(self.fetch(token, ip=ip).status_code, 200)

    def test_twenty_wrong_links_in_an_hour_block_that_address_for_the_hour(self):
        self.patch(main, "LINK_MISSES_PER_MIN", 2)
        self.patch(main, "LINK_MISSES_PER_HOUR", 4)
        token, _ = self.add()
        ip = "198.51.100.52"
        self.assertEqual(self.misses(ip, 2), [404, 404])
        self.assertEqual(self.fetch(token, ip=ip).status_code, 429)                    # minute limit
        self.clock.advance(61)
        self.assertEqual(self.misses(ip, 2), [404, 404])
        self.clock.advance(61)
        blocked = self.fetch(token, ip=ip)
        self.assertEqual((blocked.status_code, blocked.headers["retry-after"]), (429, "3600"))
        self.assertEqual(self.fetch(token, ip="198.51.100.53").status_code, 200)
        self.clock.advance(3600)
        self.assertEqual(self.fetch(token, ip=ip).status_code, 200)

    def test_the_limits_are_five_a_minute_and_twenty_an_hour(self):
        self.assertEqual((main.LINK_MISSES_PER_MIN, main.LINK_MISSES_PER_HOUR), (5, 20))

    def test_good_loads_and_browser_visits_never_count_as_misses(self):
        self.patch(main, "SCRIPT_FETCHES_PER_MIN", 100)
        token, _ = self.add()
        ip = "198.51.100.54"
        self.assertEqual([self.fetch(token, ip=ip).status_code for _ in range(25)], [200] * 25)
        page = BROWSER_PAGE_LOADS["chrome tab"]
        self.assertEqual(self.misses(ip, 20, headers=page), [302] * 20)
        self.assertEqual(self.misses(ip, 1), [404])                                    # first real miss, not blocked

    def test_links_in_the_wrong_shape_count_as_misses_too(self):
        ip = "198.51.100.55"
        shapes = ("short", "x" * 65, "../etc/passwd", "a b c d e f g h i j k l", "é" * 20)
        self.assertEqual([self.fetch(bad, ip=ip).status_code for bad in shapes], [404] * 5)
        self.assertEqual(self.fetch("short", ip=ip).status_code, 429)

    def test_a_blocked_address_is_answered_without_looking_anything_up(self):
        token, _ = self.add()
        ip = "198.51.100.56"
        self.misses(ip, 5)
        with mock.patch.object(main, "serve_script") as lookup:
            self.assertEqual(self.fetch(token, ip=ip).status_code, 429)
            lookup.assert_not_called()
        self.assertEqual(self.script_rows()[0]["hits"], 0)


# ═══════════════════════════════════════════════════════════════════════════
class ClientAddressTests(HardeningCase):
    """The limits go by the visitor's address. Railway's edge sets X-Real-IP to it; uvicorn is told
    to trust X-Forwarded-For, which a visitor can add to, so its address can be faked."""

    def setUp(self):
        super().setUp()
        self.patch(main, "SCRIPT_FETCHES_PER_MIN", 2)
        self.token, _ = self.add()

    def statuses(self, count, wire, headers=None):
        return [self.fetch(self.token, ip=wire, headers=headers).status_code for _ in range(count)]

    def test_x_real_ip_decides_who_is_asking(self):
        wire = "198.51.100.60"
        self.assertEqual(self.statuses(3, wire, {"x-real-ip": "203.0.113.1"}), [200, 200, 429])
        self.assertEqual(self.statuses(1, wire, {"x-real-ip": "203.0.113.2"}), [200])

    def test_a_faked_forwarding_header_does_not_give_a_fresh_allowance(self):
        # uvicorn would report each faked X-Forwarded-For as the address; X-Real-IP stays the same
        out = [self.fetch(self.token, ip=f"10.9.8.{i}",
                          headers={"x-real-ip": "203.0.113.5", "x-forwarded-for": f"7.7.7.{i}"}).status_code
               for i in range(4)]
        self.assertEqual(out, [200, 200, 429, 429])

    def test_without_x_real_ip_the_address_uvicorn_reports_is_used(self):
        self.assertEqual(self.statuses(3, "198.51.100.61"), [200, 200, 429])
        self.assertEqual(self.statuses(1, "198.51.100.62"), [200])

    def test_a_bad_x_real_ip_is_ignored(self):
        for i, bad in enumerate(("garbage", "1.2.3.4:80", "1.2.3.4, 5.6.7.8", "", "   ", "999.1.1.1",
                                 "<script>", "1.2.3", "0x7f.1")):
            with self.subTest(header=bad):
                wire = f"198.51.100.{100 + i}"
                self.assertEqual(self.statuses(3, wire, {"x-real-ip": bad}), [200, 200, 429])   # the wire address
                self.assertEqual(self.statuses(1, wire + "9", {"x-real-ip": bad}), [200])        # another wire address

    def test_every_way_of_writing_an_ipv6_address_is_one_visitor(self):
        wire = "198.51.100.70"
        out = [self.fetch(self.token, ip=wire, headers={"x-real-ip": value}).status_code
               for value in ("2001:db8::1", "2001:0DB8:0:0:0:0:0:1", " 2001:db8:0::1 ")]
        self.assertEqual(out, [200, 200, 429])

    def test_the_admin_lockout_goes_by_it_too(self):
        real = {"x-real-ip": "203.0.113.30"}
        for i in range(10):
            self.assertEqual(self.call(main.admin_page, "/admin", auth=basic(WRONG), headers=real,
                                       ip=f"10.1.1.{i}").status_code, 401)
        self.assertEqual(self.call(main.admin_page, "/admin", auth=basic(PASSWORD), headers=real,
                                   ip="10.1.1.200").status_code, 429)
        other = {"x-real-ip": "203.0.113.31"}
        self.assertEqual(self.call(main.admin_page, "/admin", auth=basic(PASSWORD), headers=other,
                                   ip="10.1.1.200").status_code, 200)

    def test_wrong_links_are_counted_by_it_too(self):
        self.patch(main, "SCRIPT_FETCHES_PER_MIN", 100)       # so it is the wrong-link limit that answers
        real = {"x-real-ip": "203.0.113.32"}
        for i in range(5):
            self.assertEqual(self.fetch("Z" * 32, ip=f"10.2.2.{i}", headers=real).status_code, 404)
        self.assertEqual(self.fetch(self.token, ip="10.2.2.99", headers=real).status_code, 429)

    def test_the_checkout_limit_goes_by_it_too(self):
        calls = []
        self.stripe_stub("create", lambda **kw: calls.append(kw) or {"url": "https://checkout.stripe.test/c/1"})

        def checkout(wire, real):
            request = stubs.make_request("/buy", "POST", headers={"x-real-ip": real}, client=(wire, 5000))
            return asyncio.run(main.start_checkout(request, "week", {"agree": "yes"})).status_code

        self.assertEqual([checkout(f"10.3.3.{i}", "203.0.113.40") for i in range(8)], [303] * 8)
        self.assertEqual(checkout("10.3.3.99", "203.0.113.40"), 429)
        self.assertEqual(checkout("10.3.3.99", "203.0.113.41"), 303)

    def test_one_note_per_start_says_whether_the_two_addresses_agree_and_never_shows_them(self):
        agree = {"x-real-ip": "198.51.100.80"}
        with self.logged("INFO") as cm:
            self.fetch(self.token, ip="198.51.100.80", headers=agree)
            self.fetch(self.token, ip="198.51.100.80", headers=agree)
        notes = [line for line in cm.output if "X-Real-IP" in line]
        self.assertEqual(len(notes), 1)
        self.assertIn("matches", notes[0])
        self.assertNotIn("198.51.100.80", "".join(cm.output))

    def test_the_note_says_when_they_differ(self):
        with self.logged("INFO") as cm:
            self.fetch(self.token, ip="198.51.100.81", headers={"x-real-ip": "203.0.113.82"})
        notes = [line for line in cm.output if "X-Real-IP" in line]
        self.assertEqual(len(notes), 1)
        self.assertIn("differs from", notes[0])
        self.assertNotIn("203.0.113.82", "".join(cm.output))
        self.assertNotIn("198.51.100.81", "".join(cm.output))

    def test_no_note_when_there_is_no_x_real_ip(self):
        with self.no_logs("INFO"):
            self.fetch(self.token, ip="198.51.100.83")
            self.fetch(self.token, ip="198.51.100.83", headers={"x-real-ip": "garbage"})


# ═══════════════════════════════════════════════════════════════════════════
class AdminSignInNoticeTests(HardeningCase):
    """A line in the log the first time the admin is used from an address, so a sign-in that
    wasn't yours can be spotted."""

    def notes(self, cm):
        return [line for line in cm.output if "Admin signed in from a new address" in line]

    def test_a_new_address_is_noted_once(self):
        with self.logged("INFO") as cm:
            for ip in ("192.0.2.30", "192.0.2.30", "192.0.2.31", "192.0.2.30"):
                self.assertEqual(self.page(ip=ip).status_code, 200)
        notes = self.notes(cm)
        self.assertEqual(len(notes), 2)
        self.assertIn("192.0.2.30", notes[0])
        self.assertIn("192.0.2.31", notes[1])

    def test_uploads_and_other_admin_routes_count_as_signing_in_too(self):
        with self.logged("INFO") as cm:
            self.assertEqual(self.upload("v4", ip="192.0.2.32").status_code, 200)
        self.assertEqual(len(self.notes(cm)), 1)

    def test_a_wrong_password_is_not_a_sign_in_and_the_password_is_never_logged(self):
        with self.logged("INFO") as cm:
            self.assertEqual(self.page(auth=basic(WRONG), ip="192.0.2.33").status_code, 401)
            self.assertEqual(self.page(ip="192.0.2.34").status_code, 200)
        self.assertEqual(len(self.notes(cm)), 1)
        self.assertNotIn("192.0.2.33", self.notes(cm)[0])
        self.assertNotIn(PASSWORD, "".join(cm.output))
        self.assertNotIn(WRONG, "".join(cm.output))

    def test_it_goes_by_the_real_address(self):
        with self.logged("INFO") as cm:
            self.call(main.admin_page, "/admin", auth=bearer(PASSWORD), ip="10.0.0.1",
                      headers={"x-real-ip": "203.0.113.50"})
        (note,) = self.notes(cm)
        self.assertIn("203.0.113.50", note)
        self.assertNotIn("10.0.0.1", note)

    def test_the_list_of_known_addresses_stays_small_and_nothing_goes_unnoted(self):
        with self.logged("INFO") as cm:
            for i in range(120):
                self.assertEqual(self.page(ip=f"192.0.2.{i + 100}").status_code, 200)
        self.assertEqual(len(self.notes(cm)), 120)
        self.assertLessEqual(len(main._admin_seen_addresses), 50)


if __name__ == "__main__":
    unittest.main()
