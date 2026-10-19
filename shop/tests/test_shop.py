"""Tests for the shop's Luarmor key issuing (app/main.py), run against stand-ins for
fastapi / sqlalchemy / stripe (see stubs.py) and a fake Luarmor. Nothing here touches the real
Stripe, Luarmor, Railway or database.

Run from the shop folder:  python3 -m unittest discover -s tests -v
"""
import asyncio
import contextlib
import json
import logging
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

logging.disable(logging.CRITICAL)   # the shop logs a lot on purpose; keep test output readable

import stubs  # noqa: E402

main, stripe = stubs.install()
from app.luarmor import Luarmor, LuarmorError  # noqa: E402
from test_luarmor import FakeLuarmor as HttpFakeLuarmor  # noqa: E402

SID = "cs_test_a1B2c3D4e5F6g7H8"


def stock_key(plan, n):
    """A made-up 32-character Luarmor key that says which pile it belongs to."""
    prefix = f"{plan.capitalize()}Key"
    return prefix + str(n).zfill(32 - len(prefix))
PI = "pi_test_123"


def paid_session(sid=SID, plan="week", pi=PI, email="buyer@example.com", total=200):
    return {"id": sid, "payment_status": "paid", "metadata": {"plan": plan},
            "customer_details": {"email": email}, "amount_subtotal": total, "amount_total": total,
            "currency": "gbp", "total_details": {"amount_tax": 0, "amount_discount": 0},
            "payment_intent": pi}


class FakeLuarmor:
    """In-process stand-in for app.luarmor.Luarmor that records what the shop asks of it."""
    configured = True

    def __init__(self):
        self.created, self.blacklisted, self.deleted = [], [], []
        self.fail = None          # an exception to raise instead of answering
        self.on_create = None     # runs in the middle of create_key (to stage a concurrent request)

    def create_key(self, days, note):
        if self.on_create:
            self.on_create()
        if self.fail:
            raise self.fail
        key = f"LuaKey{len(self.created) + 1:026d}"      # 32 characters, mixed case
        self.created.append((days, note, key))
        return key

    def blacklist_key(self, key, reason):
        if self.fail:
            raise self.fail
        self.blacklisted.append((key, reason))

    def delete_key(self, key):
        self.deleted.append(key)


class ShopCase(unittest.TestCase):
    def setUp(self):
        with main.engine.begin() as conn:
            conn.execute("DELETE FROM license_keys")
            conn.execute("DELETE FROM luarmor_stock")
        main._hits.clear()
        self.luarmor = FakeLuarmor()
        self.patch(main, "luarmor", self.luarmor)
        self.patch(main, "STRIPE_SECRET_KEY", "sk_test_x")
        self.patch(main, "STRIPE_WEBHOOK_SECRET", "whsec_x")
        # Most tests here are about the Luarmor API path, so it is on. StockCase turns it off.
        self.patch(main, "LUARMOR_USE_API", True)

    def patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    def stripe_stub(self, name, fn):
        self.patch(stripe.checkout.Session, name, staticmethod(fn))

    def stock(self, **piles):
        """Load keys as the LUARMOR_STOCK_* variables would: stock(day=[...], week=[...]).
        Plans left out get an empty variable. Returns what the loading changed."""
        env = {var: "\n".join(piles.get(plan_id, [])) for plan_id, var in main.STOCK_VARS.items()}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        return main.sync_stock()

    @contextlib.contextmanager
    def logged(self, level="INFO"):
        """Capture the shop's log lines (the suite normally silences logging)."""
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("shop", level) as cm:
                yield cm
        finally:
            logging.disable(logging.CRITICAL)

    def stock_rows(self):
        with main.engine.begin() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM luarmor_stock ORDER BY id").fetchall()]

    # ── database helpers ───────────────────────────────────────────────────
    def rows(self):
        with main.engine.begin() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM license_keys ORDER BY id").fetchall()]

    def insert_row(self, key, sid=SID, plan="week", created=None, claimed_by=None, pay_total=None, pi=None):
        with main.engine.begin() as conn:
            conn.execute('INSERT INTO license_keys ("key", plan, order_ref, created_at, claimed_by, '
                         'pay_total, payment_intent) VALUES (:key, :plan, :sid, :now, :cb, :pt, :pi)',
                         {"key": key, "plan": plan, "sid": sid, "now": created or main.utcnow(),
                          "cb": claimed_by, "pt": pay_total, "pi": pi})

    # ── request helpers ────────────────────────────────────────────────────
    def webhook(self, event, signature="t=1,v1=x"):
        payload = json.dumps(event).encode()
        resp = asyncio.run(main.stripe_webhook(
            stubs.make_request("/stripe/webhook", "POST", body=payload,
                               headers={"stripe-signature": signature})))
        if isinstance(resp, dict):
            return 200, resp
        return resp.status_code, json.loads(resp.body)

    def completed(self, session=None):
        return {"type": "checkout.session.completed", "data": {"object": session or paid_session()}}

    def dispute(self, pi=PI, did="dp_1"):
        return {"type": "charge.dispute.created", "data": {"object": {"id": did, "payment_intent": pi}}}

    def success_page(self, sid=SID):
        return main.success(stubs.make_request("/success", query=f"session_id={sid}".encode()), session_id=sid)


