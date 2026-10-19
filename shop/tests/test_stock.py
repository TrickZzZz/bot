"""Tests for the shop's key stock: keys made by hand in the Luarmor dashboard and loaded from the
LUARMOR_STOCK_* variables, handed out one per paid order. Same stand-ins as test_shop.py (see
stubs.py); nothing here touches the real Stripe, Luarmor, Railway or database.

Run from the shop folder:  python3 -m unittest discover -s tests -v
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_shop import PI, SID, ShopCase, main, paid_session, stock_key, stubs  # noqa: E402
from test_luarmor import FakeLuarmor as HttpFakeLuarmor  # noqa: E402
from app.luarmor import Luarmor  # noqa: E402


class StockCase(ShopCase):
    """Stock mode: the Luarmor API is off, so a key can only come from the stock."""

    def setUp(self):
        super().setUp()
        self.patch(main, "LUARMOR_USE_API", False)

    def order(self, n, plan="week"):
        return paid_session(sid=f"cs_test_order{n:0>12}", plan=plan, pi=f"pi_test_{n}")


# ═══════════════════════════════════════════════════════════════════════════
class DefaultsTests(unittest.TestCase):
    """No setUp patching here: this is the value the shop starts with when nobody set the variable."""

    def test_the_luarmor_api_is_off_unless_someone_switches_it_on(self):
        self.assertFalse(main.LUARMOR_USE_API)


# ═══════════════════════════════════════════════════════════════════════════
class LoadingTests(StockCase):
    def test_keys_in_each_variable_are_loaded_under_their_own_length(self):
        done = self.stock(day=[stock_key("day", 1), stock_key("day", 2)],
                          week=[stock_key("week", 1)], month=[stock_key("month", 1), stock_key("month", 2)])
        self.assertEqual(done, {"added": 5, "moved": 0, "dropped": 0})
        self.assertEqual([(r["key"], r["plan"]) for r in self.stock_rows()],
                         [(stock_key("day", 1), "day"), (stock_key("day", 2), "day"),
                          (stock_key("week", 1), "week"),
                          (stock_key("month", 1), "month"), (stock_key("month", 2), "month")])
        self.assertTrue(all(r["order_ref"] is None for r in self.stock_rows()))
        self.assertEqual(main.stock_counts(), {"day": 2, "week": 1, "month": 2})

    def test_new_lines_commas_spaces_and_pasted_quotes_all_separate_keys(self):
        a, b, c, d = (stock_key("day", n) for n in range(1, 5))
        with mock.patch.dict("os.environ", {"LUARMOR_STOCK_DAY": f'\n  "{a}",\n{b} ; {c}\t\'{d}\'\n\n',
                                            "LUARMOR_STOCK_WEEK": "", "LUARMOR_STOCK_MONTH": ""}):
            main.sync_stock()
        self.assertEqual([r["key"] for r in self.stock_rows()], [a, b, c, d])

    def test_loading_the_same_keys_again_changes_nothing(self):
        piles = {"day": [stock_key("day", 1)], "week": [stock_key("week", 1), stock_key("week", 2)]}
        self.stock(**piles)
        before = self.stock_rows()
        self.assertEqual(self.stock(**piles), {"added": 0, "moved": 0, "dropped": 0})
        self.assertEqual(self.stock_rows(), before)

    def test_a_key_listed_twice_in_one_variable_is_one_key(self):
        k = stock_key("day", 1)
        self.assertEqual(self.stock(day=[k, k, k])["added"], 1)
        self.assertEqual(len(self.stock_rows()), 1)

    def test_new_keys_are_added_to_what_is_already_there(self):
        self.stock(day=[stock_key("day", 1)])
        done = self.stock(day=[stock_key("day", 1), stock_key("day", 2)])
        self.assertEqual(done["added"], 1)
        self.assertEqual(main.stock_counts()["day"], 2)

    def test_items_that_are_not_keys_are_skipped_and_good_ones_still_load(self):
        good = stock_key("day", 1)
        junk = ["short", "has!symbol!in!it", "x" * 100, "<script>alert(1)</script>"]
        with self.logged("ERROR") as cm:
            done = self.stock(day=junk + [good])
        self.assertEqual(done["added"], 1)
        self.assertEqual([r["key"] for r in self.stock_rows()], [good])
        self.assertEqual(len(cm.output), len(junk))                      # one complaint per bad item

    def test_a_key_listed_under_two_lengths_is_skipped_everywhere(self):
        clash, fine = stock_key("day", 1), stock_key("week", 1)
        with self.logged("ERROR"):
            self.stock(day=[clash], week=[clash, fine])
        self.assertEqual([r["key"] for r in self.stock_rows()], [fine])

    def test_nothing_is_logged_with_a_key_in_it(self):
        keys = [stock_key("day", 1), stock_key("week", 1), stock_key("week", 2)]
        typo = stock_key("day", 9)[:-1] + "!"           # a key with one wrong character is still a secret
        with self.logged("INFO") as cm:
            self.stock(day=[keys[0], typo], week=[keys[1], keys[2], keys[0]])   # includes a clash and a junk item
        text = "\n".join(cm.output)
        for k in keys + [typo]:
            self.assertNotIn(k, text)

    def test_the_api_switch_reads_the_usual_ways_of_saying_yes(self):
        for value, expected in (("1", True), ("true", True), ("TRUE", True), ("Yes", True), ("on", True),
                                (" 1 ", True), ("", False), ("0", False), ("false", False),
                                ("no", False), ("off", False), ("2", False), ("enabled", False)):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {"LUARMOR_USE_API": value}):
                    self.assertEqual(main.flag("LUARMOR_USE_API"), expected)
        with mock.patch.dict("os.environ", clear=False):
            os.environ.pop("LUARMOR_USE_API", None)
            self.assertFalse(main.flag("LUARMOR_USE_API"))     # unset means off

    def test_the_api_needs_the_switch_and_a_configured_client(self):
        for use, client, expected in ((True, Luarmor("k", "proj1"), True), (False, Luarmor("k", "proj1"), False),
                                      (True, Luarmor("", ""), False), (False, Luarmor("", ""), False)):
            with self.subTest(use=use, configured=client.configured):
                self.patch(main, "LUARMOR_USE_API", use)
                self.patch(main, "luarmor", client)
                self.assertEqual(main.api_enabled(), expected)

    def test_an_unsold_key_moved_to_another_variable_changes_length(self):
        k = stock_key("day", 1)
        self.stock(day=[k])
        done = self.stock(week=[k])
        self.assertEqual(done, {"added": 0, "moved": 1, "dropped": 0})
        self.assertEqual([(r["key"], r["plan"]) for r in self.stock_rows()], [(k, "week")])

    def test_an_unsold_key_removed_from_the_variables_is_dropped(self):
        keep, gone = stock_key("day", 1), stock_key("day", 2)
        self.stock(day=[keep, gone])
        done = self.stock(day=[keep])
        self.assertEqual(done["dropped"], 1)
        self.assertEqual([r["key"] for r in self.stock_rows()], [keep])

    def test_a_sold_key_is_never_dropped_or_moved(self):
        sold, unsold = stock_key("week", 1), stock_key("week", 2)
        self.stock(week=[sold, unsold])
        self.assertEqual(main.fulfil(self.order(1)), sold)
        done = self.stock(day=[sold])                  # removed from WEEK and listed under DAY
        self.assertEqual(done, {"added": 0, "moved": 0, "dropped": 1})   # only the unsold one went
        (row,) = self.stock_rows()
        self.assertEqual((row["key"], row["plan"], row["order_ref"]), (sold, "week", self.order(1)["id"]))

    def test_a_sold_key_stays_when_it_is_taken_out_of_the_variables(self):
        sold, unsold = stock_key("week", 1), stock_key("week", 2)
        self.stock(week=[sold, unsold])
        main.fulfil(self.order(1))
        self.stock()                                   # both variables emptied
        (row,) = self.stock_rows()
        self.assertEqual((row["key"], row["order_ref"]), (sold, self.order(1)["id"]))
        self.assertEqual(main.fulfil(self.order(1)), sold)           # the buyer still gets their key back

    def test_emptying_the_variables_removes_the_unsold_keys(self):
        self.stock(day=[stock_key("day", 1)], week=[stock_key("week", 1)])
        self.stock()
        self.assertEqual(self.stock_rows(), [])
        self.assertEqual(main.stock_counts(), {"day": 0, "week": 0, "month": 0})

    def test_a_loading_failure_does_not_stop_the_shop_from_starting(self):
        with mock.patch.dict("os.environ", {"LUARMOR_STOCK_DAY": stock_key("day", 1)}):
            self.patch(main, "STOCK_ADD_SQL", "INSERT INTO nowhere VALUES (1)")
            main.init_db()                             # must not raise
        self.assertEqual(self.stock_rows(), [])

    def test_the_loaded_stock_survives_a_restart(self):
        self.stock(day=[stock_key("day", 1)])
        main.init_db()                                 # a restart with the same variables
        self.assertEqual(main.stock_counts()["day"], 1)


# ═══════════════════════════════════════════════════════════════════════════
class HandingOutTests(StockCase):
    def test_a_paid_order_gets_the_oldest_unsold_key_of_its_plan_and_luarmor_is_not_called(self):
        first, second = stock_key("week", 1), stock_key("week", 2)
        self.stock(week=[first, second])
        key = main.fulfil(self.order(1))
        self.assertEqual(key, first)
        self.assertEqual(self.luarmor.created, [])
        (row,) = self.rows()
        self.assertEqual((row["key"], row["plan"], row["order_ref"], row["buyer_email"], row["pay_total"]),
                         (first, "week", self.order(1)["id"], "buyer@example.com", 200))
        self.assertIsNone(row["claimed_by"])
        self.assertEqual(main.stock_counts()["week"], 1)

    def test_each_plan_draws_from_its_own_pile(self):
        self.stock(day=[stock_key("day", 1)], week=[stock_key("week", 1)], month=[stock_key("month", 1)])
        for n, plan in enumerate(("month", "day", "week"), 1):
            with self.subTest(plan=plan):
                self.assertEqual(main.fulfil(self.order(n, plan)), stock_key(plan, 1))

    def test_orders_are_served_in_the_order_the_keys_were_loaded(self):
        keys = [stock_key("week", n) for n in (7, 3, 9)]
        self.stock(week=keys)
        self.assertEqual([main.fulfil(self.order(n)) for n in (1, 2, 3)], keys)

    def test_asking_again_for_the_same_order_gives_the_same_key_and_uses_one_key(self):
        self.stock(week=[stock_key("week", 1), stock_key("week", 2)])
        first = main.fulfil(self.order(1))
        self.assertEqual(main.fulfil(self.order(1)), first)
        self.assertEqual(main.stock_counts()["week"], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_no_key_is_ever_sold_twice(self):
        self.stock(week=[stock_key("week", n) for n in range(1, 6)])
        sold = [main.fulfil(self.order(n)) for n in range(1, 6)]
        self.assertEqual(len(set(sold)), 5)
        with self.assertRaises(main.OutOfStock):
            main.fulfil(self.order(6))

    def test_a_plan_with_no_stock_is_out_of_stock_and_leaves_no_trace(self):
        self.stock(day=[stock_key("day", 1)])
        with self.assertRaises(main.OutOfStock) as ctx:
            main.fulfil(self.order(1, "week"))
        self.assertEqual(ctx.exception.plan, "week")
        self.assertEqual(self.rows(), [])
        self.assertEqual(main.stock_counts()["day"], 1)             # the other pile is untouched
        self.assertEqual(self.luarmor.created, [])

    def test_the_same_order_completes_once_the_plan_is_restocked(self):
        with self.assertRaises(main.OutOfStock):
            main.fulfil(self.order(1))
        self.stock(week=[stock_key("week", 1)])
        self.assertEqual(main.fulfil(self.order(1)), stock_key("week", 1))

    def test_the_api_is_only_a_fallback_for_a_plan_with_no_stock(self):
        self.patch(main, "LUARMOR_USE_API", True)
        self.stock(week=[stock_key("week", 1)])
        self.assertEqual(main.fulfil(self.order(1)), stock_key("week", 1))
        self.assertEqual(self.luarmor.created, [])
        api_key = main.fulfil(self.order(2))
        self.assertEqual([(days, key) for days, _, key in self.luarmor.created], [(7, api_key)])

    def test_a_key_that_could_not_be_saved_stays_with_its_order_and_comes_back(self):
        self.stock(week=[stock_key("week", 1), stock_key("week", 2)])
        with mock.patch.object(main, "FINISH_ORDER_SQL", "SELECT nonsense FROM nowhere"):
            with self.assertRaises(Exception):
                main.fulfil(self.order(1))
        self.assertEqual(self.rows(), [])
        self.assertEqual([r["order_ref"] for r in self.stock_rows()], [self.order(1)["id"], None])
        self.assertEqual(self.luarmor.deleted, [])                  # a stock key is not Luarmor's to delete
        self.assertEqual(main.fulfil(self.order(1)), stock_key("week", 1))     # same key, not the next one
        self.assertEqual(main.stock_counts()["week"], 1)

    def test_losing_the_order_to_another_request_keeps_the_stock_key_with_the_order(self):
        self.stock(week=[stock_key("week", 1)])
        real = main.take_stock_key

        def take_then_get_taken_over(sid, plan_id):
            key = real(sid, plan_id)
            with main.engine.begin() as conn:
                conn.execute('UPDATE license_keys SET "key" = :k WHERE order_ref = :sid',
                             {"k": main.PENDING_PREFIX + "new-owner", "sid": sid})
            return key

        self.patch(main, "take_stock_key", take_then_get_taken_over)
        with self.assertRaises(main.KeyPending):
            main.fulfil(self.order(1))
        self.assertEqual(self.luarmor.deleted, [])                  # nothing to delete on Luarmor
        self.assertEqual(self.stock_rows()[0]["order_ref"], self.order(1)["id"])
        # The other request finishes the order later and gets the very same key.
        self.patch(main, "take_stock_key", real)
        with main.engine.begin() as conn:
            conn.execute("DELETE FROM license_keys WHERE order_ref = :sid", {"sid": self.order(1)["id"]})
        self.assertEqual(main.fulfil(self.order(1)), stock_key("week", 1))

    def spy_on_claims(self, before_claim):
        """Run `before_claim(connection, params)` once, just before the first stock claim goes to the
        database: a stand-in for another buyer getting there first."""
        original, fired = stubs._Conn.execute, []

        def spy(conn, stmt, params=None):
            sql = stmt.text if isinstance(stmt, stubs.TextClause) else str(stmt)
            if sql == main.STOCK_CLAIM_SQL and not fired:
                fired.append(1)
                before_claim(conn, params)
            return original(conn, stmt, params)

        p = mock.patch.object(stubs._Conn, "execute", spy)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def steal(conn, params):
        conn._raw.execute("UPDATE luarmor_stock SET order_ref = 'cs_test_thief' WHERE id = :id",
                          {"id": params["id"]})

    def test_a_key_taken_by_another_buyer_at_the_last_moment_is_not_sold_twice(self):
        first, second = stock_key("week", 1), stock_key("week", 2)
        self.stock(week=[first, second])
        self.spy_on_claims(self.steal)
        self.assertEqual(main.fulfil(self.order(1)), second)
        thief, mine = self.stock_rows()
        self.assertEqual((thief["key"], thief["order_ref"]), (first, "cs_test_thief"))   # not overwritten
        self.assertEqual((mine["key"], mine["order_ref"]), (second, self.order(1)["id"]))

    def test_losing_the_only_key_at_the_last_moment_is_out_of_stock(self):
        self.stock(week=[stock_key("week", 1)])
        self.spy_on_claims(self.steal)
        with self.assertRaises(main.OutOfStock):
            main.fulfil(self.order(1))
        self.assertEqual(self.rows(), [])


# ═══════════════════════════════════════════════════════════════════════════
class CheckoutAndShopPageTests(StockCase):
    def checkout(self, plan):
        return asyncio.run(main.start_checkout(stubs.make_request("/buy", "POST"), plan, {"agree": "yes"}))

    def index_html(self):
        return main.index(stubs.make_request("/")).body.decode()

    def test_a_length_with_no_stock_cannot_be_bought_but_the_others_can(self):
        calls = []
        self.stripe_stub("create", lambda **kw: calls.append(kw) or {"url": "https://checkout.stripe.test/c/1"})
        self.stock(day=[stock_key("day", 1)])
        self.assertEqual(self.checkout("week").status_code, 409)
        self.assertEqual(calls, [])
        resp = self.checkout("day")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "https://checkout.stripe.test/c/1"))

    def test_with_nothing_in_stock_checkout_is_closed(self):
        self.stripe_stub("create", lambda **kw: self.fail("Stripe must not be called"))
        for plan in ("day", "week", "month"):
            self.assertEqual(self.checkout(plan).status_code, 503)

    def test_a_length_stays_open_to_buy_until_its_last_key_is_sold(self):
        self.stripe_stub("create", lambda **kw: {"url": "https://checkout.stripe.test/c/1"})
        self.stock(week=[stock_key("week", 1)])
        self.assertEqual(self.checkout("week").status_code, 303)
        main.fulfil(self.order(1))
        self.assertEqual(self.checkout("week").status_code, 503)

    def test_with_the_api_on_every_length_is_for_sale_even_without_stock(self):
        self.patch(main, "LUARMOR_USE_API", True)
        self.stripe_stub("create", lambda **kw: {"url": "https://checkout.stripe.test/c/1"})
        self.assertEqual(self.checkout("month").status_code, 303)

    def test_the_shop_page_lists_only_what_is_in_stock_and_names_what_is_sold_out(self):
        self.stock(day=[stock_key("day", 1)], week=[stock_key("week", 1)])
        html = self.index_html()
        self.assertIn('value="day"', html)
        self.assertIn('value="week"', html)
        self.assertNotIn('value="month"', html)
        self.assertIn("Sold out for now: 30 days.", html)
        self.assertNotIn("disabled", html)
        self.assertNotIn("switched on yet", html)

    def test_the_preselected_length_moves_to_one_that_is_in_stock(self):
        self.stock(day=[stock_key("day", 1)])
        html = self.index_html()
        self.assertEqual(html.count(" selected>"), 1)
        self.assertRegex(html, r'<option value="day"[^>]* selected>')

    def test_the_usual_default_is_kept_when_it_is_in_stock(self):
        self.stock(day=[stock_key("day", 1)], week=[stock_key("week", 1)])
        self.assertRegex(self.index_html(), r'<option value="week"[^>]* selected>')

    def test_with_everything_sold_out_the_page_says_so_and_the_button_is_locked(self):
        html = self.index_html()
        self.assertIn("Every key is sold out right now", html)
        self.assertIn("disabled", html)
        self.assertNotIn("switched on yet", html)
        for plan in ("day", "week", "month"):
            self.assertIn(f'value="{plan}"', html)

    def test_with_stripe_off_the_page_says_checkout_is_not_switched_on_even_with_stock(self):
        self.stock(day=[stock_key("day", 1)])
        self.patch(main, "STRIPE_SECRET_KEY", "")
        html = self.index_html()
        self.assertIn("switched on yet", html)
        self.assertIn("disabled", html)

    def test_a_stock_database_that_cannot_be_read_shows_sold_out_instead_of_failing(self):
        self.stock(day=[stock_key("day", 1)])
        self.patch(main, "STOCK_COUNTS_SQL", "SELECT nonsense FROM nowhere")
        self.assertEqual(main.available_plans(), set())
        self.assertIn("sold out", self.index_html().lower())
        self.assertEqual(main.health()["stock"], [])

    def test_health_names_the_lengths_in_stock_without_giving_counts(self):
        self.stock(day=[stock_key("day", n) for n in range(1, 4)], month=[stock_key("month", 1)])
        self.assertEqual(main.health(), {"ok": True, "checkout": True, "webhook": True, "luarmor": True,
                                         "api": False, "stock": ["day", "month"]})

    def test_stock_alone_is_enough_to_sell_even_when_luarmor_is_not_configured(self):
        self.patch(main, "luarmor", Luarmor("", ""))
        self.stock(day=[stock_key("day", 1)])
        self.assertEqual(main.health()["luarmor"], False)
        self.assertEqual(main.health()["checkout"], True)


# ═══════════════════════════════════════════════════════════════════════════
class PaymentFlowTests(StockCase):
    def test_a_payment_webhook_hands_out_a_stock_key_and_the_page_shows_it(self):
        self.stock(week=[stock_key("week", 1)])
        self.assertEqual(self.webhook(self.completed())[0], 200)
        (row,) = self.rows()
        self.assertEqual((row["key"], row["payment_intent"]), (stock_key("week", 1), PI))
        html = self.success_page().body.decode()
        self.assertIn(stock_key("week", 1), html)
        self.assertEqual(self.luarmor.created, [])

    def test_the_success_page_alone_also_hands_out_a_stock_key(self):
        self.stock(week=[stock_key("week", 1)])
        self.stripe_stub("retrieve", lambda sid, api_key=None: paid_session(sid))
        resp = self.success_page()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(stock_key("week", 1), resp.body.decode())

    def test_webhook_then_page_then_webhook_again_use_one_key(self):
        self.stock(week=[stock_key("week", 1), stock_key("week", 2)])
        self.webhook(self.completed())
        self.success_page()
        self.webhook(self.completed())
        self.assertEqual(main.stock_counts()["week"], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_out_of_stock_webhook_answers_500_and_stripes_retry_works_after_a_restock(self):
        with self.logged("ERROR") as cm:
            status, _ = self.webhook(self.completed())
        self.assertEqual(status, 500)
        self.assertIn("LUARMOR_STOCK_WEEK", "\n".join(cm.output))   # the log says which variable to fill
        self.assertEqual(self.rows(), [])
        self.stock(week=[stock_key("week", 1)])
        self.assertEqual(self.webhook(self.completed())[0], 200)
        self.assertEqual(self.rows()[0]["key"], stock_key("week", 1))

    def test_out_of_stock_page_says_restocking_and_the_same_page_shows_the_key_later(self):
        self.stripe_stub("retrieve", lambda sid, api_key=None: paid_session(sid))
        with self.logged("ERROR"):
            resp = self.success_page()
        html = resp.body.decode()
        self.assertEqual(resp.status_code, 503)
        self.assertIn("Restocking", html)
        self.assertIn('http-equiv="refresh" content="30"', html)
        self.assertIn(SID, html)                                    # the order id to quote in a ticket
        self.assertNotIn("Waiting for payment", html)
        self.assertEqual(self.rows(), [])
        self.stock(week=[stock_key("week", 1)])
        resp = self.success_page()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(stock_key("week", 1), resp.body.decode())

    def paid(self):
        self.stock(week=[stock_key("week", 1)])
        self.assertEqual(self.webhook(self.completed())[0], 200)
        return stock_key("week", 1)

    def test_dispute_on_a_stock_key_revokes_it_and_logs_what_to_blacklist_by_hand(self):
        key = self.paid()
        with self.logged("ERROR") as cm:
            self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(self.luarmor.blacklisted, [])              # the API is off
        (row,) = self.rows()
        self.assertEqual(row["claimed_by"], 0)                      # revoked, as the bot expects
        self.assertIsNotNone(row["dispute_at"])
        self.assertIsNone(row["luarmor_banned_at"])                 # not banned: someone still has to do it
        text = "\n".join(cm.output)
        self.assertIn("BLACKLIST BY HAND", text)
        self.assertIn(key, text)
        self.assertIn("dp_1", text)

    def test_the_ban_is_made_for_real_once_the_api_is_on_and_the_dispute_event_comes_again(self):
        key = self.paid()
        with self.logged("ERROR"):
            self.webhook(self.dispute())
        self.patch(main, "LUARMOR_USE_API", True)
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(self.luarmor.blacklisted, [(key, "Payment dispute")])
        self.assertIsNotNone(self.rows()[0]["luarmor_banned_at"])

    def test_a_redeemed_stock_key_keeps_its_owner_when_disputed(self):
        self.paid()
        with main.engine.begin() as conn:
            conn.execute("UPDATE license_keys SET claimed_by = 424242")
        with self.logged("ERROR"):
            self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(self.rows()[0]["claimed_by"], 424242)


# ═══════════════════════════════════════════════════════════════════════════
class StockEndToEndTests(StockCase):
    def test_a_stock_order_makes_no_request_to_luarmor_at_all(self):
        http = HttpFakeLuarmor()
        self.addCleanup(http.close)
        self.patch(main, "luarmor", Luarmor("e2e-api-key", "e2eproject123", http.url, timeout=2))
        self.stripe_stub("retrieve", lambda sid, api_key=None: paid_session(sid, plan="month"))
        self.stock(month=[stock_key("month", 1)])
        self.assertIn(stock_key("month", 1), self.success_page().body.decode())
        self.assertEqual(self.webhook(self.completed(paid_session(plan="month")))[0], 200)
        self.assertEqual(http.requests, [])


if __name__ == "__main__":
    unittest.main()
