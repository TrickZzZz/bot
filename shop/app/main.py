"""Tracker Access shop.

Stripe Checkout -> license key -> redeemed in Discord with /claim_key.
Shares the Postgres database (table: license_keys) with the Discord bot.

Environment:
  DATABASE_URL           required, same database the bot uses
  STRIPE_SECRET_KEY      sk_test_... / sk_live_... (checkout stays off until set)
  STRIPE_WEBHOOK_SECRET  whsec_... for POST /stripe/webhook
  SITE_URL               public base URL, e.g. https://keys.example.com (optional)
  SUPPORT_URL            Discord invite shown on the site (optional)
  MAIL_API_KEY           API key of the email service that sends each buyer their key (optional)
  MAIL_FROM              sender, e.g. "Tracker Access <keys@yourdomain.com>" (needed with MAIL_API_KEY)
  MAIL_PROVIDER          "resend" (default) or "brevo"
  MAIL_REPLY_TO          where replies go (optional)
  LOG_LEVEL              default INFO

Key emails stay off until MAIL_API_KEY and MAIL_FROM are both set.
For Stripe, send the checkout.session.completed and charge.dispute.created events to
/stripe/webhook. A disputed payment revokes its key; the Discord bot then removes the role.
"""
import hashlib
import html
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
from functools import cache
from pathlib import Path
from urllib.parse import parse_qs

import stripe
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

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

# Emailing each buyer their key. Off until a key and a sender are both set (see mail_enabled).
MAIL_API_KEY  = _env("MAIL_API_KEY")
MAIL_FROM     = _env("MAIL_FROM")
MAIL_REPLY_TO = _env("MAIL_REPLY_TO")
MAIL_PROVIDER = _env("MAIL_PROVIDER", "resend").lower()

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
    # Which Stripe payment bought the key, so a dispute on that payment can find it.
    payment_intent = Column(String(255), nullable=True)
    # Key email: when it went out, and when the last send was started (see claim_email_send).
    email_sent_at    = Column(DateTime(timezone=True), nullable=True)
    email_attempt_at = Column(DateTime(timezone=True), nullable=True)
    # Payment dispute: when Stripe told us, and when the bot finished ending the access.
    dispute_at         = Column(DateTime(timezone=True), nullable=True)
    dispute_handled_at = Column(DateTime(timezone=True), nullable=True)


# Columns the shop owns on license_keys (the bot's own copy of the table doesn't map them).
SHOP_COLUMNS = {
    "buyer_email":  "VARCHAR(320)",
    "pay_currency": "VARCHAR(8)",
    "pay_subtotal": "INTEGER",
    "pay_tax":      "INTEGER",
    "pay_discount": "INTEGER",
    "pay_total":    "INTEGER",
    "payment_intent":     "VARCHAR(255)",
    "email_sent_at":      "TIMESTAMPTZ",
    "email_attempt_at":   "TIMESTAMPTZ",
    "dispute_at":         "TIMESTAMPTZ",
    "dispute_handled_at": "TIMESTAMPTZ",
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
        # claimed_by == 0 is the bot's "revoked" marker (an owner, or a payment dispute), not a real Discord id.
        emailed = row.email_sent_at is not None and bool(row.buyer_email)
        return {"key": row.key, "plan": row.plan,
                "claimed": row.claimed_by is not None and row.claimed_by != 0,
                "revoked": row.claimed_by == 0,
                "paid_at": when_text(row.created_at), "details": order_details(row),
                "emailed": emailed, "email_hint": mask_email(row.buyer_email) if emailed else "",
                "mailable": bool(row.buyer_email) and row.claimed_by != 0}
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
    mail_problem = mail_config_problem()
    if mail_enabled():
        log.info("Key emails are on (%s)", MAIL_PROVIDER)
    elif mail_problem:
        log.warning("Key emails are off: %s", mail_problem)
    else:
        log.info("Key emails are off (set MAIL_API_KEY and MAIL_FROM to turn them on)")
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
                  checkout_ready=bool(STRIPE_SECRET_KEY), email_on=mail_enabled())


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
async def stripe_webhook(request: Request, background: BackgroundTasks):
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
        if key:
            # Neither of these can fail the webhook: the key already exists, and both log their own errors.
            await run_in_threadpool(save_payment_intent, order.get("id"), order.get("payment_intent"))
            background.add_task(send_key_email, order.get("id"), site_base(request))
    elif event.get("type") == "charge.dispute.created":
        dispute = event["data"]["object"]
        try:
            outcome = await run_in_threadpool(handle_dispute, dispute)
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
def success(request: Request, background: BackgroundTasks, session_id: str = ""):
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
        issued = fulfil(data)
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

    # The webhook normally sends the email. Opening this page sends it too if that hasn't
    # happened yet (a lost webhook, or a send that failed); an atomic claim stops a double send.
    if mail_enabled() and order["mailable"] and not order["emailed"]:
        background.add_task(send_key_email, session_id, site_base(request))

    resp = render(request, "success.html", 200, key=order["key"], order_id=session_id,
                  plan_name=PLANS[order["plan"]]["name"], claimed=order["claimed"],
                  revoked=order["revoked"], details=order["details"], paid_at=order["paid_at"],
                  emailed=order["emailed"], email_hint=order["email_hint"])
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/health")
@app.head("/health")
def health():
    return {"ok": True, "checkout": bool(STRIPE_SECRET_KEY), "webhook": bool(STRIPE_WEBHOOK_SECRET),
            "email": mail_enabled()}


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