# ═══════════════════════════════════════════════════════════════════════════
class FulfilTests(ShopCase):
    def test_each_plan_asks_luarmor_for_its_own_length(self):
        for plan, days in (("day", 1), ("week", 7), ("month", 30)):
            with self.subTest(plan=plan):
                sid = f"cs_test_{plan}XXXXXXXXXXXX"
                key = main.fulfil(paid_session(sid=sid, plan=plan))
                asked_days, note, made = self.luarmor.created[-1]
                self.assertEqual(asked_days, days)
                self.assertEqual(key, made)
                self.assertTrue(note.startswith(f"shop-{plan}-"))
                self.assertTrue(note.endswith(sid[-10:]))
        self.assertEqual(len(self.luarmor.created), 3)

    def test_plan_lengths_in_the_shop_match_what_is_sent(self):
        self.assertEqual({k: v["days"] for k, v in main.PLANS.items()}, {"day": 1, "week": 7, "month": 30})

    def test_key_is_saved_and_returned_exactly_as_luarmor_made_it(self):
        key = main.fulfil(paid_session())
        self.assertEqual(key, "LuaKey00000000000000000000000001")
        (row,) = self.rows()
        self.assertEqual(row["key"], key)          # same letter case, nothing added or removed
        self.assertEqual((row["plan"], row["order_ref"], row["buyer_email"]), ("week", SID, "buyer@example.com"))
        self.assertEqual((row["pay_total"], row["pay_currency"]), (200, "gbp"))
        self.assertIsNone(row["claimed_by"])

    def test_same_order_again_returns_the_same_key_and_asks_luarmor_once(self):
        first = main.fulfil(paid_session())
        again = main.fulfil(paid_session())
        self.assertEqual(first, again)
        self.assertEqual(len(self.luarmor.created), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_two_orders_get_two_different_keys(self):
        a = main.fulfil(paid_session(sid="cs_test_orderAAAAAAAAAA"))
        b = main.fulfil(paid_session(sid="cs_test_orderBBBBBBBBBB"))
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.rows()), 2)

    def test_unpaid_session_makes_no_key(self):
        order = paid_session()
        order["payment_status"] = "unpaid"
        self.assertIsNone(main.fulfil(order))
        self.assertEqual((self.rows(), self.luarmor.created), ([], []))

    def test_unknown_or_missing_plan_makes_no_key(self):
        for plan in ("year", "", None, "WEEK"):
            with self.subTest(plan=plan):
                order = paid_session(plan=plan)
                if plan is None:
                    order["metadata"] = {}
                self.assertIsNone(main.fulfil(order))
        self.assertEqual((self.rows(), self.luarmor.created), ([], []))

    def test_luarmor_failure_leaves_nothing_behind_and_the_next_try_works(self):
        self.luarmor.fail = LuarmorError("Luarmor answered HTTP 403")
        with self.assertRaises(LuarmorError):
            main.fulfil(paid_session())
        self.assertEqual(self.rows(), [])                 # no row, no placeholder
        self.luarmor.fail = None
        key = main.fulfil(paid_session())
        self.assertEqual([r["key"] for r in self.rows()], [key])
        self.assertEqual(len(self.luarmor.created), 1)

    def test_unconfigured_luarmor_with_no_stock_is_out_of_stock_and_leaves_nothing(self):
        self.patch(main, "luarmor", Luarmor("", ""))
        with self.assertRaises(main.OutOfStock):
            main.fulfil(paid_session())
        self.assertEqual(self.rows(), [])

    def test_request_arriving_during_the_luarmor_call_waits_instead_of_issuing_twice(self):
        seen = {}

        def meanwhile():
            seen["find_order"] = main.find_order(SID)
            try:
                main.fulfil(paid_session())
            except main.KeyPending:
                seen["pending"] = True

        self.luarmor.on_create = meanwhile
        key = main.fulfil(paid_session())
        self.assertTrue(seen["pending"])
        self.assertIsNone(seen["find_order"])             # the placeholder is never shown as a key
        self.assertEqual(len(self.luarmor.created), 1)
        self.assertEqual([r["key"] for r in self.rows()], [key])

    def test_losing_the_insert_race_does_not_call_luarmor(self):
        self.insert_row(main.PENDING_PREFIX + "other-request")
        real, calls = main.order_row, {"n": 0}

        def blind_the_first_time(sid):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(sid)

        self.patch(main, "order_row", blind_the_first_time)
        with self.assertRaises(main.KeyPending):
            main.fulfil(paid_session())
        self.assertEqual(self.luarmor.created, [])
        self.assertEqual(self.rows()[0]["key"], main.PENDING_PREFIX + "other-request")

    def test_fresh_placeholder_is_left_alone(self):
        self.insert_row(main.PENDING_PREFIX + "busy", created=main.utcnow() - timedelta(seconds=10))
        with self.assertRaises(main.KeyPending):
            main.fulfil(paid_session())
        self.assertEqual(self.luarmor.created, [])

    def test_stale_placeholder_from_a_dead_request_is_taken_over(self):
        self.insert_row(main.PENDING_PREFIX + "dead", created=main.utcnow() - timedelta(seconds=600))
        key = main.fulfil(paid_session())
        self.assertEqual([r["key"] for r in self.rows()], [key])
        self.assertEqual(len(self.luarmor.created), 1)

    def test_placeholder_taken_over_while_luarmor_is_slow_discards_the_extra_key(self):
        def someone_else_takes_over():
            with main.engine.begin() as conn:
                conn.execute('UPDATE license_keys SET "key" = :k WHERE order_ref = :sid',
                             {"k": main.PENDING_PREFIX + "new-owner", "sid": SID})

        self.luarmor.on_create = someone_else_takes_over
        with self.assertRaises(main.KeyPending):
            main.fulfil(paid_session())
        (_, _, made), = self.luarmor.created
        self.assertEqual(self.luarmor.deleted, [made])    # no orphan key left on Luarmor
        self.assertEqual(self.rows()[0]["key"], main.PENDING_PREFIX + "new-owner")

    def test_database_failure_after_luarmor_answers_releases_the_order_and_discards_the_key(self):
        self.patch(main, "FINISH_ORDER_SQL", "SELECT nonsense FROM nowhere")
        with self.assertRaises(Exception):
            main.fulfil(paid_session())
        (_, _, made), = self.luarmor.created
        self.assertEqual(self.luarmor.deleted, [made])
        self.assertEqual(self.rows(), [])

    def test_failed_cleanup_of_an_orphan_key_does_not_hide_the_original_problem(self):
        self.patch(main, "FINISH_ORDER_SQL", "SELECT nonsense FROM nowhere")
        self.luarmor.delete_key = mock.Mock(side_effect=LuarmorError("down"))
        with self.assertRaises(Exception) as ctx:
            main.fulfil(paid_session())
        self.assertNotIsInstance(ctx.exception, LuarmorError)

    def test_old_style_key_is_returned_untouched_and_gets_its_amounts_filled_in(self):
        self.insert_row("TRK-W-AAAA-BBBB-CCCC", pay_total=None)
        key = main.fulfil(paid_session())
        self.assertEqual(key, "TRK-W-AAAA-BBBB-CCCC")
        self.assertEqual(self.luarmor.created, [])
        self.assertEqual(self.rows()[0]["pay_total"], 200)

    def test_find_order_shows_the_real_key_and_hides_placeholders(self):
        self.insert_row(main.PENDING_PREFIX + "x")
        self.assertIsNone(main.find_order(SID))
        with main.engine.begin() as conn:
            conn.execute("DELETE FROM license_keys")
        key = main.fulfil(paid_session())
        found = main.find_order(SID)
        self.assertEqual((found["key"], found["plan"], found["claimed"], found["revoked"]),
                         (key, "week", False, False))


