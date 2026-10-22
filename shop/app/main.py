"""Tracker Access shop.

Stripe Checkout -> Luarmor key -> redeemed with the Redeem Key button of the Luarmor panel
in Discord. A paid order gets a key of 1, 7 or 30 days that starts counting when the buyer
first redeems or runs it. The key comes from one of two places:
  * Stock: keys made by hand in the Luarmor dashboard and listed in the LUARMOR_STOCK_*
    variables. Each paid order gets the next unsold key of its plan. No Luarmor API call is
    needed, so no whitelisted IP either. A plan with no unsold keys is shown as sold out.
  * The Luarmor API (app/luarmor.py), only when LUARMOR_USE_API is on: makes a key when a
    plan has no stock left, and blacklists the key of a disputed payment.
Shares the Postgres database (table: license_keys) with the Discord bot. Stock is kept in
its own table, luarmor_stock.

Environment:
  DATABASE_URL           required, same database the bot uses
  STRIPE_SECRET_KEY      sk_test_... / sk_live_... (checkout stays off until set)
  STRIPE_WEBHOOK_SECRET  whsec_... for POST /stripe/webhook
  LUARMOR_STOCK_DAY      keys for the 1-day plan, separated by new lines, spaces or commas
  LUARMOR_STOCK_WEEK     keys for the 7-day plan (same format)
  LUARMOR_STOCK_MONTH    keys for the 30-day plan (same format)
  LUARMOR_USE_API        1 to let the shop call the Luarmor API (default off, see below)
  LUARMOR_API_KEY        Luarmor API key (used only when LUARMOR_USE_API is on)
  LUARMOR_PROJECT_ID     id of the Luarmor project (used only when LUARMOR_USE_API is on)
  LUARMOR_API_URL        default https://api.luarmor.net/v3 (only change it for testing)
  SITE_URL               public base URL, e.g. https://keys.example.com (optional)
  SUPPORT_URL            Discord invite shown on the site (optional)
  LOG_LEVEL              default INFO
  SCRIPT_ADMIN_TOKEN     password for /admin, where scripts are hosted (24+ characters; until it
                         is set, /admin and the upload routes answer as if they did not exist)
  LOADER_SECRET          key material for the hosted loader (24+ characters, optional; without it
                         SCRIPT_ADMIN_TOKEN is used). Changing it breaks nothing: each loader is made per request

Stock keys must be made in the Luarmor dashboard to last N days from first use, not until a
fixed date, or the time would run out before the buyer starts. The variables are read when the
shop starts: new keys are added, a key moved to another variable changes plan if it is unsold,
an unsold key removed from the variables is dropped, and a sold key is never touched.

Luarmor only accepts API calls from IP addresses whitelisted in its dashboard, so turn
LUARMOR_USE_API on only once the shop service has a static outbound IP (Railway: service
settings, Networking) that is added there. While it is off, a disputed payment's stock key has
to be blacklisted by hand: the shop logs a "BLACKLIST BY HAND" line with the key.

For Stripe, send the checkout.session.completed and charge.dispute.created events to
/stripe/webhook. A disputed payment revokes its key here and blacklists it on Luarmor.

Hosted scripts are separate from the shop: nothing on the storefront mentions them and they need
no key. A script uploaded on /admin has the loadstring
    loadstring(game:HttpGet("https://<site>/l/<token>"))()
That link serves a short loader (app/loader.py), not the script: the loader asks /p/<token> for the
script with a signed, one-time request, receives it encrypted and decrypts it in the executor, so
a browser, curl or a saved copy of the link never holds the script and a captured request works
once. Users type no key. The loader carries its link's secret, so someone who runs it can still
read the script after it is decrypted; the script's own obfuscation is what protects it from there.
The key material is LOADER_SECRET when it is set (24+ characters), otherwise SCRIPT_ADMIN_TOKEN.
The token is random, so only people who were given the link can find it, but anyone who has the
link can run the script. "New link" on /admin changes the token and the old link stops working.
A browser that opens the link is sent to the home page; the executor's HttpGet is not. That is a
courtesy, not security: curl can send any header.
Guessing is slowed per address (X-Real-IP, which Railway's edge sets, else the address uvicorn
reports): wrong passwords on /admin, 10 a minute and 30 an hour, and wrong links, 5 a minute and
20 an hour. SCRIPT_ADMIN_TOKEN needs 24+ characters, 10+ of them different, or /admin stays off
and the start-up log says why. The link is hidden in uvicorn's access log, and the log notes the
first admin sign-in from each address.
"""
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs

import stripe
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import loader
from .luarmor import DEFAULT_API_URL, KEY_RE, Luarmor, LuarmorError