def handle_dispute(dispute: dict) -> str:
    """Revoke the key a disputed payment bought. Returns "recorded", "duplicate" (Stripe sent
    the event again) or "unmatched" (not a payment for one of our keys). Raises if the database
    or Stripe can't be reached, so the webhook answers 500 and Stripe sends the event again."""
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
    if not recorded:
        return "duplicate"
    log.warning("Dispute %s recorded: key row %s is revoked and its access will end", did, key_id)
    return "recorded"


# ── Emailing the key ───────────────────────────────────────────────────────
# Sent through an email service's web API (Railway blocks the usual SMTP ports on smaller plans).
# Off until MAIL_API_KEY and MAIL_FROM are both set.
MAIL_PROVIDERS   = ("resend", "brevo")
MAIL_ENDPOINTS   = {"resend": "https://api.resend.com/emails",
                    "brevo":  "https://api.brevo.com/v3/smtp/email"}
MAIL_USER_AGENT  = "TrackerAccessShop/1.0"   # Resend is behind Cloudflare, which blocks Python's default agent
MAIL_TIMEOUT     = 15                        # seconds
MAIL_RETRY_AFTER = timedelta(minutes=2)      # a started send blocks another for this long
# Claiming the send is one statement, so the webhook and the success page can't both send it.
CLAIM_EMAIL_SQL = ("UPDATE license_keys SET email_attempt_at = :now "
                   "WHERE order_ref = :sid AND email_sent_at IS NULL "
                   "AND buyer_email IS NOT NULL AND buyer_email <> '' "
                   "AND (claimed_by IS NULL OR claimed_by <> 0) "
                   "AND (email_attempt_at IS NULL OR email_attempt_at < :cutoff)")
EMAIL_ORDER_SQL = 'SELECT "key", plan, buyer_email FROM license_keys WHERE order_ref = :sid'
MARK_EMAIL_SENT_SQL = ("UPDATE license_keys SET email_sent_at = :now "
                       "WHERE order_ref = :sid AND email_sent_at IS NULL")


class MailError(Exception):
    """The email service didn't take the message. The text is safe to log."""


def mail_config_problem() -> str:
    """Why emailing can't be on, in words for the log. Empty when it is on or was never asked for."""
    if not (MAIL_API_KEY or MAIL_FROM):
        return ""
    if MAIL_PROVIDER not in MAIL_PROVIDERS:
        return f'MAIL_PROVIDER "{MAIL_PROVIDER}" is not one of: {", ".join(MAIL_PROVIDERS)}'
    if not MAIL_API_KEY:
        return "MAIL_API_KEY is not set"
    if not MAIL_FROM:
        return "MAIL_FROM is not set"
    if "@" not in parseaddr(MAIL_FROM)[1]:
        return "MAIL_FROM must look like keys@yourdomain.com or Tracker Access <keys@yourdomain.com>"
    return ""


def mail_enabled() -> bool:
    return bool(MAIL_API_KEY and MAIL_FROM) and not mail_config_problem()


def mask_email(address) -> str:
    """'jane.doe@gmail.com' -> 'j***@gmail.com', so a page can say where a copy went."""
    if not isinstance(address, str):
        return ""
    local, at, domain = address.rpartition("@")
    return f"{local[0]}***@{domain}" if local and at and domain else ""


def build_key_email(key: str, plan_name: str, order_id: str, base: str, support_url: str):
    """(subject, plain text, html) for the email that carries a buyer's key."""
    confirm_url = f"{base}/success?session_id={order_id}"
    discord     = support_url if support_url.startswith(("https://", "http://")) else ""
    subject     = "Your Tracker Access key"

    open_discord = f"Open our Discord ({discord})." if discord else "Open our Discord."
    text_body = "\n".join([
        f"Thanks for your purchase. Here is your {plan_name} Tracker Access key:",
        "",
        key,
        "",
        "How to use it:",
        f"1. {open_discord}",
        "2. Run /claim_key and paste the key.",
        "Your time starts when you run the command, not when you paid.",
        "",
        f"Order ID: {order_id}",
        f"Your confirmation page, with the key and your order details: {confirm_url}",
        "",
        "If anything goes wrong, open a support ticket in the Discord and quote the order ID.",
        "All sales are final, including lost keys, so keep this email.",
    ])

    e = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    discord_html = f'<a href="{e(discord)}" style="color:#4f46e5;">our Discord</a>' if discord else "our Discord"
    html_body = (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#f4f4f5;'
        'font-family:Arial,Helvetica,sans-serif;color:#18181b;">'
        '<div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:8px;padding:28px;">'
        '<h1 style="font-size:20px;margin:0 0 12px;">Your Tracker Access key</h1>'
        f'<p style="margin:0 0 16px;line-height:1.5;">Thanks for your purchase. Here is your {e(plan_name)} key:</p>'
        '<p style="margin:0 0 20px;padding:14px;background:#f4f4f5;border-radius:6px;'
        'font-family:Consolas,Menlo,monospace;font-size:18px;letter-spacing:0.5px;word-break:break-all;">'
        f'{e(key)}</p>'
        '<ol style="margin:0 0 16px;padding-left:20px;line-height:1.7;">'
        f'<li>Open {discord_html}.</li>'
        '<li>Run <b>/claim_key</b> and paste the key.</li></ol>'
        '<p style="margin:0 0 20px;line-height:1.5;">Your time starts when you run the command, not when you paid.</p>'
        '<p style="margin:0;font-size:13px;line-height:1.6;color:#52525b;">'
        f'Order ID: <span style="font-family:Consolas,Menlo,monospace;">{e(order_id)}</span><br>'
        f'<a href="{e(confirm_url)}" style="color:#4f46e5;">Open your confirmation page</a><br>'
        'If anything goes wrong, open a support ticket in the Discord and quote the order ID.<br>'
        'All sales are final, including lost keys, so keep this email.</p>'
        '</div></body></html>'
    )
    return subject, text_body, html_body