# ═══════════════════════════════════════════════════════════════════════════
class CheckoutTests(ShopCase):
    def checkout(self, plan="week", form=None):
        form = {"agree": "yes"} if form is None else form
        return asyncio.run(main.start_checkout(stubs.make_request("/buy", "POST"), plan, form))

    def test_checkout_opens_with_the_plan_the_buyer_picked(self):
        calls = []
        self.stripe_stub("create", lambda **kw: calls.append(kw) or {"url": "https://checkout.stripe.test/c/1"})
        resp = self.checkout("month")
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, "https://checkout.stripe.test/c/1"))
        (kw,) = calls
        self.assertEqual(kw["metadata"]["plan"], "month")
        item = kw["line_items"][0]["price_data"]
        self.assertEqual((item["unit_amount"], item["currency"]), (500, "gbp"))
        self.assertEqual(item["product_data"]["description"], "Redeem it with the Redeem Key button in our Discord")
        self.assertNotIn("claim_key", json.dumps(kw))
        self.assertTrue(kw["success_url"].endswith("/success?session_id={CHECKOUT_SESSION_ID}"))

    def test_checkout_stays_closed_when_luarmor_is_not_configured(self):
        self.patch(main, "luarmor", Luarmor("", ""))
        self.stripe_stub("create", lambda **kw: self.fail("Stripe must not be called"))
        self.assertEqual(self.checkout().status_code, 503)

    def test_checkout_stays_closed_when_the_project_id_is_malformed(self):
        self.patch(main, "luarmor", Luarmor("key", "bad/id"))
        self.stripe_stub("create", lambda **kw: self.fail("Stripe must not be called"))
        self.assertEqual(self.checkout().status_code, 503)

    def test_checkout_stays_closed_when_stripe_is_not_configured(self):
        self.patch(main, "STRIPE_SECRET_KEY", "")
        self.assertEqual(self.checkout().status_code, 503)

    def test_terms_must_be_ticked_and_the_plan_must_exist(self):
        self.stripe_stub("create", lambda **kw: self.fail("Stripe must not be called"))
        self.assertEqual(self.checkout(form={}).status_code, 400)
        self.assertEqual(self.checkout("lifetime").status_code, 404)

    def test_stripe_being_down_is_a_clean_error_page(self):
        def boom(**kw):
            raise stripe.StripeError("down")
        self.stripe_stub("create", boom)
        self.assertEqual(self.checkout().status_code, 502)

    def test_shop_page_is_locked_until_stripe_is_set_up(self):
        on = main.index(stubs.make_request("/"))
        self.assertEqual(on.status_code, 200)
        self.assertNotIn("switched on yet", on.body.decode())      # Stripe and Luarmor are set in setUp
        self.assertNotIn("sold out", on.body.decode().lower())
        self.assertNotIn("disabled", on.body.decode())
        self.patch(main, "STRIPE_SECRET_KEY", "")
        html = main.index(stubs.make_request("/")).body.decode()
        self.assertIn("switched on yet", html)
        self.assertIn("disabled", html)

    def test_shop_page_says_sold_out_when_no_key_can_be_issued(self):
        self.patch(main, "luarmor", Luarmor("", ""))               # API unusable, and no stock
        html = main.index(stubs.make_request("/")).body.decode()
        self.assertIn("sold out", html.lower())
        self.assertNotIn("switched on yet", html)
        self.assertIn("disabled", html)

    def test_health_reports_luarmor(self):
        self.assertEqual(main.health(), {"ok": True, "checkout": True, "webhook": True, "luarmor": True,
                                         "api": True, "stock": []})
        self.patch(main, "luarmor", Luarmor("", ""))
        self.assertEqual(main.health(), {"ok": True, "checkout": False, "webhook": True, "luarmor": False,
                                         "api": False, "stock": []})

    def test_no_page_still_tells_buyers_to_use_claim_key(self):
        pages = [main.index(stubs.make_request("/")).body.decode(),
                 main.terms(stubs.make_request("/terms")).body.decode()]
        for path in (HERE.parent / "app" / "templates").glob("*.html"):
            pages.append(path.read_text())
        for page in pages:
            self.assertNotIn("claim_key", page)
            self.assertNotIn("/claim", page)


