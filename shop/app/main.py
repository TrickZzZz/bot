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
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
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

# Prices are in US cents (Stripe's minimum charge is 50 cents). Must match the
# plan names the bot understands ("day", "week").
CURRENCY = "usd"
PLANS = {
    "day":  {"name": "1 day",  "label": "1-day key", "cents": 50,  "days": 1},
    "week": {"name": "7 days", "label": "7-day key", "cents": 200, "days": 7},
}
# Bump this date whenever the wording on /terms changes. It is saved on every payment.
TOS_VERSION = "2026-10-16"
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I lookalikes
SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")


def price_text(cents: int) -> str:
    return f"${cents / 100:.2f}"


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


def init_db():
    Base.metadata.create_all(bind=engine)
    # If the bot created the table first, make sure the email column exists.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE license_keys ADD COLUMN IF NOT EXISTS buyer_email VARCHAR(320)"))
    except Exception as e:  # SQLite has no IF NOT EXISTS for columns; fine for local runs
        log.info("buyer_email column check skipped: %s", e)


def generate_key(plan: str) -> str:
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(12))
    return f"TRK-{plan[0].upper()}-{body[:4]}-{body[4:8]}-{body[8:]}"


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

    for _ in range(3):
        db = SessionLocal()
        try:
            row = db.query(LicenseKey).filter(LicenseKey.order_ref == sid).first()
            if row:
                return row.key
            key = generate_key(plan_id)
            db.add(LicenseKey(key=key, plan=plan_id, order_ref=sid, buyer_email=email))
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
                "revoked": row.claimed_by == 0}
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
    plans = [{
        "id": pid, "name": p["name"], "label": p["label"], "days": p["days"],
        "price": price_text(p["cents"]),
        "per_day": price_text(round(p["cents"] / p["days"])),
    } for pid, p in PLANS.items()]
    return render(request, "index.html", plans=plans, checkout_ready=bool(STRIPE_SECRET_KEY))


@app.get("/terms")
def terms(request: Request):
    d = date.fromisoformat(TOS_VERSION)
    return render(request, "terms.html", updated=f"{d.day} {d:%B %Y}")


@app.post("/buy/{plan_id}")
async def buy(request: Request, plan_id: str):
    plan = PLANS.get(plan_id)
    if plan is None:
        return message(request, "Unknown key", "That key length doesn't exist. Pick one from the shop.", 404)
    if not STRIPE_SECRET_KEY:
        return message(request, "Checkout isn't ready",
                       "Payments aren't switched on yet. Check back soon.", 503)
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
                    "unit_amount": plan["cents"],
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

        data = {
            "id": sess["id"],
            "payment_status": sess["payment_status"],
            "metadata": dict(sess.get("metadata") or {}),
            "customer_details": dict(sess.get("customer_details") or {}),
        }
        if fulfil(data) is None:
            if data["payment_status"] == "paid":
                return message(request, "Key not created",
                               "Your payment went through but the key wasn't created. "
                               "Contact support with the email you paid with.", 500)
            return message(request, "Waiting for payment",
                           "Stripe hasn't confirmed this payment yet. This page checks again on its own.",
                           200, refresh=5)
        order = find_order(session_id)

    if order is None:
        return message(request, "Key not found", "Contact support with the email you paid with.", 500)

    resp = render(request, "success.html", 200, key=order["key"],
                  plan_name=PLANS[order["plan"]]["name"], claimed=order["claimed"],
                  revoked=order["revoked"])
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/health")
def health():
    return {"ok": True, "checkout": bool(STRIPE_SECRET_KEY), "webhook": bool(STRIPE_WEBHOOK_SECRET)}


@app.get("/robots.txt")
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /success\nDisallow: /stripe/\n")
