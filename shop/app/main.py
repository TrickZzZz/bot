"""Tracker Access shop.

Stripe Checkout -> Luarmor key -> redeemed with the Redeem Key button of the Luarmor panel
in Discord. The key is made on Luarmor (app/luarmor.py) the moment a payment clears, with
a length of 1, 7 or 30 days that starts counting when the buyer first redeems or runs it.
Shares the Postgres database (table: license_keys) with the Discord bot.

Environment:
  DATABASE_URL           required, same database the bot uses
  STRIPE_SECRET_KEY      sk_test_... / sk_live_... (checkout stays off until set)
  STRIPE_WEBHOOK_SECRET  whsec_... for POST /stripe/webhook
  LUARMOR_API_KEY        Luarmor API key (checkout stays off until set)
  LUARMOR_PROJECT_ID     id of the Luarmor project the keys belong to (checkout stays off until set)
  LUARMOR_API_URL        default https://api.luarmor.net/v3 (only change it for testing)
  SITE_URL               public base URL, e.g. https://keys.example.com (optional)
  SUPPORT_URL            Discord invite shown on the site (optional)
  LOG_LEVEL              default INFO

Luarmor only accepts API calls from IP addresses whitelisted in its dashboard, so the shop
service needs a static outbound IP (Railway: service settings, Networking) that is added there.

For Stripe, send the checkout.session.completed and charge.dispute.created events to
/stripe/webhook. A disputed payment blacklists its key on Luarmor and marks it revoked here.
"""
import hashlib
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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .luarmor import DEFAULT_API_URL, Luarmor, LuarmorError

