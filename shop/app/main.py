"""Tracker Access shop.

Stripe Checkout -> license key -> redeemed in Discord with /claim_key.
Shares the Postgres database (table: license_keys) with the Discord bot.

Environment:
  DATABASE_URL           required, same database the bot uses
  STRIPE_SECRET_KEY      sk_test_... / sk_live_... (checkout stays off until set)
  STRIPE_WEBHOOK_SECRET  whsec_... for POST /stripe/webhook
  SITE_URL               public base URL, e.g. https://keys.example.com (optional)
  SUPPORT_URL            Discord invite shown on the site (optional)
  LOG_LEVEL              default INFO
"""
import hashlib
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs

import stripe
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.concurrency import run_in_threadpool

logging.basicConfig(
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
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I lookalikes
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


# Columns the shop owns on license_keys (the bot's own copy of the table doesn't map them).
SHOP_COLUMNS = {
    "buyer_email":  "VARCHAR(320)",
    "pay_currency": "VARCHAR(8)",
    "pay_subtotal": "INTEGER",
    "pay_tax":      "INTEGER",
    "pay_discount": "INTEGER",
    "pay_total":    "INTEGER",
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


def generate_key(plan: str) -> str:
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(12))
    return f"TRK-{plan[0].upper()}-{body[:4]}-{body[4:8]}-{body[8:]}"


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


def fulfil(order: dict):
    """Create (or return) the key for a paid Checkout Session. Safe to call repeatedly."""
    if order.get("payment_status") != "paid":
        return None
    sid     = order.get("id")
    plan_id = (order.get("metadata") or {}).get("plan")
    if not sid or plan_id not in PLANS:
        log.error("Paid order %s has no valid plan metadata (%r)", sid, plan_id)
        return None
    email = (order.get("customer_details") or {}).get("email")
    pay   = payment_fields(order)

    for _ in range(3):
        db = SessionLocal()
        try:
            row = db.query(LicenseKey).filter(LicenseKey.order_ref == sid).first()
            if row:
                existing = row.key
                if row.pay_total is None and pay["pay_total"] is not None:
                    try:  # keys issued before amounts were saved get them on the next event
                        for name, value in pay.items():
                            setattr(row, name, value)
                        db.commit()
                    except Exception:
                        db.rollback()
                        log.exception("Could not save the payment details for %s", sid)
                return existing
            key = generate_key(plan_id)
            db.add(LicenseKey(key=key, plan=plan_id, order_ref=sid, buyer_email=email, **pay))
            db.commit()
            log.info("Issued a %s key for order %s", plan_id, sid)
            return key
        except IntegrityError:
            db.rollback()  # raced the webhook or the success page; look again
        finally:
            db.close()
    log.error("Could not issue a key for order %s after retries", sid)
    return None


def find_order(session_id: str):
    db = SessionLocal()
    try:
        row = db.query(LicenseKey).filter(LicenseKey.order_ref == session_id).first()
        if row is None:
            return None
        # claimed_by == 0 is the bot's "revoked by an owner" marker, not a real Discord id.
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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith("/static/"):
        # Check with the server on every visit (a cheap 304 when nothing changed)
        # instead of letting a browser guess how long a file stays fresh.
        resp.headers.setdefault("Cache-Control", "no-cache")
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
                  checkout_ready=bool(STRIPE_SECRET_KEY))


@app.get("/terms")
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
                        "description": "Redeem in Discord with /claim_key",
                    },
                },
            }],
            success_url=f"{base}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/",
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
        except Exception:
            log.exception("Fulfilment failed for %s", order.get("id"))
            return JSONResponse({"error": "fulfilment failed"}, status_code=500)  # Stripe retries
        log.info("Webhook %s for %s: %s", event.get("type"), order.get("id"),
                 "key ready" if key else "no key (unpaid or invalid)")
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
        }
        if fulfil(data) is None:
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
def health():
    return {"ok": True, "checkout": bool(STRIPE_SECRET_KEY), "webhook": bool(STRIPE_WEBHOOK_SECRET)}


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /success\nDisallow: /stripe/\n")