# ═══════════════════════════════════════════════════════════════════════════
class SuccessPageTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.retrieved = []
        self.stripe_stub("retrieve", lambda sid, api_key=None: self.retrieved.append(sid) or paid_session(sid))

    def test_buyer_sees_the_luarmor_key_and_the_luarmor_steps(self):
        resp = self.success_page()
        html = resp.body.decode()
        key = self.rows()[0]["key"]
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'class="key" id="key">{key}<', html)
        self.assertIn(f'data-copy="{key}"', html)
        self.assertIn("Redeem Key", html)
        self.assertIn("Get Script", html)
        self.assertNotIn("claim_key", html)
        self.assertNotIn("Copy command", html)
        self.assertEqual(resp.headers["cache-control"], "no-store")

    def test_opening_the_page_again_shows_the_same_key_without_asking_stripe_or_luarmor_again(self):
        first = self.success_page().body.decode()
        again = self.success_page().body.decode()
        self.assertEqual(self.retrieved, [SID])
        self.assertEqual(len(self.luarmor.created), 1)
        self.assertIn(self.rows()[0]["key"], again)
        self.assertIn("Order details", first)

    def test_page_when_luarmor_is_down_keeps_retrying_and_then_works(self):
        self.luarmor.fail = LuarmorError("Luarmor answered HTTP 403")
        resp = self.success_page()
        html = resp.body.decode()
        self.assertEqual(resp.status_code, 503)
        self.assertIn('http-equiv="refresh"', html)
        self.assertIn("Creating your key", html)
        self.assertIn(SID, html)                        # the order id to quote in a ticket
        self.assertEqual(self.rows(), [])
        self.luarmor.fail = None
        resp = self.success_page()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(self.rows()[0]["key"], resp.body.decode())
        self.assertEqual(len(self.luarmor.created), 1)

    def test_page_while_another_request_is_making_the_key_waits(self):
        self.insert_row(main.PENDING_PREFIX + "busy")
        resp = self.success_page()
        html = resp.body.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('http-equiv="refresh"', html)
        self.assertNotIn(main.PENDING_PREFIX, html)
        self.assertEqual(self.luarmor.created, [])

    def test_unpaid_order_waits_for_payment(self):
        self.stripe_stub("retrieve", lambda sid, api_key=None: {**paid_session(sid), "payment_status": "unpaid"})
        resp = self.success_page()
        self.assertIn("Waiting for payment", resp.body.decode())
        self.assertEqual(self.luarmor.created, [])

    def test_bad_session_ids_are_refused(self):
        for bad in ("", "nope", "cs_test_<script>", "cs_live_short"):
            with self.subTest(sid=bad):
                self.assertEqual(self.success_page(bad).status_code, 400)

    def test_a_disputed_key_is_shown_as_revoked(self):
        self.success_page()
        main.save_payment_intent(SID, PI)
        main.handle_dispute({"id": "dp_1", "payment_intent": PI})
        self.assertIn("has been revoked", self.success_page().body.decode())