# stdout, not the default stderr: Railway labels everything on stderr as an error, which
# made every normal "Issued a key" line look like a failure in the log view.
logging.basicConfig(
    stream=sys.stdout,
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shop")


class HideLinks(logging.Filter):
    """uvicorn's access log writes the whole request line, and the text after /l/ is the entire
    address of a hosted script: anyone who can read the platform's logs could run it. This swaps
    it for <hidden> before the line is written, whether it sits in the arguments or the message.
    The arguments keep their shape (uvicorn's own formatter unpacks five of them)."""
    LINK = re.compile(r"/l/[^\s?#\"/]+")

    def hide(self, value):
        return self.LINK.sub("/l/<hidden>", value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple):
                record.args = tuple(self.hide(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self.hide(v) for k, v in record.args.items()}
            record.msg = self.hide(record.msg)
        except Exception:                      # never let logging fail because of this
            pass
        return True


def hide_links_in_logs() -> None:
    """Put HideLinks on uvicorn's loggers, once however often this is called."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, HideLinks) for f in logger.filters):
            logger.addFilter(HideLinks())


hide_links_in_logs()

BASE_DIR = Path(__file__).resolve().parent


# ── Config ─────────────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def flag(name: str) -> bool:
    """An on/off variable: 1, true, yes or on (any letter case) is on; anything else, or unset, is off."""
    return _env(name).lower() in ("1", "true", "yes", "on")


DATABASE_URL = _env("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required.")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

STRIPE_SECRET_KEY     = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
SITE_URL              = _env("SITE_URL").rstrip("/")
SUPPORT_URL           = _env("SUPPORT_URL")
# Password of the /admin page (hosted scripts). Shorter than 24 characters counts as not set.
SCRIPT_ADMIN_TOKEN    = _env("SCRIPT_ADMIN_TOKEN")
# Key material for the hosted loader (app/loader.py). Optional: SCRIPT_ADMIN_TOKEN is used when it is not set.
LOADER_SECRET         = _env("LOADER_SECRET")
LUARMOR_API_KEY       = _env("LUARMOR_API_KEY")
LUARMOR_PROJECT_ID    = _env("LUARMOR_PROJECT_ID")
LUARMOR_API_URL       = _env("LUARMOR_API_URL", DEFAULT_API_URL)

# Off unless asked for: Luarmor refuses API calls from an address that isn't whitelisted, and a
# shop that can't whitelist one must not take payment for a key it would then fail to make.
LUARMOR_USE_API       = flag("LUARMOR_USE_API")

luarmor = Luarmor(LUARMOR_API_KEY, LUARMOR_PROJECT_ID, LUARMOR_API_URL)

STOCK_VARS = {"day": "LUARMOR_STOCK_DAY", "week": "LUARMOR_STOCK_WEEK", "month": "LUARMOR_STOCK_MONTH"}


def api_enabled() -> bool:
    """True when the shop may call the Luarmor API: switched on AND the API key and project id are set."""
    return LUARMOR_USE_API and luarmor.configured


def checkout_ready() -> bool:
    """Checkout only opens when Stripe can take the payment AND at least one key length
    can be issued, so nobody pays for a key the shop has no way to hand over."""
    return bool(STRIPE_SECRET_KEY) and bool(available_plans())

# Prices are in pence (Stripe's minimum GBP charge is 30p). Must match the
# plan names the bot understands ("day", "week", "month"), and "days" must match
# the length the bot gives that plan.
CURRENCY = "gbp"
PLANS = {
    "day":   {"name": "1 day",   "label": "1-day key",  "pence": 50,  "days": 1},
    "week":  {"name": "7 days",  "label": "7-day key",  "pence": 200, "days": 7},
    "month": {"name": "30 days", "label": "30-day key", "pence": 500, "days": 30},
}
DEFAULT_PLAN = "week"   # what the dropdown shows before anyone picks
# Bump this date whenever the wording on /terms changes. It is saved on every payment.
TOS_VERSION = "2026-10-16"
SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")


def price_text(pence: int) -> str:
    return money(pence, CURRENCY)


# ── Database (table definition mirrors bot/bot.py) ─────────────────────────
def utcnow():
    return datetime.now(timezone.utc)


engine       = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(bind=engine)
Base         = declarative_base()


class LicenseKey(Base):
    __tablename__ = "license_keys"
    id         = Column(Integer, primary_key=True)
    key        = Column(String(64), unique=True, nullable=False, index=True)
    plan       = Column(String(10), nullable=False)
    order_ref  = Column(String(200), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    claimed_by = Column(BigInteger, nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    buyer_email = Column(String(320), nullable=True)
    # What Stripe charged, in minor units (pence). Saved when the order is fulfilled.
    pay_currency = Column(String(8), nullable=True)
    pay_subtotal = Column(Integer, nullable=True)
    pay_tax      = Column(Integer, nullable=True)
    pay_discount = Column(Integer, nullable=True)
    pay_total    = Column(Integer, nullable=True)
    # Which Stripe payment bought the key, so a dispute on that payment can find it.
    payment_intent = Column(String(255), nullable=True)
    # Payment dispute: when Stripe told us, and when the bot finished ending the access.
    dispute_at         = Column(DateTime(timezone=True), nullable=True)
    dispute_handled_at = Column(DateTime(timezone=True), nullable=True)
    # When the key was blacklisted on Luarmor because of a dispute (stays empty until then).
    luarmor_banned_at  = Column(DateTime(timezone=True), nullable=True)


class LuarmorStock(Base):
    """Keys made by hand in the Luarmor dashboard, loaded from the LUARMOR_STOCK_* variables.
    A key with no order_ref is unsold; the unique order_ref is the Stripe session that bought it."""
    __tablename__ = "luarmor_stock"
    id          = Column(Integer, primary_key=True)
    key         = Column(String(64), unique=True, nullable=False)
    plan        = Column(String(10), nullable=False)
    added_at    = Column(DateTime(timezone=True), default=utcnow)
    order_ref   = Column(String(200), unique=True, nullable=True)
    assigned_at = Column(DateTime(timezone=True), nullable=True)


class HostedScript(Base):
    """A script served at /l/<token>. The random token is the whole address; `name` is only
    the label shown on /admin."""
    __tablename__ = "hosted_scripts"
    id          = Column(Integer, primary_key=True)
    name        = Column(String(32), unique=True, nullable=False)
    token       = Column(String(64), unique=True, nullable=False)
    body        = Column(Text, nullable=False)
    size        = Column(Integer, nullable=False)       # bytes, as uploaded
    sha256      = Column(String(64), nullable=False)    # of the uploaded bytes
    hits        = Column(Integer, nullable=False, default=0)
    created_at  = Column(DateTime(timezone=True), default=utcnow)
    updated_at  = Column(DateTime(timezone=True), default=utcnow)
    last_hit_at = Column(DateTime(timezone=True), nullable=True)


# Columns the shop owns on license_keys (the bot's own copy of the table doesn't map them).
SHOP_COLUMNS = {
    "buyer_email":  "VARCHAR(320)",
    "pay_currency": "VARCHAR(8)",
    "pay_subtotal": "INTEGER",
    "pay_tax":      "INTEGER",
    "pay_discount": "INTEGER",
    "pay_total":    "INTEGER",
    "payment_intent":     "VARCHAR(255)",
    "dispute_at":         "TIMESTAMPTZ",
    "dispute_handled_at": "TIMESTAMPTZ",
    "luarmor_banned_at":  "TIMESTAMPTZ",
}


def init_db():
    Base.metadata.create_all(bind=engine)
    # If the bot created the table first, add the shop's columns to it.
    for name, sql_type in SHOP_COLUMNS.items():
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE license_keys ADD COLUMN IF NOT EXISTS {name} {sql_type}"))
        except Exception as e:  # SQLite has no IF NOT EXISTS for columns; fine for local runs
            log.info("%s column check skipped: %s", name, e)
    try:
        sync_stock()
    except Exception:
        # Keys loaded on an earlier start stay in the table, so people who already paid still get theirs.
        log.exception("Loading the key stock from the LUARMOR_STOCK_* variables failed")


CURRENCY_SYMBOLS = {"usd": "$", "gbp": "£", "eur": "€"}


def money(cents: int, currency: str) -> str:
    """12345, 'usd' -> '$123.45'. Currencies without a symbol show as '123.45 CAD'."""
    cur    = (currency or CURRENCY).lower()
    amount = f"{abs(cents) / 100:,.2f}"
    symbol = CURRENCY_SYMBOLS.get(cur)
    shown  = f"{symbol}{amount}" if symbol else f"{amount} {cur.upper()}"
    return f"-{shown}" if cents < 0 else shown


def when_text(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.day} {dt:%b %Y}, {dt:%H:%M} UTC"


def _cents(value):
    """Stripe amounts are whole numbers. Anything else counts as 'not provided'."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def payment_fields(order: dict) -> dict:
    """The amounts on a Checkout Session, as the columns they are stored in."""
    totals   = order.get("total_details") or {}
    currency = order.get("currency")
    return {
        "pay_currency": currency.lower()[:8] if isinstance(currency, str) else None,
        "pay_subtotal": _cents(order.get("amount_subtotal")),
        "pay_tax":      _cents(totals.get("amount_tax")),
        "pay_discount": _cents(totals.get("amount_discount")),
        "pay_total":    _cents(order.get("amount_total")),
    }


def order_details(row):
    """Lines for the 'Order details' box. Discount and tax only appear when Stripe
    included them. None when no amounts were saved (keys sold before this existed)."""
    if row.pay_total is None:
        return None
    cur  = row.pay_currency
    rows = []
    if row.pay_subtotal is not None:
        rows.append(("Subtotal", money(row.pay_subtotal, cur)))
    if row.pay_discount:
        rows.append(("Discount", money(-row.pay_discount, cur)))
    if row.pay_tax:
        rows.append(("Tax / VAT", money(row.pay_tax, cur)))
    return {"rows": rows, "total": money(row.pay_total, cur),
            "currency": (cur or CURRENCY).upper()}


# ── Key stock ──────────────────────────────────────────────────────────────
# Keys made by hand in the Luarmor dashboard are listed in the LUARMOR_STOCK_* variables. At
# start-up they are copied into luarmor_stock, and a paid order takes the oldest unsold key of
# its plan. The unique order_ref on a stock row is the Stripe session that bought it, so asking
# again for the same order returns the same key and one key can never be sold twice.
# The SQL sits in constants so the tests can run these exact statements on a scratch database.
STOCK_ADD_SQL       = ('INSERT INTO luarmor_stock ("key", plan, added_at) VALUES (:key, :plan, :now) '
                       'ON CONFLICT ("key") DO NOTHING')
STOCK_REPLAN_SQL    = ('UPDATE luarmor_stock SET plan = :plan '
                       'WHERE "key" = :key AND order_ref IS NULL AND plan <> :plan')
STOCK_UNSOLD_SQL    = 'SELECT "key" FROM luarmor_stock WHERE order_ref IS NULL'
STOCK_DROP_SQL      = 'DELETE FROM luarmor_stock WHERE "key" = :key AND order_ref IS NULL'
STOCK_COUNTS_SQL    = 'SELECT plan, COUNT(*) FROM luarmor_stock WHERE order_ref IS NULL GROUP BY plan'
STOCK_FOR_ORDER_SQL = 'SELECT "key" FROM luarmor_stock WHERE order_ref = :sid'
STOCK_PICK_SQL      = ('SELECT id, "key" FROM luarmor_stock WHERE plan = :plan AND order_ref IS NULL '
                       'ORDER BY id LIMIT 1')
# The "order_ref IS NULL" is what makes two buyers racing for the same key safe: whoever commits
# second updates no row and goes round again for the next key.
STOCK_CLAIM_SQL     = ('UPDATE luarmor_stock SET order_ref = :sid, assigned_at = :now '
                       'WHERE id = :id AND order_ref IS NULL')
STOCK_TAKE_TRIES    = 8


class OutOfStock(Exception):
    """A paid order has no key to get: its plan has no unsold stock and the Luarmor API is off."""

    def __init__(self, plan_id: str):
        super().__init__(plan_id)
        self.plan = plan_id


def parse_stock(raw: str) -> list:
    """The items in a LUARMOR_STOCK_* value: separated by white space, commas or semicolons,
    with any quote marks pasted around a key ignored."""
    items = (item.strip("\"'") for item in re.split(r"[\s,;]+", raw or ""))
    return [item for item in items if item]


def read_stock_env() -> dict:
    """The stock the variables describe, as {key: plan}. An item that isn't shaped like a Luarmor
    key, or a key listed under two lengths, is left out and reported in the log (never with the
    key itself, because the log is not a safe place for keys)."""
    wanted, clashes = {}, set()
    for plan_id, var in STOCK_VARS.items():
        for number, item in enumerate(parse_stock(_env(var)), 1):
            if not KEY_RE.match(item):
                log.error("%s: item %d is not a Luarmor key (%d characters) and was skipped",
                          var, number, len(item))
            elif wanted.get(item, plan_id) != plan_id:
                clashes.add(item)
                log.error("%s: item %d is also listed under another key length, so it was skipped "
                          "everywhere (a key can only be one length)", var, number)
            else:
                wanted[item] = plan_id
    for item in clashes:
        wanted.pop(item, None)
    return wanted


def stock_fingerprint(keys) -> str:
    """A short fingerprint of a set of keys, whatever order they are in. The log shows it so the
    owner can check that exactly the intended keys were loaded without any key being in the log."""
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:10]


def sync_stock() -> dict:
    """Bring luarmor_stock in line with the variables: add new keys, move an unsold key whose
    variable changed, drop an unsold key that was removed. A sold key is never touched.
    Returns the counts of what changed."""
    wanted = read_stock_env()
    for plan_id, var in STOCK_VARS.items():
        keys = [key for key, plan in wanted.items() if plan == plan_id]
        log.info("%s: %d keys, fingerprint %s", var, len(keys), stock_fingerprint(keys))
    done = {"added": 0, "moved": 0, "dropped": 0}
    now = utcnow()
    with engine.begin() as conn:
        for key, plan_id in wanted.items():
            if conn.execute(text(STOCK_ADD_SQL), {"key": key, "plan": plan_id, "now": now}).rowcount == 1:
                done["added"] += 1
            elif conn.execute(text(STOCK_REPLAN_SQL), {"key": key, "plan": plan_id}).rowcount == 1:
                done["moved"] += 1
        for row in conn.execute(text(STOCK_UNSOLD_SQL)).fetchall():
            if row[0] not in wanted:
                conn.execute(text(STOCK_DROP_SQL), {"key": row[0]})
                done["dropped"] += 1
    log.info("Key stock loaded: %d added, %d moved to another length, %d removed",
             done["added"], done["moved"], done["dropped"])
    return done


def stock_counts() -> dict:
    """Unsold keys per plan, e.g. {"day": 20, "week": 3, "month": 0}."""
    counts = {plan_id: 0 for plan_id in PLANS}
    with engine.begin() as conn:
        for row in conn.execute(text(STOCK_COUNTS_SQL)).fetchall():
            if row[0] in counts:
                counts[row[0]] = int(row[1])
    return counts


def plans_in_stock() -> set:
    """Key lengths with at least one unsold stock key. Empty (and logged) if the database can't
    be read, so a broken database shows 'sold out' instead of taking payments."""
    try:
        return {plan_id for plan_id, left in stock_counts().items() if left > 0}
    except Exception:
        log.exception("Could not read the key stock; treating every key length as sold out")
        return set()


def available_plans() -> set:
    """Key lengths that can be bought right now: every length when the Luarmor API can make keys
    on demand, otherwise the lengths that still have stock."""
    return set(PLANS) if api_enabled() else plans_in_stock()


def take_stock_key(sid: str, plan_id: str):
    """The stock key that belongs to this order: the one it already took (so asking again gives
    the same key), otherwise the oldest unsold key of the plan. None when the plan has none left."""
    for _ in range(STOCK_TAKE_TRIES):
        with engine.begin() as conn:
            mine = conn.execute(text(STOCK_FOR_ORDER_SQL), {"sid": sid}).first()
            if mine is not None:
                return mine[0]
            pick = conn.execute(text(STOCK_PICK_SQL), {"plan": plan_id}).first()
            if pick is None:
                return None
            if conn.execute(text(STOCK_CLAIM_SQL),
                            {"sid": sid, "now": utcnow(), "id": pick[0]}).rowcount == 1:
                return pick[1]
        # Another buyer took that key between the two statements; look again for the next one.
    return None


# ── Issuing a key ──────────────────────────────────────────────────────────
# A paid order gets its row in license_keys first, holding a placeholder key, and only then is
# the real key fetched (a stock key, or a new one from Luarmor). The unique order_ref means
# exactly one request (the webhook or the success page, whichever is first) wins that insert and
# fetches the key, so a buyer can never end up with two keys or an unsold one left behind on Luarmor.
# The SQL sits in constants so the tests can run these exact statements on a scratch database.
PENDING_PREFIX     = "PENDING-"   # placeholder in license_keys.key while Luarmor is being asked
LEGACY_PREFIX      = "TRK-"       # keys the shop made itself before Luarmor (redeemed with /claim_key)
PENDING_STALE_SECS = 180          # a placeholder older than this belongs to a request that died

ORDER_ROW_SQL     = 'SELECT id, "key", pay_total, created_at FROM license_keys WHERE order_ref = :sid'
CLAIM_ORDER_SQL   = ('INSERT INTO license_keys ("key", plan, order_ref, created_at, buyer_email, '
                     'pay_currency, pay_subtotal, pay_tax, pay_discount, pay_total) '
                     'VALUES (:key, :plan, :sid, :now, :email, :cur, :sub, :tax, :disc, :total)')
TAKE_OVER_SQL     = 'UPDATE license_keys SET "key" = :new, created_at = :now WHERE id = :id AND "key" = :old'
FINISH_ORDER_SQL  = 'UPDATE license_keys SET "key" = :key WHERE id = :id AND "key" = :pending'
RELEASE_ORDER_SQL = 'DELETE FROM license_keys WHERE id = :id AND "key" = :pending'
SAVE_PAY_SQL      = ('UPDATE license_keys SET pay_currency = :cur, pay_subtotal = :sub, pay_tax = :tax, '
                     'pay_discount = :disc, pay_total = :total WHERE id = :id AND pay_total IS NULL')


class KeyPending(Exception):
    """Another request is making this order's key right now. Ask again in a moment."""


def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def new_placeholder() -> str:
    return PENDING_PREFIX + secrets.token_hex(16)


def pay_params(pay: dict) -> dict:
    return {"cur": pay["pay_currency"], "sub": pay["pay_subtotal"], "tax": pay["pay_tax"],
            "disc": pay["pay_discount"], "total": pay["pay_total"]}


def order_row(sid: str):
    with engine.begin() as conn:
        return conn.execute(text(ORDER_ROW_SQL), {"sid": sid}).first()


def claim_order(sid: str, plan_id: str, email, params: dict):
    """Insert the order's row with a placeholder key. Returns (row id, placeholder) when this
    call made the row, or None when another request got there first."""
    placeholder = new_placeholder()
    try:
        with engine.begin() as conn:
            conn.execute(text(CLAIM_ORDER_SQL), {"key": placeholder, "plan": plan_id, "sid": sid,
                                                 "now": utcnow(), "email": email, **params})
    except IntegrityError:
        return None
    row = order_row(sid)
    return (row[0], placeholder) if row is not None and row[1] == placeholder else None


def release_order(row_id: int, placeholder: str) -> None:
    """Give the order back (delete its placeholder row) so the next attempt starts clean."""
    try:
        with engine.begin() as conn:
            conn.execute(text(RELEASE_ORDER_SQL), {"id": row_id, "pending": placeholder})
    except Exception:
        log.exception("Could not release the placeholder for row %s; it expires by itself in %ss",
                      row_id, PENDING_STALE_SECS)


def discard_luarmor_key(key: str, sid: str) -> None:
    """Best effort: delete a key Luarmor made that no order is going to get."""
    try:
        luarmor.delete_key(key)
    except Exception as e:
        log.error("Order %s: a Luarmor key was made but not used, and deleting it failed (%s). "
                  "Delete this key by hand in the Luarmor dashboard: %s", sid, e, key)


def obtain_key(sid: str, plan_id: str):
    """The key for a paid order: its stock key if the plan has one, else a new key from the
    Luarmor API when that is switched on. Returns (key, made_just_now), where made_just_now is
    True for a key the API made for this call (the only kind that has to be deleted again if the
    order can't be completed). Raises OutOfStock, or LuarmorError when the API is tried and fails."""
    key = take_stock_key(sid, plan_id)
    if key is not None:
        return key, False
    if not api_enabled():
        raise OutOfStock(plan_id)
    return luarmor.create_key(PLANS[plan_id]["days"], f"shop-{plan_id}-{sid[-10:]}"), True


def fulfil(order: dict):
    """Give a paid Checkout Session its key (or return the one it already has). Safe to call
    repeatedly and from several places at once.

    Returns the key, or None for an order that isn't paid or has no valid plan. Raises
    KeyPending when another request is getting the key right now, OutOfStock when the plan has
    no stock and the Luarmor API is off, and LuarmorError when the API can't make the key; in
    all three cases nothing is left half done, so calling again is safe."""
    if order.get("payment_status") != "paid":
        return None
    sid     = order.get("id")
    plan_id = (order.get("metadata") or {}).get("plan")
    if not sid or plan_id not in PLANS:
        log.error("Paid order %s has no valid plan metadata (%r)", sid, plan_id)
        return None
    email  = (order.get("customer_details") or {}).get("email")
    params = pay_params(payment_fields(order))

    mine = None   # (row id, placeholder) once this call owns the job of asking Luarmor
    for _ in range(4):
        row = order_row(sid)
        if row is None:
            mine = claim_order(sid, plan_id, email, params)
            if mine:
                break
            continue                      # another request inserted it first; look again
        row_id, key, pay_total, created = row[0], row[1], row[2], row[3]
        if not key.startswith(PENDING_PREFIX):
            if pay_total is None and params["total"] is not None:
                try:  # keys issued before amounts were saved get them on the next event
                    with engine.begin() as conn:
                        conn.execute(text(SAVE_PAY_SQL), {"id": row_id, **params})
                except Exception:
                    log.exception("Could not save the payment details for %s", sid)
            return key
        age = (utcnow() - aware(created)).total_seconds() if created else PENDING_STALE_SECS
        if age < PENDING_STALE_SECS:
            raise KeyPending(sid)         # someone else is asking Luarmor right now
        taken = new_placeholder()         # that request died; take its job over
        with engine.begin() as conn:
            won = conn.execute(text(TAKE_OVER_SQL), {"new": taken, "now": utcnow(),
                                                     "id": row_id, "old": key}).rowcount == 1
        if won:
            log.warning("Order %s: took over a stale placeholder (%.0fs old)", sid, age)
            mine = (row_id, taken)
            break
    if mine is None:
        log.error("Could not take the key job for order %s after retries", sid)
        raise KeyPending(sid)

    row_id, placeholder = mine
    try:
        key, made_now = obtain_key(sid, plan_id)
    except Exception:
        release_order(row_id, placeholder)
        raise
    # A stock key stays assigned to this order (its order_ref) whatever happens next, so the next
    # attempt gets the same key back. A key the API just made has no such memory: it is deleted.
    try:
        with engine.begin() as conn:
            saved = conn.execute(text(FINISH_ORDER_SQL), {"key": key, "id": row_id,
                                                          "pending": placeholder}).rowcount == 1
    except Exception:
        release_order(row_id, placeholder)
        if made_now:
            discard_luarmor_key(key, sid)
        raise
    if not saved:
        # Getting the key was slow enough that another request took this order over. It owns the order now.
        log.error("Order %s: lost the placeholder while the key was being fetched", sid)
        if made_now:
            discard_luarmor_key(key, sid)
        raise KeyPending(sid)
    log.info("Issued a %s key for order %s (%s)", plan_id, sid, "Luarmor API" if made_now else "stock")
    return key


def find_order(session_id: str):
    db = SessionLocal()
    try:
        row = db.query(LicenseKey).filter(LicenseKey.order_ref == session_id).first()
        if row is None or row.key.startswith(PENDING_PREFIX):
            return None    # no key yet; the placeholder is not something to show a buyer
        # claimed_by == 0 is the bot's "revoked" marker (an owner, or a payment dispute), not a real Discord id.
        return {"key": row.key, "plan": row.plan,
                "claimed": row.claimed_by is not None and row.claimed_by != 0,
                "revoked": row.claimed_by == 0,
                "paid_at": when_text(row.created_at), "details": order_details(row)}
    finally:
        db.close()


# ── Tiny in-memory rate limits ─────────────────────────────────────────────
_hits: dict = {}          # counts over a minute (checkouts, link opens, wrong passwords)
_slow_hits: dict = {}     # counts over an hour, kept apart so tidying one store never forgets the other
RATE_TIDY_AT      = 5000      # forget expired counts once a store holds this many addresses
RATE_MAX_TRACKED  = 10_000    # and if it still holds more than this, drop the older half


def rate_limited(ip: str, limit: int = 8, window: float = 60.0, record: bool = True,
                 store: dict | None = None) -> bool:
    """True when `ip` has already used `limit` attempts inside the window. A call counts as an
    attempt unless record is False, which only looks. Every window has a store of its own."""
    store  = _hits if store is None else store
    now    = time.monotonic()
    recent = [t for t in store.get(ip, []) if now - t < window]
    if len(recent) >= limit:
        store[ip] = recent
        return True
    if not record:
        return False
    recent.append(now)
    store[ip] = recent
    if len(store) > RATE_TIDY_AT:  # keep memory bounded
        for k in [k for k, v in store.items() if not v or now - v[-1] >= window]:
            store.pop(k, None)
        if len(store) > RATE_MAX_TRACKED:
            for k in list(store)[: len(store) // 2]:
                store.pop(k, None)
    return False


def hourly_limited(key: str, limit: int, record: bool = True) -> bool:
    """rate_limited over an hour, in its own store."""
    return rate_limited(key, limit, 3600.0, record, _slow_hits)


_address_note_done = False


def client_ip(request: Request) -> str:
    """Who is asking, for the limits above. Railway's edge sets X-Real-IP to the visitor's address
    (its docs list it for that). A visitor can add an X-Forwarded-For of their own and uvicorn is
    told to trust that header, so the address it reports can be faked; X-Real-IP is used when it
    is a valid address, otherwise the address uvicorn reports. Once per start the log says whether
    the two agree, without printing either."""
    global _address_note_done
    reported = request.client.host if request.client else "unknown"
    try:
        real = str(ipaddress.ip_address(request.headers.get("x-real-ip", "").strip()))
    except ValueError:
        return reported
    if not _address_note_done:
        _address_note_done = True
        try:
            same = real == str(ipaddress.ip_address(reported))
        except ValueError:
            same = False
        log.info("Visitor address: X-Real-IP %s the address uvicorn reports", "matches" if same else "differs from")
    return real


# ── App ────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not STRIPE_SECRET_KEY:
        log.warning("STRIPE_SECRET_KEY is not set — checkout is disabled")
    if not STRIPE_WEBHOOK_SECRET:
        log.warning("STRIPE_WEBHOOK_SECRET is not set — webhook is disabled")
    if LUARMOR_USE_API and not luarmor.configured:
        log.warning("LUARMOR_USE_API is on but LUARMOR_API_KEY / LUARMOR_PROJECT_ID are not (both) set, "
                    "or the project id is malformed — the Luarmor API will not be used")
    note = admin_setup_note()
    if note:
        log.warning(note)
    if LOADER_SECRET and len(LOADER_SECRET) < loader.MIN_SECRET_LEN:
        log.warning("LOADER_SECRET is shorter than %d characters, so it is not used for the hosted loader", loader.MIN_SECRET_LEN)
    try:
        counts = stock_counts()
    except Exception:
        log.exception("Could not read the key stock at start-up")
        counts = {}
    log.info("Unsold stock keys: %s. Luarmor API: %s",
             ", ".join(f"{plan_id} {left}" for plan_id, left in counts.items()) or "unknown",
             "on" if api_enabled() else "off")
    if not api_enabled() and not any(counts.values()):
        log.warning("No stock keys and the Luarmor API is off — nothing can be sold. "
                    "Set %s and redeploy.", " / ".join(STOCK_VARS.values()))
    yield


app = FastAPI(title="Tracker Access shop", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@cache
def asset(path: str) -> str:
    """URL for a file under /static with a short hash of its contents on the end.
    When a file changes its URL changes, so a browser can never pair a new page with
    an old cached copy of its stylesheet or script. Files don't change while the
    server is running, so each hash is worked out once."""
    try:
        digest = hashlib.sha256((BASE_DIR / "static" / path).read_bytes()).hexdigest()[:10]
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={digest}"


templates.env.globals["asset"] = asset

CSP = ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; script-src 'self'; "
       "form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'; "
       "base-uri 'none'")


VERSION_RE = re.compile(r"^[0-9a-f]{6,64}$")


def static_cache_control(version: str | None) -> str:
    """Files asked for with the content hash that asset() adds (?v=...) can be kept for a
    year: when a file changes its URL changes too. Anything without a hash is checked
    with the server on every visit (a cheap 304 when nothing changed)."""
    if version and VERSION_RE.match(version):
        return "public, max-age=31536000, immutable"
    return "no-cache"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/static/") and resp.status_code in (200, 304):
        resp.headers.setdefault("Cache-Control", static_cache_control(request.query_params.get("v")))
    return resp


def site_base(request: Request) -> str:
    return SITE_URL or str(request.base_url).rstrip("/")


def render(request: Request, name: str, status: int = 200, **ctx):
    ctx.setdefault("support_url", SUPPORT_URL)
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def message(request: Request, title: str, body: str, status: int = 200, refresh: int = 0):
    return render(request, "message.html", status, title=title, body=body, refresh=refresh)


async def read_form(request: Request, limit: int = 4096) -> dict:
    """Read a small urlencoded form body without extra dependencies. Anything bigger
    than `limit` bytes is treated as empty."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return {}
        chunks.append(chunk)
    parsed = parse_qs(b"".join(chunks).decode("utf-8", "replace"))
    return {k: v[-1] for k, v in parsed.items()}


@app.get("/")
@app.head("/")
def index(request: Request):
    # The plan that costs least per day gets the "Best value" tag.
    best = min(PLANS, key=lambda pid: PLANS[pid]["pence"] / PLANS[pid]["days"])
    # Only lengths that can be bought go in the dropdown; with none left they all show, locked.
    for_sale = available_plans()
    shown = [pid for pid in PLANS if pid in for_sale] or list(PLANS)
    plans = [{
        "id": pid, "name": PLANS[pid]["name"], "label": PLANS[pid]["label"], "days": PLANS[pid]["days"],
        "price": price_text(PLANS[pid]["pence"]),
        "per_day": price_text(round(PLANS[pid]["pence"] / PLANS[pid]["days"])),
        "best": pid == best and len(PLANS) > 1,
    } for pid in shown]
    default_plan = DEFAULT_PLAN if DEFAULT_PLAN in shown else shown[0]
    sold_out = [PLANS[pid]["name"] for pid in PLANS if pid not in for_sale]
    return render(request, "index.html", plans=plans, default_plan=default_plan,
                  checkout_ready=bool(STRIPE_SECRET_KEY) and bool(for_sale),
                  stripe_ready=bool(STRIPE_SECRET_KEY), sold_out=sold_out)


@app.get("/terms")
@app.head("/terms")
def terms(request: Request):
    d = date.fromisoformat(TOS_VERSION)
    return render(request, "terms.html", updated=f"{d.day} {d:%B %Y}")


@app.post("/buy/{plan_id}")
async def buy(request: Request, plan_id: str):
    """Start checkout for the key length named in the URL."""
    return await start_checkout(request, plan_id)


@app.post("/buy")
async def buy_selected(request: Request):
    """Start checkout for the key length picked in the dropdown (the form's `plan` field)."""
    form = await read_form(request)
    return await start_checkout(request, form.get("plan", ""), form)


async def start_checkout(request: Request, plan_id: str, form: dict | None = None):
    plan = PLANS.get(plan_id)
    if plan is None:
        return message(request, "Unknown key", "That key length doesn't exist. Pick one from the shop.", 404)
    if not STRIPE_SECRET_KEY:
        return message(request, "Checkout isn't ready",
                       "Payments aren't switched on yet. Check back soon.", 503)
    for_sale = await run_in_threadpool(available_plans)
    if not for_sale:
        return message(request, "Sold out",
                       "Every key is sold out right now. Check back soon.", 503)
    if plan_id not in for_sale:
        return message(request, "Sold out",
                       f"The {plan['name']} key is sold out right now. Pick another length or check back soon.", 409)
    if form is None:
        form = await read_form(request)
    if form.get("agree") != "yes":
        return message(request, "Agree to the terms first",
                       "Tick the box to accept the terms of service, then choose your key again.", 400)
    ip = client_ip(request)
    if rate_limited(ip):
        return message(request, "Too many attempts",
                       "Wait a minute, then start checkout again.", 429)

    base = site_base(request)
    try:
        session = await run_in_threadpool(
            stripe.checkout.Session.create,
            api_key=STRIPE_SECRET_KEY,
            mode="payment",
            line_items=[{
                "quantity": 1,
                "price_data": {
                    "currency": CURRENCY,
                    "unit_amount": plan["pence"],
                    "product_data": {
                        "name": f"Tracker Access: {plan['name']}",
                        "description": "Redeem it with the Redeem Key button in our Discord",
                    },
                },
            }],
            success_url=f"{base}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/#keys",
            # The terms version and time are kept on the payment as a record of consent.
            metadata={"plan": plan_id,
                      "tos_version": TOS_VERSION,
                      "tos_accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        )
    except stripe.StripeError:
        log.exception("Creating a Checkout Session failed")
        return message(request, "Couldn't start checkout",
                       "Stripe didn't respond. Try again in a moment.", 502)
    return RedirectResponse(session["url"], status_code=303)


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        log.error("Webhook hit but STRIPE_WEBHOOK_SECRET is not set")
        return JSONResponse({"error": "webhook not configured"}, status_code=503)

    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    try:
        stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    except stripe.SignatureVerificationError:
        log.warning("Webhook with a bad signature rejected")
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    event = json.loads(payload)
    if event.get("type") in ("checkout.session.completed",
                             "checkout.session.async_payment_succeeded"):
        order = event["data"]["object"]
        try:
            key = await run_in_threadpool(fulfil, order)
        except KeyPending:
            log.info("Order %s: its key is being made by another request; Stripe will ask again",
                     order.get("id"))
            return JSONResponse({"error": "key is being made"}, status_code=500)  # Stripe retries
        except OutOfStock as e:
            log.error("Order %s is paid but no %s key is left. Add keys to %s and redeploy; "
                      "Stripe will retry and the buyer's page keeps checking",
                      order.get("id"), e.plan, STOCK_VARS[e.plan])
            return JSONResponse({"error": "out of stock"}, status_code=500)  # Stripe retries
        except LuarmorError as e:
            log.error("Order %s: Luarmor couldn't make the key (%s); Stripe will retry", order.get("id"), e)
            return JSONResponse({"error": "key service unavailable"}, status_code=500)  # Stripe retries
        except Exception:
            log.exception("Fulfilment failed for %s", order.get("id"))
            return JSONResponse({"error": "fulfilment failed"}, status_code=500)  # Stripe retries
        log.info("Webhook %s for %s: %s", event.get("type"), order.get("id"),
                 "key ready" if key else "no key (unpaid or invalid)")
        if key:
            # Can't fail the webhook: the key already exists, and this logs its own errors.
            await run_in_threadpool(save_payment_intent, order.get("id"), order.get("payment_intent"))
    elif event.get("type") == "charge.dispute.created":
        dispute = event["data"]["object"]
        try:
            outcome = await run_in_threadpool(handle_dispute, dispute)
        except LuarmorError as e:
            log.error("Dispute %s: the key could not be blacklisted on Luarmor (%s); Stripe will retry",
                      dispute.get("id"), e)
            return JSONResponse({"error": "key service unavailable"}, status_code=500)  # Stripe retries
        except Exception:
            log.exception("Handling dispute %s failed", dispute.get("id"))
            return JSONResponse({"error": "dispute handling failed"}, status_code=500)  # Stripe retries
        log.info("Webhook charge.dispute.created for %s: %s", dispute.get("id"), outcome)
    return {"received": True}


def plain(obj):
    """Newer stripe-python objects aren't dicts (obj.get() raises), so convert to plain data."""
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v) for v in obj]
    return obj


@app.get("/success")
def success(request: Request, session_id: str = ""):
    if not SESSION_ID_RE.match(session_id):
        return message(request, "Order not found",
                       "That confirmation link isn't valid. Use the link Stripe sent you back to.", 400)

    order = find_order(session_id)
    if order is None:
        # The webhook can lag behind the redirect, so confirm the payment with Stripe directly.
        if not STRIPE_SECRET_KEY:
            return message(request, "Checkout isn't ready", "Payments aren't switched on yet.", 503)
        try:
            sess = stripe.checkout.Session.retrieve(session_id, api_key=STRIPE_SECRET_KEY)
        except stripe.InvalidRequestError:
            return message(request, "Order not found", "We couldn't find that order.", 404)
        except stripe.StripeError:
            log.exception("Retrieving Checkout Session %s failed", session_id)
            return message(request, "Still checking",
                           "Stripe didn't answer. This page tries again on its own.", 502, refresh=5)

        raw = plain(sess)
        data = {
            "id": raw.get("id"),
            "payment_status": raw.get("payment_status"),
            "metadata": raw.get("metadata") or {},
            "customer_details": raw.get("customer_details") or {},
            "amount_subtotal": raw.get("amount_subtotal"),
            "amount_total": raw.get("amount_total"),
            "currency": raw.get("currency"),
            "total_details": raw.get("total_details") or {},
            "payment_intent": raw.get("payment_intent"),
        }
        try:
            issued = fulfil(data)
        except KeyPending:
            return message(request, "Creating your key",
                           "Your payment went through and your key is being created. "
                           "This page checks again on its own.", 200, refresh=3)
        except OutOfStock as e:
            log.error("Order %s is paid but no %s key is left. Add keys to %s and redeploy",
                      session_id, e.plan, STOCK_VARS[e.plan])
            return message(request, "Restocking",
                           "Your payment went through, but we have just run out of this key length. "
                           "Your key will appear on this page as soon as we restock, and this page "
                           "checks again on its own. If it is still here after a while, open a "
                           f"support ticket in the Discord with this order ID: {session_id}",
                           503, refresh=30)
        except LuarmorError as e:
            log.error("Order %s: Luarmor couldn't make the key (%s)", session_id, e)
            return message(request, "Creating your key",
                           "Your payment went through but the key isn't ready yet. This page keeps "
                           "trying. If it is still here after a few minutes, open a support ticket "
                           f"in the Discord with this order ID: {session_id}", 503, refresh=8)
        if issued is not None:
            save_payment_intent(session_id, data["payment_intent"])
        if issued is None:
            if data["payment_status"] == "paid":
                return message(request, "Key not created",
                               "Your payment went through but the key wasn't created. "
                               "Open a support ticket in the Discord with the email you paid "
                               f"with and this order ID: {session_id}", 500)
            return message(request, "Waiting for payment",
                           "Stripe hasn't confirmed this payment yet. This page checks again on its own.",
                           200, refresh=5)
        order = find_order(session_id)

    if order is None:
        return message(request, "Key not found",
                       "Open a support ticket in the Discord with the email you paid "
                       f"with and this order ID: {session_id}", 500)

    resp = render(request, "success.html", 200, key=order["key"], order_id=session_id,
                  plan_name=PLANS[order["plan"]]["name"], claimed=order["claimed"],
                  revoked=order["revoked"], details=order["details"], paid_at=order["paid_at"])
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/health")
@app.head("/health")
def health():
    in_stock = plans_in_stock()
    return {"ok": True, "checkout": checkout_ready(), "webhook": bool(STRIPE_WEBHOOK_SECRET),
            "luarmor": luarmor.configured, "api": api_enabled(),
            "stock": [plan_id for plan_id in PLANS if plan_id in in_stock]}


@app.get("/robots.txt")
@app.head("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /success\nDisallow: /stripe/\n")


@app.get("/favicon.ico", include_in_schema=False)
@app.head("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers and link checkers ask for /favicon.ico whatever the page's <link> says."""
    return FileResponse(BASE_DIR / "static" / "favicon.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


# ── Hosted scripts ─────────────────────────────────────────────────────────
# Nothing on the storefront links here; see the module docstring. The SQL sits in constants so the
# tests run these exact statements on a scratch database.
SCRIPT_NAME_RE         = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SCRIPT_TOKEN_RE        = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SCRIPT_MAX_BYTES       = 8 * 1024 * 1024
SCRIPT_FETCHES_PER_MIN = 30     # per address; a loader runs once, so this only slows scraping
ADMIN_TOKEN_MIN        = 24
ADMIN_TOKEN_DISTINCT_MIN = 10   # ...and this many different characters, so "aaaa…" or "abab…" can't be it
ADMIN_FAILS_PER_MIN    = 10     # wrong passwords per address before it is told to wait
ADMIN_FAILS_PER_HOUR   = 30     # ...and per hour, which stops a guesser who goes slowly
ADMIN_ADDRESSES_KEPT   = 50     # sign-in addresses remembered for the "new address" note
LINK_MISSES_PER_MIN    = 5      # wrong links per address before it is told to wait
LINK_MISSES_PER_HOUR   = 20
NO_STORE               = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
BROWSER_DESTINATIONS   = {"document", "iframe", "frame", "embed", "object"}   # Sec-Fetch-Dest of a page load

SCRIPT_SERVE_SQL  = ("UPDATE hosted_scripts SET hits = hits + 1, last_hit_at = :now "
                     "WHERE token = :token RETURNING body")
SCRIPT_EXISTS_SQL = "SELECT 1 FROM hosted_scripts WHERE name = :name"
SCRIPT_FIND_SQL   = "SELECT 1 FROM hosted_scripts WHERE token = :token"   # a loader request: no visit counted yet
NONCES            = loader.NonceStore()                                  # one-time numbers handed to loaders
# A new name gets the token passed in; an existing name keeps its own, so the link people
# already have keeps working after the script is replaced.
SCRIPT_SAVE_SQL   = ("INSERT INTO hosted_scripts (name, token, body, size, sha256, hits, created_at, updated_at) "
                     "VALUES (:name, :token, :body, :size, :sha, 0, :now, :now) "
                     "ON CONFLICT (name) DO UPDATE SET body = excluded.body, size = excluded.size, "
                     "sha256 = excluded.sha256, updated_at = excluded.updated_at "
                     "RETURNING token")
SCRIPT_LIST_SQL   = ("SELECT name, token, size, sha256, hits, updated_at, last_hit_at "
                     "FROM hosted_scripts ORDER BY name")
SCRIPT_ROTATE_SQL = "UPDATE hosted_scripts SET token = :token, updated_at = :now WHERE name = :name"
SCRIPT_DELETE_SQL = "DELETE FROM hosted_scripts WHERE name = :name"


def admin_password_problem(value: str):
    """None when `value` may be the admin password, otherwise why not, in words that never quote it."""
    if len(value) < ADMIN_TOKEN_MIN:
        return f"shorter than {ADMIN_TOKEN_MIN} characters"
    if len(set(value)) < ADMIN_TOKEN_DISTINCT_MIN:
        return f"made of fewer than {ADMIN_TOKEN_DISTINCT_MIN} different characters"
    return None


def admin_enabled() -> bool:
    return admin_password_problem(SCRIPT_ADMIN_TOKEN) is None


def admin_setup_note():
    """A start-up warning when SCRIPT_ADMIN_TOKEN is set but can't be used, otherwise None.
    Not setting it at all is fine and says nothing. The password itself is never quoted."""
    problem = admin_password_problem(SCRIPT_ADMIN_TOKEN) if SCRIPT_ADMIN_TOKEN else None
    if problem is None:
        return None
    return (f"SCRIPT_ADMIN_TOKEN is {problem}, so /admin stays switched off. "
            "Set a longer random value and redeploy.")


def script_loadstring(base: str, token: str) -> str:
    return f'loadstring(game:HttpGet("{base}/l/{token}"))()'


def size_text(size: int) -> str:
    return f"{size / 1024:,.0f} KB" if size >= 1024 else f"{size} bytes"


def serve_script(token: str):
    """The script's text, counting the visit. None when no script has that token."""
    with engine.begin() as conn:
        row = conn.execute(text(SCRIPT_SERVE_SQL), {"token": token, "now": utcnow()}).first()
    return row[0] if row else None


def script_exists(token: str) -> bool:
    """True when a script has this token. The visit is counted later, when the script itself is sent."""
    with engine.begin() as conn:
        return conn.execute(text(SCRIPT_FIND_SQL), {"token": token}).first() is not None


def save_script(name: str, body: str, size: int, sha: str):
    """Store a script under `name`. Returns (token, created)."""
    with engine.begin() as conn:
        existed = conn.execute(text(SCRIPT_EXISTS_SQL), {"name": name}).first() is not None
        row = conn.execute(text(SCRIPT_SAVE_SQL), {
            "name": name, "token": secrets.token_urlsafe(24), "body": body,
            "size": size, "sha": sha, "now": utcnow()}).first()
    return row[0], not existed


def list_scripts() -> list:
    with engine.begin() as conn:
        return [tuple(r) for r in conn.execute(text(SCRIPT_LIST_SQL)).fetchall()]


def rotate_script(name: str):
    """Give a script a new random token. Returns it, or None when no script has that name."""
    token = secrets.token_urlsafe(24)
    with engine.begin() as conn:
        changed = conn.execute(text(SCRIPT_ROTATE_SQL),
                               {"name": name, "token": token, "now": utcnow()}).rowcount
    return token if changed else None


def delete_script(name: str) -> bool:
    with engine.begin() as conn:
        return bool(conn.execute(text(SCRIPT_DELETE_SQL), {"name": name}).rowcount)


async def read_capped(request: Request, limit: int):
    """The request body, or None when it is longer than `limit` bytes."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def admin_password(request: Request):
    """(password, from_browser) taken from the Authorization header: Basic auth, with any user
    name and the admin token as the password (what the browser's sign-in box sends), or
    'Bearer <token>' for a script. ('', False) when nothing usable was sent."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    scheme, value = scheme.lower(), value.strip()
    if scheme == "bearer":
        return value, False
    if scheme == "basic":
        try:
            return base64.b64decode(value, validate=True).decode("utf-8").partition(":")[2], True
        except (binascii.Error, UnicodeDecodeError):
            return "", True
    return "", False


def admin_check(request: Request, mutating: bool = False):
    """None when the request may use the admin routes, otherwise the response to send instead.
    With no SCRIPT_ADMIN_TOKEN set the routes don't exist (404). A browser's sign-in is only
    trusted for changes when the page's own script sent X-Admin-Request, which another
    website's form or request can't add."""
    if not admin_enabled():
        raise StarletteHTTPException(status_code=404)
    ip  = client_ip(request)
    key = f"admin:{ip}"
    if hourly_limited(key, ADMIN_FAILS_PER_HOUR, record=False):
        return PlainTextResponse("Too many wrong passwords. Try again in an hour.", status_code=429,
                                 headers={"Retry-After": "3600", **NO_STORE})
    if rate_limited(key, ADMIN_FAILS_PER_MIN, record=False):
        return PlainTextResponse("Too many wrong passwords. Wait a minute.", status_code=429,
                                 headers={"Retry-After": "60", **NO_STORE})
    given, from_browser = admin_password(request)
    signin = {"WWW-Authenticate": 'Basic realm="Hosted scripts", charset="UTF-8"', **NO_STORE}
    if not given:
        return PlainTextResponse("Sign in with the admin password (any user name).",
                                 status_code=401, headers=signin)
    if not hmac.compare_digest(given.encode(), SCRIPT_ADMIN_TOKEN.encode()):
        rate_limited(key, ADMIN_FAILS_PER_MIN)      # count this wrong password
        hourly_limited(key, ADMIN_FAILS_PER_HOUR)
        log.warning("Wrong admin password from %s", ip)
        return PlainTextResponse("Wrong password.", status_code=401, headers=signin)
    if mutating and from_browser and request.headers.get("x-admin-request") != "1":
        return PlainTextResponse("Refused.", status_code=403, headers=NO_STORE)
    note_admin_address(ip)
    return None


_admin_seen_addresses: set = set()


def note_admin_address(ip: str) -> None:
    """Log the first sign-in from each address, so one that wasn't yours stands out. The list is
    cleared when it is full, which makes an address count as new again rather than go unnoted."""
    if ip in _admin_seen_addresses:
        return
    if len(_admin_seen_addresses) >= ADMIN_ADDRESSES_KEPT:
        _admin_seen_addresses.clear()
    _admin_seen_addresses.add(ip)
    log.info("Admin signed in from a new address: %s", ip)


def admin_error(message_text: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message_text}, status_code=status, headers=NO_STORE)


def opened_as_a_page(request: Request) -> bool:
    """True when a person opened the link in a browser tab rather than a game executor running
    the loadstring. A browser says so itself: every page it opens carries Sec-Fetch-Mode: navigate
    and Sec-Fetch-Dest: document (or a frame), and older ones ask for text/html under a Mozilla
    user agent. An HttpGet sends none of that. A client that asks for text/html but doesn't call
    itself a browser still gets the script, so an executor with odd headers isn't turned away.
    Headers can be faked by anyone running curl, so this keeps curious people off the source; it
    doesn't stop someone who is determined."""
    headers = request.headers
    if headers.get("sec-fetch-mode", "").lower() == "navigate":
        return True
    if headers.get("sec-fetch-dest", "").lower() in BROWSER_DESTINATIONS:
        return True
    return "text/html" in headers.get("accept", "").lower() and "mozilla" in headers.get("user-agent", "").lower()


@app.get("/l/{token}")
async def script_file(request: Request, token: str):
    """Stage 1 of the hosted script (app/loader.py), for loadstring(game:HttpGet(...))(): a short
    loader that fetches and decrypts the script itself. A browser that opens the link is sent to
    the home page instead, whether or not the link is real, so the address shows nothing to
    someone who only pastes it into a tab."""
    if opened_as_a_page(request):
        return RedirectResponse("/", status_code=302, headers=NO_STORE)
    ip = client_ip(request)
    if rate_limited(f"l:{ip}", SCRIPT_FETCHES_PER_MIN):
        return PlainTextResponse("Too many requests. Wait a minute.", status_code=429,
                                 headers={"Retry-After": "60", **NO_STORE})
    misses = f"lmiss:{ip}"           # wrong links: an address that keeps guessing waits, even for the right one
    if hourly_limited(misses, LINK_MISSES_PER_HOUR, record=False):
        return PlainTextResponse("Too many wrong links. Try again in an hour.", status_code=429,
                                 headers={"Retry-After": "3600", **NO_STORE})
    if rate_limited(misses, LINK_MISSES_PER_MIN, record=False):
        return PlainTextResponse("Too many wrong links. Wait a minute.", status_code=429,
                                 headers={"Retry-After": "60", **NO_STORE})
    master = loader.master_key(LOADER_SECRET, SCRIPT_ADMIN_TOKEN)
    found = bool(master) and bool(SCRIPT_TOKEN_RE.fullmatch(token)) and await run_in_threadpool(script_exists, token)
    if not found:
        rate_limited(misses, LINK_MISSES_PER_MIN)
        hourly_limited(misses, LINK_MISSES_PER_HOUR)
        return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    nonce = NONCES.issue(token)
    try:
        stage = loader.stage_one(site_base(request), token, nonce, loader.link_secret(master, token))
    except ValueError:
        log.error("The hosted loader was not sent: the site address is not in a form it can use (check SITE_URL)")
        return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    return Response(content=stage, media_type="text/plain", headers=NO_STORE)


@app.get("/p/{token}")
async def script_payload(request: Request, token: str):
    """Stage 2: the script, encrypted, for one signed request from a loader this site handed out.
    The signature must match the link's secret and the one-time number must be unused, unexpired
    and issued for this link. Anything else is the same plain 404, whatever was wrong."""
    if opened_as_a_page(request):
        return RedirectResponse("/", status_code=302, headers=NO_STORE)
    not_found = PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    nonce = request.query_params.get("n", "")
    given = request.query_params.get("s", "")
    master = loader.master_key(LOADER_SECRET, SCRIPT_ADMIN_TOKEN)
    if not (master and SCRIPT_TOKEN_RE.fullmatch(token) and loader.NONCE_RE.fullmatch(nonce) and loader.SIG_RE.fullmatch(given)):
        return not_found
    secret = loader.link_secret(master, token)
    if not loader.signature_ok(secret, token, nonce, given) or not NONCES.take(token, nonce):
        return not_found
    body = await run_in_threadpool(serve_script, token)
    if body is None:
        return not_found
    payload = await run_in_threadpool(loader.encrypt, secret, nonce, body.encode("utf-8"))
    return Response(content=payload, media_type="text/plain", headers=NO_STORE)


@app.get("/admin")
async def admin_page(request: Request):
    denied = admin_check(request)
    if denied:
        return denied
    base = site_base(request)
    scripts = [{"name": name, "line": script_loadstring(base, token), "size": size_text(size),
                "sha": sha[:12], "hits": hits, "updated": when_text(updated),
                "last": when_text(last) or "never"}
               for name, token, size, sha, hits, updated, last in await run_in_threadpool(list_scripts)]
    resp = render(request, "admin.html", scripts=scripts)
    resp.headers.update(NO_STORE)
    return resp


@app.put("/admin/scripts/{name}")
async def admin_upload(request: Request, name: str):
    """Save the request body as the script `name` (a new name makes a new link)."""
    denied = admin_check(request, mutating=True)
    if denied:
        return denied
    if not SCRIPT_NAME_RE.fullmatch(name):
        return admin_error("The name can use a-z, 0-9, - and _, up to 32 characters.", 400)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    raw = None if declared > SCRIPT_MAX_BYTES else await read_capped(request, SCRIPT_MAX_BYTES)
    if raw is None:
        return admin_error(f"That file is over {SCRIPT_MAX_BYTES // (1024 * 1024)} MB.", 413)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        return admin_error("That file isn't UTF-8 text.", 400)
    if not body.strip() or "\x00" in body:
        return admin_error("That file is empty or isn't text.", 400)
    sha = hashlib.sha256(raw).hexdigest()
    token, created = await run_in_threadpool(save_script, name, body, len(raw), sha)
    log.info("Hosted script %s %s: %d bytes, sha256 %s", name, "added" if created else "replaced",
             len(raw), sha[:12])
    return JSONResponse({"name": name, "created": created, "bytes": len(raw), "sha256": sha,
                         "loadstring": script_loadstring(site_base(request), token)}, headers=NO_STORE)


@app.post("/admin/scripts/{name}/rotate")
async def admin_rotate(request: Request, name: str):
    """Give the script a new link; the old link stops working at once."""
    denied = admin_check(request, mutating=True)
    if denied:
        return denied
    token = await run_in_threadpool(rotate_script, name) if SCRIPT_NAME_RE.fullmatch(name) else None
    if token is None:
        return admin_error("No script has that name.", 404)
    log.info("Hosted script %s got a new link", name)
    return JSONResponse({"name": name, "loadstring": script_loadstring(site_base(request), token)},
                        headers=NO_STORE)


@app.delete("/admin/scripts/{name}")
async def admin_delete(request: Request, name: str):
    denied = admin_check(request, mutating=True)
    if denied:
        return denied
    if not (SCRIPT_NAME_RE.fullmatch(name) and await run_in_threadpool(delete_script, name)):
        return admin_error("No script has that name.", 404)
    log.info("Hosted script %s removed", name)
    return JSONResponse({"name": name, "removed": True}, headers=NO_STORE)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """A visitor who lands on a wrong address or a link that needs the Buy button gets a
    page from the shop, not a bare JSON error. Files under /static, Stripe's webhook and hosted
    script links (/l/) keep the plain response, and every other error is handled as before."""
    path = request.url.path
    if exc.status_code in (404, 405) and not path.startswith(("/static/", "/stripe/", "/l/")):
        if exc.status_code == 404:
            resp = message(request, "Page not found",
                           "That page doesn't exist. Head back to the shop to choose a key.", 404)
        else:
            resp = message(request, "Can't open that here",
                           "That address only works from the shop's Buy button. Head back and choose your key.", 405)
        for name, value in (exc.headers or {}).items():  # keeps the Allow header on a 405
            resp.headers[name] = value
        return resp
    return await http_exception_handler(request, exc)


# ── Payment disputes ───────────────────────────────────────────────────────
# A customer who disputes a payment with their bank loses the key it bought. The shop marks
# the key (dispute_at); the Discord bot then removes the role from whoever redeemed it.
# The SQL sits in constants so the tests can run these exact statements on a scratch database.
SAVE_PAYMENT_INTENT_SQL = ("UPDATE license_keys SET payment_intent = :pi "
                           "WHERE order_ref = :sid AND payment_intent IS NULL")
KEY_BY_PAYMENT_SQL      = "SELECT id FROM license_keys WHERE payment_intent = :pi ORDER BY id LIMIT 1"
KEY_BY_ORDER_SQL        = "SELECT id FROM license_keys WHERE order_ref = :sid"
BACKFILL_PAYMENT_SQL    = ("UPDATE license_keys SET payment_intent = :pi "
                           "WHERE id = :id AND payment_intent IS NULL")
# One statement, so a /claim_key racing a dispute can't slip through: an unclaimed key is
# marked revoked (claimed_by 0, the same marker /revoke uses) and a claimed one is left for the
# bot to end. Only the first dispute on a key is recorded.
RECORD_DISPUTE_SQL = ("UPDATE license_keys SET dispute_at = :now, "
                      "claimed_by = CASE WHEN claimed_by IS NULL THEN 0 ELSE claimed_by END, "
                      "claimed_at = CASE WHEN claimed_by IS NULL THEN :now ELSE claimed_at END "
                      "WHERE id = :id AND dispute_at IS NULL")


def save_payment_intent(sid, payment_intent) -> None:
    """Remember which Stripe payment bought a key, so a dispute on that payment can find it.
    The key already exists when this runs, so a failure is logged and nothing more."""
    if not sid or not isinstance(payment_intent, str) or not payment_intent.startswith("pi_"):
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(SAVE_PAYMENT_INTENT_SQL), {"pi": payment_intent[:255], "sid": sid})
    except Exception:
        log.exception("Could not save the payment reference for %s", sid)


def key_id_for_payment(payment_intent: str):
    """The row id of the key a payment bought, or None. Keys sold before payment references
    were saved are found through Stripe, and get their reference saved on the way."""
    with engine.begin() as conn:
        row = conn.execute(text(KEY_BY_PAYMENT_SQL), {"pi": payment_intent}).first()
    if row is not None:
        return row[0]
    if not STRIPE_SECRET_KEY:
        return None
    found    = plain(stripe.checkout.Session.list(payment_intent=payment_intent, limit=1,
                                                  api_key=STRIPE_SECRET_KEY))
    sessions = found.get("data") or []
    sid      = sessions[0].get("id") if sessions else None
    if not sid:
        return None
    with engine.begin() as conn:
        row = conn.execute(text(KEY_BY_ORDER_SQL), {"sid": sid}).first()
        if row is None:
            return None
        conn.execute(text(BACKFILL_PAYMENT_SQL), {"pi": payment_intent, "id": row[0]})
    return row[0]


KEY_BAN_STATE_SQL = 'SELECT "key", luarmor_banned_at FROM license_keys WHERE id = :id'
MARK_BANNED_SQL   = "UPDATE license_keys SET luarmor_banned_at = :now WHERE id = :id AND luarmor_banned_at IS NULL"


def ban_on_luarmor(key_id: int, dispute_id=None) -> str:
    """Blacklist a disputed order's key on Luarmor so it stops loading the script. Returns what
    happened ("banned", "already banned", "not a Luarmor key", "blacklist by hand", "no key").
    Raises LuarmorError when Luarmor can't be told; the dispute stays recorded and the next
    Stripe retry tries again. With the Luarmor API off the shop can't do it at all, so it logs
    the key for the owner to blacklist in the Luarmor dashboard and leaves the key unmarked."""
    with engine.begin() as conn:
        row = conn.execute(text(KEY_BAN_STATE_SQL), {"id": key_id}).first()
    if row is None:
        return "no key"
    key, banned_at = row[0], row[1]
    if banned_at is not None:
        return "already banned"
    if key.startswith((LEGACY_PREFIX, PENDING_PREFIX)):
        return "not a Luarmor key"      # the Discord bot ends access for the old keys
    if not api_enabled():
        log.error("BLACKLIST BY HAND: payment dispute %s on key %s. The Luarmor API is off, so "
                  "blacklist this key in the Luarmor dashboard.", dispute_id or "?", key)
        return "blacklist by hand"
    luarmor.blacklist_key(key, "Payment dispute")
    with engine.begin() as conn:
        conn.execute(text(MARK_BANNED_SQL), {"now": utcnow(), "id": key_id})
    return "banned"


def handle_dispute(dispute: dict) -> str:
    """Revoke the key a disputed payment bought. Returns "recorded", "duplicate" (Stripe sent
    the event again) or "unmatched" (not a payment for one of our keys). Raises if the database,
    Stripe or Luarmor can't be reached, so the webhook answers 500 and Stripe sends the event again."""
    did = dispute.get("id")
    pi  = dispute.get("payment_intent")
    if not isinstance(pi, str) or not pi:
        log.warning("Dispute %s names no payment, so no key can be matched to it", did)
        return "unmatched"
    key_id = key_id_for_payment(pi)
    if key_id is None:
        log.warning("Dispute %s on payment %s matches no key sold here", did, pi)
        return "unmatched"
    with engine.begin() as conn:
        recorded = conn.execute(text(RECORD_DISPUTE_SQL),
                                {"now": utcnow(), "id": key_id}).rowcount == 1
    # Also on a repeat event: if telling Luarmor failed last time, this is the retry that finishes it.
    banned = ban_on_luarmor(key_id, did)
    if not recorded:
        log.info("Dispute %s repeated for key row %s (Luarmor: %s)", did, key_id, banned)
        return "duplicate"
    log.warning("Dispute %s recorded: key row %s is revoked (Luarmor: %s)", did, key_id, banned)
    return "recorded"