# stdout, not the default stderr: Railway labels everything on stderr as an error, which
# made every normal "Issued a key" line look like a failure in the log view.
logging.basicConfig(
    stream=sys.stdout,
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shop")

BASE_DIR = Path(__file__).resolve().parent


# ── Config ─────────────────────────────────────────────────────────────────
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DATABASE_URL = _env("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required.")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

STRIPE_SECRET_KEY     = _env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
SITE_URL              = _env("SITE_URL").rstrip("/")
SUPPORT_URL           = _env("SUPPORT_URL")
LUARMOR_API_KEY       = _env("LUARMOR_API_KEY")
LUARMOR_PROJECT_ID    = _env("LUARMOR_PROJECT_ID")
LUARMOR_API_URL       = _env("LUARMOR_API_URL", DEFAULT_API_URL)

luarmor = Luarmor(LUARMOR_API_KEY, LUARMOR_PROJECT_ID, LUARMOR_API_URL)


def checkout_ready() -> bool:
    """Checkout only opens when Stripe can take the payment AND Luarmor can make the key,
    so nobody pays for a key the shop has no way to issue."""
    return bool(STRIPE_SECRET_KEY) and luarmor.configured

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


# ── Issuing a Luarmor key ──────────────────────────────────────────────────
# A paid order gets its row in license_keys first, holding a placeholder key, and only then is
# Luarmor asked for the real key. The unique order_ref means exactly one request (the webhook
# or the success page, whichever is first) wins that insert and makes the Luarmor key, so a
# buyer can never end up with two keys or an unsold one left behind on Luarmor.
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


def fulfil(order: dict):
    """Create (or return) the Luarmor key for a paid Checkout Session. Safe to call repeatedly
    and from several places at once.

    Returns the key, or None for an order that isn't paid or has no valid plan. Raises
    KeyPending when another request is making the key right now, and LuarmorError when Luarmor
    can't make it; in both cases nothing is left half done, so calling again is safe."""
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
        key = luarmor.create_key(PLANS[plan_id]["days"], f"shop-{plan_id}-{sid[-10:]}")
    except Exception:
        release_order(row_id, placeholder)
        raise
    try:
        with engine.begin() as conn:
            saved = conn.execute(text(FINISH_ORDER_SQL), {"key": key, "id": row_id,
                                                          "pending": placeholder}).rowcount == 1
    except Exception:
        release_order(row_id, placeholder)
        discard_luarmor_key(key, sid)
        raise
    if not saved:
        # Luarmor was slow enough that another request took this order over. It owns the order now.
        log.error("Order %s: lost the placeholder while Luarmor was answering", sid)
        discard_luarmor_key(key, sid)
        raise KeyPending(sid)
    log.info("Issued a %s Luarmor key for order %s", plan_id, sid)
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


# ── Tiny in-memory rate limit for starting checkouts ───────────────────────
_hits: dict = {}


def rate_limited(ip: str, limit: int = 8, window: float = 60.0) -> bool:
    now    = time.monotonic()
    recent = [t for t in _hits.get(ip, []) if now - t < window]
    if len(recent) >= limit:
        _hits[ip] = recent
        return True
    recent.append(now)
    _hits[ip] = recent
    if len(_hits) > 5000:  # keep memory bounded
        for k in [k for k, v in _hits.items() if not v or now - v[-1] >= window]:
            _hits.pop(k, None)
    return False


# ── App ────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not STRIPE_SECRET_KEY:
        log.warning("STRIPE_SECRET_KEY is not set — checkout is disabled")
    if not STRIPE_WEBHOOK_SECRET:
        log.warning("STRIPE_WEBHOOK_SECRET is not set — webhook is disabled")
    if not luarmor.configured:
        log.warning("LUARMOR_API_KEY / LUARMOR_PROJECT_ID are not (both) set, or the project id "
                    "is malformed — checkout is disabled because no key could be issued")
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
    plans = [{
        "id": pid, "name": p["name"], "label": p["label"], "days": p["days"],
        "price": price_text(p["pence"]),
        "per_day": price_text(round(p["pence"] / p["days"])),
        "best": pid == best and len(PLANS) > 1,
    } for pid, p in PLANS.items()]
    default_plan = DEFAULT_PLAN if DEFAULT_PLAN in PLANS else next(iter(PLANS))
    return render(request, "index.html", plans=plans, default_plan=default_plan,
                  checkout_ready=checkout_ready())


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
    if not checkout_ready():
        return message(request, "Checkout isn't ready",
                       "Payments aren't switched on yet. Check back soon.", 503)
    if form is None:
        form = await read_form(request)
    if form.get("agree") != "yes":
        return message(request, "Agree to the terms first",
                       "Tick the box to accept the terms of service, then choose your key again.", 400)
    ip = request.client.host if request.client else "unknown"
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
    return {"ok": True, "checkout": checkout_ready(), "webhook": bool(STRIPE_WEBHOOK_SECRET),
            "luarmor": luarmor.configured}


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


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """A visitor who lands on a wrong address or a link that needs the Buy button gets a
    page from the shop, not a bare JSON error. Files under /static and Stripe's webhook keep
    the plain response, and every other error is handled as before."""
    path = request.url.path
    if exc.status_code in (404, 405) and not path.startswith(("/static/", "/stripe/")):
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


def ban_on_luarmor(key_id: int) -> str:
    """Blacklist a disputed order's key on Luarmor so it stops loading the script. Returns what
    happened ("banned", "already banned", "not a Luarmor key", "no key"). Raises LuarmorError when
    Luarmor can't be told; the dispute stays recorded and the next Stripe retry tries again."""
    with engine.begin() as conn:
        row = conn.execute(text(KEY_BAN_STATE_SQL), {"id": key_id}).first()
    if row is None:
        return "no key"
    key, banned_at = row[0], row[1]
    if banned_at is not None:
        return "already banned"
    if key.startswith((LEGACY_PREFIX, PENDING_PREFIX)):
        return "not a Luarmor key"      # the Discord bot ends access for the old keys
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
    banned = ban_on_luarmor(key_id)
    if not recorded:
        log.info("Dispute %s repeated for key row %s (Luarmor: %s)", did, key_id, banned)
        return "duplicate"
    log.warning("Dispute %s recorded: key row %s is revoked (Luarmor: %s)", did, key_id, banned)
    return "recorded"