# ═══════════════════════════════════════════════════════════════════════════
class WebhookTests(ShopCase):
    def test_payment_makes_a_luarmor_key_and_remembers_the_payment(self):
        status, body = self.webhook(self.completed())
        self.assertEqual((status, body), (200, {"received": True}))
        (row,) = self.rows()
        self.assertEqual(row["key"], self.luarmor.created[0][2])
        self.assertEqual(self.luarmor.created[0][0], 7)
        self.assertEqual(row["payment_intent"], PI)

    def test_stripe_delivering_the_event_twice_makes_one_key(self):
        self.webhook(self.completed())
        self.assertEqual(self.webhook(self.completed())[0], 200)
        self.assertEqual((len(self.luarmor.created), len(self.rows())), (1, 1))

    def test_delayed_payment_success_event_also_makes_the_key(self):
        event = {"type": "checkout.session.async_payment_succeeded", "data": {"object": paid_session()}}
        self.assertEqual(self.webhook(event)[0], 200)
        self.assertEqual(len(self.luarmor.created), 1)

    def test_luarmor_down_answers_500_so_stripe_retries_and_the_retry_works(self):
        self.luarmor.fail = LuarmorError("Luarmor answered HTTP 502")
        status, _ = self.webhook(self.completed())
        self.assertEqual(status, 500)
        self.assertEqual(self.rows(), [])
        self.luarmor.fail = None
        self.assertEqual(self.webhook(self.completed())[0], 200)
        self.assertEqual((len(self.luarmor.created), len(self.rows())), (1, 1))

    def test_unexpected_error_also_answers_500(self):
        self.patch(main, "FINISH_ORDER_SQL", "SELECT nonsense FROM nowhere")
        self.assertEqual(self.webhook(self.completed())[0], 500)

    def test_key_being_made_elsewhere_answers_500_to_try_later(self):
        self.insert_row(main.PENDING_PREFIX + "busy", created=main.utcnow())
        self.assertEqual(self.webhook(self.completed())[0], 500)
        self.assertEqual(self.luarmor.created, [])

    def test_bad_signature_is_rejected_and_nothing_is_made(self):
        def bad(payload, sig, secret):
            raise stripe.SignatureVerificationError("bad")
        self.patch(stripe.Webhook, "construct_event", staticmethod(bad))
        self.assertEqual(self.webhook(self.completed())[0], 400)
        self.assertEqual(self.luarmor.created, [])

    def test_webhook_without_a_secret_is_off(self):
        self.patch(main, "STRIPE_WEBHOOK_SECRET", "")
        self.assertEqual(self.webhook(self.completed())[0], 503)

    def test_unrelated_events_are_acknowledged_and_ignored(self):
        self.assertEqual(self.webhook({"type": "customer.created", "data": {"object": {}}})[0], 200)
        self.assertEqual(self.luarmor.created, [])