def build_mail_request(to: str, subject: str, text_body: str, html_body: str, idem: str | None = None):
    """(url, headers, JSON body) for the configured email service."""
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": MAIL_USER_AGENT}
    if MAIL_PROVIDER == "brevo":
        name, address = parseaddr(MAIL_FROM)
        sender  = {"email": address, **({"name": name} if name else {})}
        payload = {"sender": sender, "to": [{"email": to}], "subject": subject,
                   "textContent": text_body, "htmlContent": html_body}
        if MAIL_REPLY_TO:
            payload["replyTo"] = {"email": parseaddr(MAIL_REPLY_TO)[1] or MAIL_REPLY_TO}
        headers["api-key"] = MAIL_API_KEY
    else:
        payload = {"from": MAIL_FROM, "to": [to], "subject": subject,
                   "text": text_body, "html": html_body}
        if MAIL_REPLY_TO:
            payload["reply_to"] = MAIL_REPLY_TO
        headers["Authorization"] = f"Bearer {MAIL_API_KEY}"
        if idem:
            headers["Idempotency-Key"] = idem
    return MAIL_ENDPOINTS.get(MAIL_PROVIDER, MAIL_ENDPOINTS["resend"]), headers, json.dumps(payload).encode("utf-8")


def send_mail(to: str, subject: str, text_body: str, html_body: str, idem: str | None = None) -> None:
    """Hand one message to the email service. Raises MailError if it isn't accepted."""
    url, headers, body = build_mail_request(to, subject, text_body, html_body, idem)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=MAIL_TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:   # before URLError: HTTPError is a kind of URLError
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise MailError(f"{MAIL_PROVIDER} refused the email: HTTP {exc.code} {detail}".strip()) from None
    except (urllib.error.URLError, OSError) as exc:
        raise MailError(f"could not reach {MAIL_PROVIDER}: {exc}") from None
    if not 200 <= status < 300:
        raise MailError(f"{MAIL_PROVIDER} answered HTTP {status}")


def claim_email_send(sid: str):
    """Take the right to email this order's key, or None if it was sent, is being sent, has no
    address, or was revoked. On success: {"key", "plan", "to"}."""
    now = utcnow()
    with engine.begin() as conn:
        taken = conn.execute(text(CLAIM_EMAIL_SQL),
                             {"now": now, "sid": sid, "cutoff": now - MAIL_RETRY_AFTER}).rowcount == 1
        row = conn.execute(text(EMAIL_ORDER_SQL), {"sid": sid}).first() if taken else None
    return {"key": row[0], "plan": row[1], "to": row[2]} if row is not None else None


def mark_email_sent(sid: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(MARK_EMAIL_SENT_SQL), {"now": utcnow(), "sid": sid})


def send_key_email(sid, base: str) -> str:
    """Email a paid order's key to the address it was bought with. Returns "off", "skipped",
    "sent" or "failed", and never raises, so it is safe to run after the response. A failed send
    is tried again by the next visit to the order's page once MAIL_RETRY_AFTER has passed.
    The key itself is never logged."""
    if not sid or not mail_enabled():
        return "off"
    try:
        order = claim_email_send(sid)
        if order is None:
            return "skipped"
        plan = PLANS.get(order["plan"])
        subject, text_body, html_body = build_key_email(
            order["key"], plan["name"] if plan else order["plan"], sid, base, SUPPORT_URL)
        send_mail(order["to"], subject, text_body, html_body, idem=f"key-email-{sid}")
        mark_email_sent(sid)
    except MailError as exc:
        log.error("Could not email the key for order %s: %s", sid, exc)
        return "failed"
    except Exception:
        log.exception("Could not email the key for order %s", sid)
        return "failed"
    log.info("Emailed the key for order %s", sid)
    return "sent"