# ═══════════════════════════════════════════════════════════════════════════
class DisputeTests(ShopCase):
    def paid(self):
        self.assertEqual(self.webhook(self.completed())[0], 200)
        return self.rows()[0]["key"]

    def test_dispute_blacklists_the_key_on_luarmor_and_marks_it_revoked(self):
        key = self.paid()
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        (row,) = self.rows()
        self.assertEqual(self.luarmor.blacklisted, [(key, "Payment dispute")])
        self.assertIsNotNone(row["dispute_at"])
        self.assertIsNotNone(row["luarmor_banned_at"])
        self.assertEqual(row["claimed_by"], 0)

    def test_repeated_dispute_event_does_not_blacklist_twice(self):
        self.paid()
        self.webhook(self.dispute())
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(len(self.luarmor.blacklisted), 1)

    def test_luarmor_down_during_a_dispute_is_retried_by_stripe_until_it_works(self):
        key = self.paid()
        self.luarmor.fail = LuarmorError("Luarmor answered HTTP 500")
        self.assertEqual(self.webhook(self.dispute())[0], 500)
        (row,) = self.rows()
        self.assertIsNotNone(row["dispute_at"])           # the dispute itself is already recorded
        self.assertIsNone(row["luarmor_banned_at"])       # but the key is not yet banned
        self.luarmor.fail = None
        self.assertEqual(self.webhook(self.dispute())[0], 200)   # Stripe's retry
        self.assertEqual(self.luarmor.blacklisted, [(key, "Payment dispute")])
        self.assertIsNotNone(self.rows()[0]["luarmor_banned_at"])

    def test_redeemed_key_is_still_blacklisted_and_keeps_its_owner(self):
        key = self.paid()
        with main.engine.begin() as conn:
            conn.execute("UPDATE license_keys SET claimed_by = 424242")
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(self.luarmor.blacklisted, [(key, "Payment dispute")])
        self.assertEqual(self.rows()[0]["claimed_by"], 424242)

    def test_old_style_key_is_revoked_without_calling_luarmor(self):
        self.insert_row("TRK-W-AAAA-BBBB-CCCC", pi=PI)
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(self.luarmor.blacklisted, [])
        self.assertEqual(self.rows()[0]["claimed_by"], 0)

    def test_dispute_on_a_payment_we_never_sold_is_ignored(self):
        self.stripe_stub("list", lambda **kw: {"data": []})
        self.assertEqual(self.webhook(self.dispute(pi="pi_unknown"))[0], 200)
        self.assertEqual(self.luarmor.blacklisted, [])

    def test_dispute_before_the_key_exists_does_not_ban_a_placeholder(self):
        self.insert_row(main.PENDING_PREFIX + "busy", pi=PI)
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        self.assertEqual(self.luarmor.blacklisted, [])


# ═══════════════════════════════════════════════════════════════════════════
class EndToEndTests(ShopCase):
    """The shop with the real Luarmor client talking HTTP to a fake Luarmor server."""

    def setUp(self):
        super().setUp()
        self.http = HttpFakeLuarmor()
        self.addCleanup(self.http.close)
        self.http_key = "awIUiHenZzfScOsqkXwGHyRAOsTTcPMR"
        self.http.script = lambda req: (200, {"success": True, "message": "Success!", "user_key": self.http_key}, {})
        self.patch(main, "luarmor", Luarmor("e2e-api-key", "e2eproject123", self.http.url, timeout=2))
        self.stripe_stub("retrieve", lambda sid, api_key=None: paid_session(sid, plan="month"))

    def test_paid_month_order_reaches_luarmor_and_the_buyer_sees_luarmors_key(self):
        html = self.success_page().body.decode()
        (req,) = self.http.requests
        self.assertEqual((req["method"], req["path"]), ("POST", "/v3/projects/e2eproject123/users"))
        self.assertEqual(req["headers"]["Authorization"], "e2e-api-key")
        self.assertEqual(req["body"]["key_days"], 30)
        self.assertIn(self.http_key, html)
        self.assertEqual(self.rows()[0]["key"], self.http_key)

    def test_webhook_then_success_page_use_one_luarmor_key(self):
        order = paid_session(plan="week")
        self.assertEqual(self.webhook(self.completed(order))[0], 200)
        self.success_page()
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual(self.http.requests[0]["body"]["key_days"], 7)

    def test_dispute_blacklists_through_the_real_client(self):
        self.webhook(self.completed(paid_session(plan="week")))
        self.http.script = lambda req: (200, {"success": True, "message": "Blacklisted"}, {})
        self.assertEqual(self.webhook(self.dispute())[0], 200)
        req = self.http.requests[-1]
        self.assertEqual(req["path"], "/v3/projects/e2eproject123/users/blacklist")
        self.assertEqual(req["body"], {"user_key": self.http_key, "ban_reason": "Payment dispute"})

    def test_luarmor_refusing_the_api_key_never_hands_the_buyer_a_bad_key(self):
        self.http.script = lambda req: (401, {"success": False, "message": "Invalid API key"}, {})
        resp = self.success_page()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.rows(), [])
        self.assertNotIn("Invalid API key", resp.body.decode())   # internals stay out of the buyer's page


if __name__ == "__main__":
    unittest.main()
