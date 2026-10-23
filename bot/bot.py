import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timedelta, timezone
import asyncio
import os
import io
import secrets
import time
import json
import re
from dotenv import load_dotenv
from sqlalchemy import (create_engine, Column, Integer, BigInteger, Boolean,
                        String, DateTime, Text, update, delete, or_,
                        text as sql_text)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, declarative_base
from cryptography.fernet import Fernet, InvalidToken

load_dotenv()

TOKEN              = os.environ.get("TOKEN")
VOICE_CHANNEL_ID   = int(os.environ.get("VOICE_CHANNEL_ID", "1521166172182024475"))
PANEL_CHANNEL_ID   = 1521157085771993260
PANEL_MSG_PATH     = os.path.join(os.path.dirname(__file__), "panel_message_id.json")

def load_panel_msg_id() -> int | None:
    try:
        with open(PANEL_MSG_PATH) as f:
            return int(json.load(f).get("message_id", 0)) or None
    except Exception:
        return None

def save_panel_msg_id(msg_id: int):
    try:
        with open(PANEL_MSG_PATH, "w") as f:
            json.dump({"message_id": msg_id}, f)
    except Exception:
        pass

# ── Encryption ────────────────────────────────────────────────────────────
ENCRYPTION_KEY = os.environ.get("ACCOUNT_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError("ACCOUNT_ENCRYPTION_KEY environment variable is required.")
_fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

def decrypt_secret(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return "could not decrypt"

# ── Account types ──────────────────────────────────────────────────────────
ACCOUNT_TYPES    = ["+30 days old", "+1 year old", "5+ years old", "dump"]
DEFAULT_TYPES    = ["+30 days old", "+1 year old"]
PREMIUM_TYPES    = {"5+ years old", "dump"}
PREMIUM_COOLDOWN = 7200  # 2h

# ── Role config ────────────────────────────────────────────────────────────
COOLDOWN_ROLES_PATH = os.path.join(os.path.dirname(__file__), "cooldown_roles.json")
TYPE_ROLES_PATH     = os.path.join(os.path.dirname(__file__), "type_roles.json")

def load_cooldown_roles():
    try:
        with open(COOLDOWN_ROLES_PATH) as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    except Exception:
        return {}

def load_type_roles():
    try:
        with open(TYPE_ROLES_PATH) as f:
            raw = json.load(f)
        return {int(k): [t for t in v if t in ACCOUNT_TYPES]
                for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        return {}

COOLDOWN_ROLES    = load_cooldown_roles()
TYPE_ROLES        = load_type_roles()
active_cooldowns  = {}
premium_cooldowns = {}

def can_use_panel(member) -> bool:
    """Only members holding one of PANEL_ROLE_IDS (defined with the other role ids below) may use the panel."""
    return any(r.id in PANEL_ROLE_IDS for r in (getattr(member, "roles", None) or []))

def get_allowed_types(member):
    if can_use_panel(member):
        return list(ACCOUNT_TYPES)       # the old premium roles are gone: panel roles get every type
    allowed = set(DEFAULT_TYPES)
    for role in getattr(member, "roles", []):
        if role.id in TYPE_ROLES:
            allowed.update(TYPE_ROLES[role.id])
    return [t for t in ACCOUNT_TYPES if t in allowed]

def get_user_cooldown(member):
    c = [COOLDOWN_ROLES[r.id] for r in getattr(member, "roles", []) if r.id in COOLDOWN_ROLES]
    return min(c) if c else None

def get_remaining(user_id, premium=False):
    store = premium_cooldowns if premium else active_cooldowns
    ends  = store.get(user_id)
    if not ends: return 0
    rem = ends - time.time()
    if rem <= 0:
        store.pop(user_id, None)
        return 0
    return rem

def format_dur(s):
    s = int(round(s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    parts = []
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s or not parts: parts.append(f"{s}s")
    return " ".join(parts)

# ── Database ───────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
engine       = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(bind=engine)
Base         = declarative_base()

def utcnow():
    return datetime.now(timezone.utc)

class Account(Base):
    __tablename__ = "accounts"
    id           = Column(Integer, primary_key=True)
    username     = Column(String(150), nullable=False)
    password     = Column(String(1000), nullable=False)
    account_type = Column(String(50),  default="+30 days old")
    cookie       = Column(Text,        default="")
    region       = Column(String(10),  default="")
    created_at   = Column(DateTime(timezone=True), default=utcnow)
    updated_at   = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

# ── Tracker keys ───────────────────────────────────────────────────────────
TRACKER_ROLE_ID = int(os.environ.get("TRACKER_ROLE_ID", "1560565978486673450"))
SHOP_URL        = os.environ.get("SHOP_URL", "")
KEY_PLANS       = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
KEY_ALPHABET    = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I lookalikes

def parse_id_list(raw: str) -> frozenset:
    """'123, 456' -> frozenset({123, 456}). Anything that isn't a plain number is ignored."""
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)

# Only members holding one of these roles may run /gen keylicense and /revoke.
# If the variable is set but holds no valid ids, nobody can run them (fails closed).
OWNER_ROLE_IDS  = parse_id_list(os.environ.get(
    "OWNER_ROLE_IDS", "1518282859524395251,1518288403144245269"))
# Everyone can see the panel, but only these roles can use it (buttons and slash commands alike).
# Fails closed: a variable that holds no valid ids lets nobody in.
PANEL_ROLE_IDS  = parse_id_list(os.environ.get(
    "PANEL_ROLE_IDS", "1554597975202857080,1518282859524395251"))
PANEL_DENIED    = "You don't have a role that can use this panel."
OWNER_KEY_PLANS = {"1d": "day", "1w": "week", "1m": "month"}   # /gen keylicense choice -> key plan
PLAN_LABELS     = {"day": "1 day", "week": "1 week", "month": "1 month"}
REVOKED_BY      = 0   # license_keys.claimed_by sentinel: revoked by an owner, never redeemable
EXTEND_MAX_HOURS = 8760   # /extend limit (one year), so a typo like 100000 can't hand out decades
EXTEND_DM_GAP    = 0.6    # seconds between DMs when /extend all notifies people, to stay under Discord's limits

class LicenseKey(Base):
    """One row per sold key. Inserted by the shop (or /genkey), redeemed by /claim_key."""
    __tablename__ = "license_keys"
    id         = Column(Integer, primary_key=True)
    key        = Column(String(64), unique=True, nullable=False, index=True)
    plan       = Column(String(10), nullable=False)             # "day" | "week" | "month"
    order_ref  = Column(String(200), unique=True, nullable=True)  # payment session id
    created_at = Column(DateTime(timezone=True), default=utcnow)
    claimed_by = Column(BigInteger, nullable=True)              # Discord user id
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    buyer_email = Column(String(320), nullable=True)            # from the shop's checkout

class KeyAccess(Base):
    """A user's live Tracker Access window. The expiry loop removes the role at expires_at."""
    __tablename__ = "key_access"
    id         = Column(Integer, primary_key=True)
    user_id    = Column(BigInteger, nullable=False, index=True)
    guild_id   = Column(BigInteger, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    active     = Column(Boolean, nullable=False, default=True, index=True)

Base.metadata.create_all(bind=engine)

# Columns the shop adds to license_keys when Stripe reports a payment dispute. Added here too,
# so the bot never queries a column that doesn't exist yet if it starts before the shop does.
SHOP_DISPUTE_COLUMNS = ("dispute_at", "dispute_handled_at")

def ensure_dispute_columns():
    for name in SHOP_DISPUTE_COLUMNS:
        try:
            with engine.begin() as conn:
                conn.execute(sql_text(
                    f"ALTER TABLE license_keys ADD COLUMN IF NOT EXISTS {name} TIMESTAMPTZ"))
        except Exception as e:
            print(f"[keys] could not add license_keys.{name}: {e}")

ensure_dispute_columns()

def count_alts():
    db = SessionLocal()
    try:    return db.query(Account).count()
    finally: db.close()

def count_by_type():
    db = SessionLocal()
    try:
        return {t: db.query(Account).filter(Account.account_type == t).count()
                for t in ACCOUNT_TYPES}
    finally: db.close()

REGION_FLAGS = {
    "GB": "🇬🇧", "DE": "🇩🇪", "NL": "🇳🇱", "IT": "🇮🇹", "PL": "🇵🇱",
    "TR": "🇹🇷", "RU": "🇷🇺", "IQ": "🇮🇶", "US": "🇺🇸", "CA": "🇨🇦",
    "AU": "🇦🇺", "FR": "🇫🇷", "ES": "🇪🇸", "SE": "🇸🇪", "NO": "🇳🇴",
    "DK": "🇩🇰", "FI": "🇫🇮", "BE": "🇧🇪", "CH": "🇨🇭", "AT": "🇦🇹",
    "PT": "🇵🇹", "CZ": "🇨🇿", "RO": "🇷🇴", "HU": "🇭🇺", "UA": "🇺🇦",
    "BR": "🇧🇷", "MX": "🇲🇽", "AR": "🇦🇷", "IN": "🇮🇳", "JP": "🇯🇵",
    "KR": "🇰🇷", "SG": "🇸🇬", "NZ": "🇳🇿", "ZA": "🇿🇦", "SA": "🇸🇦",
    "AE": "🇦🇪", "EG": "🇪🇬", "SY": "🇸🇾", "CO": "🇨🇴",
}

def count_by_region(limit: int = 8):
    """Top `limit` regions by stock, highest first. Only regions with accounts.
    Returns a list of (code, count) tuples."""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        rows = (
            db.query(Account.region, func.count(Account.id))
            .filter(Account.region.isnot(None))
            .filter(Account.region != "")
            .group_by(Account.region)
            .all()
        )
        counts = [(str(r or "").upper(), int(c)) for r, c in rows if r and c > 0]
        counts.sort(key=lambda x: x[1], reverse=True)
        return counts[:limit]
    finally: db.close()

def stock_for_type(account_type: str):
    """(total in stock, [(region code, count), ...]) for one account type, biggest region first.
    The total includes accounts with no region; the list only has regions that have accounts."""
    db = SessionLocal()
    try:
        from sqlalchemy import func
        rows = (
            db.query(func.upper(Account.region), func.count(Account.id))
            .filter(Account.account_type == account_type)
            .group_by(func.upper(Account.region))
            .all()
        )
        total = sum(int(c) for _, c in rows)
        regions = [(str(r).strip(), int(c)) for r, c in rows if r and str(r).strip() and c > 0]
        regions.sort(key=lambda x: (-x[1], x[0]))
        return total, regions
    finally: db.close()

# ── Key helpers (sync DB work — call through asyncio.to_thread) ────────────
def aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def generate_key(plan: str) -> str:
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(12))
    return f"TRK-{plan[0].upper()}-{body[:4]}-{body[4:8]}-{body[8:]}"

def db_create_keys(plan: str, count: int) -> list:
    if plan not in KEY_PLANS:
        raise ValueError(f"Unknown plan: {plan}")
    keys = [generate_key(plan) for _ in range(count)]
    db = SessionLocal()
    try:
        for k in keys:
            db.add(LicenseKey(key=k, plan=plan))
        db.commit()
        return keys
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_claim_key(user_id: int, guild_id: int, raw_key: str):
    """Atomically claim a key and create or extend the user's access window.
    Returns ("ok", info) | ("invalid", None) | ("used", None) | ("revoked", None)."""
    key = raw_key.strip().upper()
    now = utcnow()
    db  = SessionLocal()
    try:
        row = db.query(LicenseKey).filter(LicenseKey.key == key).first()
        if row is None or row.plan not in KEY_PLANS:
            return "invalid", None
        if row.claimed_by == REVOKED_BY:
            return "revoked", None
        if row.claimed_by is not None:
            return "used", None
        key_id, plan = row.id, row.plan

        # Single-statement claim: only one concurrent caller can win this row.
        res = db.execute(
            update(LicenseKey)
            .where(LicenseKey.id == key_id, LicenseKey.claimed_by.is_(None))
            .values(claimed_by=user_id, claimed_at=now))
        if res.rowcount != 1:
            db.rollback()
            return "used", None

        duration = KEY_PLANS[plan]
        access = (db.query(KeyAccess)
                    .filter(KeyAccess.user_id == user_id,
                            KeyAccess.guild_id == guild_id,
                            KeyAccess.active.is_(True))
                    .first())
        if access:
            prev_expiry       = aware(access.expires_at)
            new_expiry        = max(now, prev_expiry) + duration   # stack on remaining time
            access.expires_at = new_expiry
            created_new       = False
        else:
            prev_expiry = None
            new_expiry  = now + duration
            access      = KeyAccess(user_id=user_id, guild_id=guild_id,
                                    expires_at=new_expiry, active=True)
            db.add(access)
            created_new = True
        db.flush()
        access_id = access.id
        db.commit()
        return "ok", {"key_id": key_id, "plan": plan, "access_id": access_id,
                      "expires_at": new_expiry, "prev_expiry": prev_expiry,
                      "created_new": created_new}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_undo_claim(info: dict):
    """Put a key back if the role could not be granted, so the buyer never loses it."""
    db = SessionLocal()
    try:
        db.execute(update(LicenseKey).where(LicenseKey.id == info["key_id"])
                   .values(claimed_by=None, claimed_at=None))
        if info["created_new"]:
            db.execute(delete(KeyAccess).where(KeyAccess.id == info["access_id"]))
        else:
            db.execute(update(KeyAccess).where(KeyAccess.id == info["access_id"])
                       .values(expires_at=info["prev_expiry"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_due_access(limit: int = 50) -> list:
    db = SessionLocal()
    try:
        rows = (db.query(KeyAccess)
                  .filter(KeyAccess.active.is_(True), KeyAccess.expires_at <= utcnow())
                  .order_by(KeyAccess.expires_at.asc()).limit(limit).all())
        return [(r.id, r.user_id, r.guild_id) for r in rows]
    finally:
        db.close()

def db_deactivate_if_due(access_id: int) -> bool:
    """True only if this call flipped a genuinely expired row — guards against a
    claim that extended the window between the scan and the revoke."""
    db = SessionLocal()
    try:
        res = db.execute(
            update(KeyAccess)
            .where(KeyAccess.id == access_id, KeyAccess.active.is_(True),
                   KeyAccess.expires_at <= utcnow())
            .values(active=False))
        db.commit()
        return res.rowcount == 1
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_reactivate(access_id: int):
    db = SessionLocal()
    try:
        db.execute(update(KeyAccess).where(KeyAccess.id == access_id).values(active=True))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_create_owner_key(plan: str, issuer_id: int) -> str:
    """Create one key for /gen keylicense. order_ref records which owner issued it."""
    if plan not in KEY_PLANS:
        raise ValueError(f"Unknown plan: {plan}")
    for _ in range(3):
        key = generate_key(plan)
        db  = SessionLocal()
        try:
            db.add(LicenseKey(key=key, plan=plan,
                              order_ref=f"owner:{issuer_id}:{secrets.token_hex(8)}"))
            db.commit()
            return key
        except IntegrityError:
            db.rollback()          # astronomically unlikely key collision: draw a fresh key
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    raise RuntimeError("Could not generate a unique key")

def db_revoke_key(raw_key: str):
    """Stop an unredeemed key from ever being claimed. The row stays (claimed_by set to
    REVOKED_BY) so the shop's idempotent fulfilment can't issue a replacement.
    Returns ("ok", plan) | ("invalid", None) | ("revoked", None) | ("claimed", user_id)."""
    key = raw_key.strip().upper()
    db  = SessionLocal()
    try:
        row = db.query(LicenseKey).filter(LicenseKey.key == key).first()
        if row is None:
            return "invalid", None
        if row.claimed_by == REVOKED_BY:
            return "revoked", None
        if row.claimed_by is not None:
            return "claimed", row.claimed_by
        row_id, plan = row.id, row.plan

        # Same single-statement guard as the claim: it only wins while the key is unclaimed.
        res = db.execute(
            update(LicenseKey)
            .where(LicenseKey.id == row_id, LicenseKey.claimed_by.is_(None))
            .values(claimed_by=REVOKED_BY, claimed_at=utcnow()))
        db.commit()
        if res.rowcount == 1:
            return "ok", plan
        fresh = db.query(LicenseKey).filter(LicenseKey.id == row_id).first()   # claimed in between
        return "claimed", (fresh.claimed_by if fresh else None)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_end_user_access(user_id: int, guild_id: int) -> list:
    """Close every live access window for a user. Returns the ids it closed so a failed
    role removal can reopen them. (If the expiry loop closes one of these between the
    read and the write, reopening it is harmless: the loop just closes it again.)"""
    db = SessionLocal()
    try:
        ids = [r[0] for r in db.query(KeyAccess.id)
                              .filter(KeyAccess.user_id == user_id,
                                      KeyAccess.guild_id == guild_id,
                                      KeyAccess.active.is_(True)).all()]
        if ids:
            db.execute(update(KeyAccess)
                       .where(KeyAccess.id.in_(ids), KeyAccess.active.is_(True))
                       .values(active=False))
            db.commit()
        return ids
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_extend_access(guild_id: int, hours: int, user_id: int | None = None) -> list:
    """Add `hours` to every live access window in the guild, or just user_id's.
    Live means active and not yet expired: a window that has already run out is left
    alone (the expiry loop is about to close it). The rows are locked while they are
    changed, so the expiry loop and a second /extend wait their turn instead of racing.
    Returns [(user_id, new_expires_at), ...] and changes nothing if it raises."""
    delta = timedelta(hours=hours)
    db    = SessionLocal()
    try:
        q = (db.query(KeyAccess)
               .filter(KeyAccess.guild_id == guild_id,
                       KeyAccess.active.is_(True),
                       KeyAccess.expires_at > utcnow()))
        if user_id is not None:
            q = q.filter(KeyAccess.user_id == user_id)
        done = []
        for row in q.with_for_update().all():
            row.expires_at = aware(row.expires_at) + delta
            done.append((row.user_id, row.expires_at))
        db.commit()
        return done
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def db_user_key_info(user_id: int, guild_id: int, show: int = 8) -> dict:
    """What /keyinfo shows about one user: their Tracker Access windows in this server and
    the keys they have redeemed (a key isn't tied to a server). Read only. Returns
      live_ends  sorted end times of windows that are active and not yet expired
      last_end   latest end time among the other windows, or None
      keys       the newest `show` redeemed keys, newest first
      key_total  how many keys they have redeemed in all
      plans      {plan: count} across all of them"""
    now = utcnow()
    db  = SessionLocal()
    try:
        windows = [(aware(w.expires_at), bool(w.active))
                   for w in db.query(KeyAccess)
                              .filter(KeyAccess.user_id == user_id,
                                      KeyAccess.guild_id == guild_id).all()]
        rows = (db.query(LicenseKey)
                  .filter(LicenseKey.claimed_by == user_id)
                  .order_by(LicenseKey.claimed_at.desc()).all())
        keys = [{"key": r.key, "plan": r.plan, "claimed_at": aware(r.claimed_at),
                 "order_ref": r.order_ref} for r in rows]
    finally:
        db.close()

    live  = sorted(end for end, active in windows if active and end > now)
    ended = [end for end, active in windows if not (active and end > now)]
    plans = {}
    for k in keys:
        plans[k["plan"]] = plans.get(k["plan"], 0) + 1
    return {"live_ends": live, "last_end": max(ended) if ended else None,
            "keys": keys[:show], "key_total": len(keys), "plans": plans}

def db_pending_disputes(limit: int = 20) -> list:
    """Redeemed keys whose payment was disputed and that the bot hasn't dealt with yet, as
    [(key_id, discord_user_id)]. The shop sets dispute_at when Stripe reports the dispute.
    A key nobody had redeemed is blocked by the shop itself (claimed_by = REVOKED_BY)."""
    db = SessionLocal()
    try:
        rows = db.execute(
            sql_text("SELECT id, claimed_by FROM license_keys "
                     "WHERE dispute_at IS NOT NULL AND dispute_handled_at IS NULL "
                     "AND claimed_by IS NOT NULL AND claimed_by <> :revoked "
                     "ORDER BY dispute_at ASC LIMIT :limit"),
            {"revoked": REVOKED_BY, "limit": limit}).all()
        return [(r[0], r[1]) for r in rows]
    finally:
        db.close()

def db_mark_dispute_handled(key_id: int) -> bool:
    """True only for the call that recorded it, so a repeat pass changes nothing."""
    db = SessionLocal()
    try:
        res = db.execute(
            sql_text("UPDATE license_keys SET dispute_handled_at = :now "
                     "WHERE id = :id AND dispute_handled_at IS NULL"),
            {"now": utcnow(), "id": key_id})
        db.commit()
        return res.rowcount == 1
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ── Core generate logic ────────────────────────────────────────────────────
async def do_generate(interaction: discord.Interaction, account_type: str, region: str = None):
    user_id = interaction.user.id
    if not can_use_panel(interaction.user):
        print(f"[panel] {interaction.user} ({user_id}) tried to generate without a panel role")
        await interaction.response.send_message(PANEL_DENIED, ephemeral=True)
        return
    allowed = get_allowed_types(interaction.user)

    if account_type not in allowed:
        await interaction.response.send_message(
            "You don't have access to this account type.", ephemeral=True)
        return

    is_premium = account_type in PREMIUM_TYPES
    rem = get_remaining(user_id, premium=is_premium)
    if rem > 0:
        label = "premium cooldown" if is_premium else "cooldown"
        await interaction.response.send_message(
            f"You are on {label}. Try again in **{format_dur(rem)}**.", ephemeral=True)
        return

    db = SessionLocal()
    try:
        q = db.query(Account).filter(Account.account_type == account_type)
        if region:
            q = q.filter(Account.region == region.upper())
        account = q.order_by(Account.id.asc()).first()

        if not account:
            reg_str = f" from **{region.upper()}**" if region else ""
            await interaction.response.send_message(
                f"No **{account_type}**{reg_str} accounts available.", ephemeral=True)
            return

        # Copy all needed attributes BEFORE delete+commit — avoids DetachedInstanceError
        acct_username = account.username
        acct_region   = account.region or ""
        pw            = decrypt_secret(account.password)
        cookie        = account.cookie or ""

        db.delete(account)
        db.commit()

        remaining_total = db.query(Account).count()

    except Exception as e:
        db.rollback()
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        raise
    finally:
        db.close()

    # Apply cooldowns now that delivery is confirmed
    if is_premium:
        premium_cooldowns[user_id] = time.time() + PREMIUM_COOLDOWN
    else:
        cd = get_user_cooldown(interaction.user)
        if cd:
            active_cooldowns[user_id] = time.time() + cd

    print(f"[+] Sent {acct_username} ({account_type}"
          f"{', ' + acct_region if acct_region else ''}) — {remaining_total} left")

    # Build and send the account embed first, then update panel
    embed = discord.Embed(color=0x7C3AED)
    embed.set_author(name=acct_username, icon_url="https://www.roblox.com/favicon.ico")
    embed.add_field(name="Username", value=f"`{acct_username}`", inline=True)
    embed.add_field(name="Password", value=f"||`{pw}`||",        inline=True)
    embed.add_field(name="Type",     value=account_type,         inline=True)
    if acct_region:
        embed.add_field(name="Region", value=acct_region, inline=True)
    embed.set_footer(text="DeltaCore Alt Generator  \u2022  Save this — it will not be shown again")

    msg_kwargs: dict = {"embed": embed, "ephemeral": True}

    if cookie:
        cookie_block = f"**Cookie** (inject with Cookie-Editor):\n||```\n{cookie}\n```||"
        if len(cookie_block) > 1900:
            f_obj = io.BytesIO(
                f"username: {acct_username}\npassword: {pw}\ncookie: {cookie}\n".encode())
            msg_kwargs["file"]    = discord.File(f_obj, filename=f"{acct_username}.txt")
            msg_kwargs["content"] = "Cookie is too long for chat — attached as a file."
        else:
            msg_kwargs["content"] = cookie_block

    await interaction.response.send_message(**msg_kwargs)

    # Update panel and channel after delivery — in separate try so errors don't affect the user
    try:
        await client.update_stock_channel(remaining_total)
        await client.send_or_update_panel()
    except Exception as e:
        print(f"[!] Panel/channel update failed after generate: {e}")


# ── Region select (ephemeral — only visible to the user who clicked) ───────
REGIONS = [
    ("GB", "United Kingdom"), ("DE", "Germany"),    ("NL", "Netherlands"),
    ("IT", "Italy"),          ("PL", "Poland"),      ("TR", "Turkiye"),
    ("RU", "Russia"),         ("US", "United States"),("FR", "France"),
    ("SE", "Sweden"),         ("SY", "Syria"),       ("AU", "Australia"),
    ("CA", "Canada"),         ("IQ", "Iraq"),         ("NO", "Norway"),
    ("CO", "Colombia"),
]

class RegionSelect(discord.ui.Select):
    def __init__(self, account_type: str, total: int, regions: list):
        self.account_type = account_type
        # No option is pre-selected: Discord only sends a selection that changes, so a pre-selected
        # "Any region" could not be picked without choosing another region first.
        # Only regions that have stock right now are listed (Discord allows 25 options: Any + 24).
        names = dict(REGIONS)
        options = [discord.SelectOption(label=f"Any region — {total:,} in stock", value="any")]
        for code, count in regions[:24]:
            options.append(discord.SelectOption(
                label=f"{names.get(code, code)} ({code}) — {count:,}", value=code))
        # No custom_id — this is a short-lived ephemeral view, not a persistent one.
        super().__init__(
            placeholder="Choose a region, or Any region...",
            options=options, min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        region = None if self.values[0] == "any" else self.values[0]
        await do_generate(interaction, self.account_type, region)


class RegionView(discord.ui.View):
    def __init__(self, account_type: str, total: int, regions: list):
        super().__init__(timeout=120)
        self.add_item(RegionSelect(account_type, total, regions))

    async def on_timeout(self):
        # Disable the select so a stale click shows it's expired rather than erroring
        for item in self.children:
            item.disabled = True


# ── Main panel view (persistent — survives restarts) ──────────────────────
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _show_region_picker(self, interaction: discord.Interaction, account_type: str):
        """Send an ephemeral region picker — only the clicking user can see it."""
        if not can_use_panel(interaction.user):
            await interaction.response.send_message(PANEL_DENIED, ephemeral=True)
            return
        try:
            total, regions = await asyncio.to_thread(stock_for_type, account_type)
        except Exception as e:
            print(f"[!] Could not read stock for {account_type}: {e}")
            await interaction.response.send_message("Couldn't read the stock. Try again.", ephemeral=True)
            return
        if total <= 0:
            await interaction.response.send_message(
                f"No **{account_type}** accounts available right now.", ephemeral=True)
            return
        embed = discord.Embed(
            description=f"Select a region for your **{account_type}** account.\nChoose **Any region** to get the next available.",
            color=0x7C3AED,
        )
        await interaction.response.send_message(embed=embed, view=RegionView(account_type, total, regions), ephemeral=True)

    @discord.ui.button(label="30 Day ", style=discord.ButtonStyle.primary,
                       custom_id="panel_30d", row=0)
    async def btn_30d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "+30 days old")

    @discord.ui.button(label="1 Year\u2007", style=discord.ButtonStyle.primary,
                       custom_id="panel_1y", row=0)
    async def btn_1y(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "+1 year old")

    @discord.ui.button(label="5 Years", style=discord.ButtonStyle.secondary,
                       custom_id="panel_5y", row=0)
    async def btn_5y(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "5+ years old")

    @discord.ui.button(label="\u2007Dump\u2007\u2007", style=discord.ButtonStyle.secondary,
                       custom_id="panel_dump", row=0)
    async def btn_dump(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "dump")


def build_panel_embed(counts: dict) -> discord.Embed:
    c30  = counts.get("+30 days old", 0)
    c1y  = counts.get("+1 year old",  0)
    c5y  = counts.get("5+ years old", 0)
    cdmp = counts.get("dump",          0)

    lines = (
        f"**30 day** \u2014 {c30:,} in stock\n"
        f"**1 year** \u2014 {c1y:,} in stock\n"
        f"**5 year** \u2014 {c5y:,} in stock  \u00b7  *premium*\n"
        f"**Dump** \u2014 {cdmp:,} in stock  \u00b7  *premium*"
    )

    embed = discord.Embed(
        title="DeltaCore Vault",
        description=f"Press a button to claim an account. The region selector is private.\n\n{lines}",
        color=0x7C3AED,
    )

    # Region breakdown — two-column grid, flags next to codes, top 8 by stock
    region_counts = count_by_region(limit=8)
    if region_counts:
        rows = [f"{REGION_FLAGS.get(code, '\U0001F310')} {code} \u2014 **{cnt:,}**"
                for code, cnt in region_counts]
        # Split into two columns for a grid look
        half = (len(rows) + 1) // 2
        col1 = "\n".join(rows[:half])
        col2 = "\n".join(rows[half:]) or "\u200b"
        embed.add_field(name="By region", value=col1, inline=True)
        embed.add_field(name="\u200b",    value=col2, inline=True)

    embed.set_footer(text="DeltaCore Alt Generator  \u2022  Accounts are deleted after delivery")
    return embed


# ── Bot ────────────────────────────────────────────────────────────────────
class CredBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PanelView())  # re-register persistent view on restart
        await self.tree.sync()
        print("[*] Commands synced")

    async def on_ready(self):
        print(f"[*] Ready as {self.user} — {count_alts()} accounts in vault")
        await self.update_stock_channel()
        await self.send_or_update_panel()
        if not self.refresh_stock.is_running():
            self.refresh_stock.start()
        if not self.expire_access.is_running():
            self.expire_access.start()
        if not self.process_disputes.is_running():
            self.process_disputes.start()

    async def update_stock_channel(self, total: int = None):
        if not VOICE_CHANNEL_ID:
            return
        if total is None:
            total = count_alts()
        ch = self.get_channel(VOICE_CHANNEL_ID)
        if not ch:
            try:
                ch = await self.fetch_channel(VOICE_CHANNEL_ID)
            except discord.HTTPException:
                return
        name = f"{total} Alts Stocked"
        if ch.name != name:
            try:
                await ch.edit(name=name)
            except discord.HTTPException:
                pass

    async def send_or_update_panel(self):
        """Send or edit the persistent panel in PANEL_CHANNEL_ID."""
        channel = self.get_channel(PANEL_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(PANEL_CHANNEL_ID)
            except discord.HTTPException as e:
                print(f"[!] Could not fetch panel channel: {e}")
                return

        counts  = count_by_type()
        embed   = build_panel_embed(counts)
        msg_id  = load_panel_msg_id()

        if msg_id:
            try:
                msg = await channel.fetch_message(msg_id)
                await msg.edit(embed=embed, view=PanelView())
                print(f"[*] Panel updated (message {msg_id})")
                return
            except discord.NotFound:
                print("[*] Panel message deleted — resending")
            except discord.HTTPException as e:
                print(f"[!] Panel edit failed: {e}")

        # Send a fresh panel
        try:
            msg = await channel.send(embed=embed, view=PanelView())
            save_panel_msg_id(msg.id)
            print(f"[*] Panel sent (message {msg.id})")
        except discord.HTTPException as e:
            print(f"[!] Could not send panel: {e}")

    @tasks.loop(minutes=30)
    async def refresh_stock(self):
        await self.update_stock_channel()
        await self.send_or_update_panel()

    @refresh_stock.before_loop
    async def before_refresh(self):
        await self.wait_until_ready()

    # ── Tracker key expiry ────────────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def expire_access(self):
        try:
            due = await asyncio.to_thread(db_due_access)
        except Exception as e:
            print(f"[!] Key expiry scan failed: {e}")
            return
        for access_id, user_id, guild_id in due:
            try:
                await self.revoke_access(access_id, user_id, guild_id)
            except Exception as e:
                print(f"[!] Revoke failed for {user_id}: {e}")

    @expire_access.before_loop
    async def before_expire(self):
        await self.wait_until_ready()

    async def revoke_access(self, access_id: int, user_id: int, guild_id: int):
        # Flip the row first; only continue if it really is expired right now.
        if not await asyncio.to_thread(db_deactivate_if_due, access_id):
            return

        guild = self.get_guild(guild_id)
        if guild is None:
            print(f"[keys] Guild {guild_id} unavailable — access {access_id} closed")
            return

        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            print(f"[keys] {user_id} left the server — access {access_id} closed")
            return
        except discord.HTTPException as e:
            print(f"[!] Could not fetch member {user_id}: {e} — will retry")
            await asyncio.to_thread(db_reactivate, access_id)
            return

        role = guild.get_role(TRACKER_ROLE_ID)
        if role is not None and role in member.roles:
            try:
                await member.remove_roles(role, reason="Tracker key expired")
            except discord.HTTPException as e:  # includes Forbidden
                print(f"[!] Could not remove role from {user_id}: {e} — will retry")
                await asyncio.to_thread(db_reactivate, access_id)
                return
        print(f"[keys] Tracker Access expired for {user_id}")

        text = "Your **Tracker Access** key has run out and the role has been removed."
        text += (f"\nGet a new key here: {SHOP_URL}" if SHOP_URL
                 else "\nPurchase a new key to get access back.")
        try:
            await member.send(embed=discord.Embed(
                title="Your key is over", description=text, color=0x7C3AED))
        except discord.HTTPException:
            print(f"[keys] Could not DM {user_id} (DMs closed)")

    # ── Payment disputes ──────────────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def process_disputes(self):
        """When Stripe reports a chargeback on a key somebody already redeemed, the shop marks
        the key and this loop ends that person's access and takes the role off them."""
        try:
            pending = await asyncio.to_thread(db_pending_disputes)
        except Exception as e:
            print(f"[!] Dispute scan failed: {e}")
            return
        for key_id, user_id in pending:
            try:
                await self.end_disputed_access(key_id, user_id)
            except Exception as e:
                print(f"[!] Dispute handling failed for key {key_id}: {e}")

    @process_disputes.before_loop
    async def before_disputes(self):
        await self.wait_until_ready()

    async def end_disputed_access(self, key_id: int, user_id: int) -> bool:
        """End the access of whoever redeemed a disputed key. True once it is dealt with (access
        ended, or there was nothing to end); False when something failed, so the next pass retries."""
        guilds = [g for g in self.guilds if g.get_role(TRACKER_ROLE_ID) is not None]
        if not guilds:
            print(f"[!] Dispute on key {key_id}: the Tracker role isn't in any server I'm in")
            return False
        for guild in guilds:
            role = guild.get_role(TRACKER_ROLE_ID)
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None                   # they left the server: no role left to remove
            except discord.HTTPException as e:
                print(f"[!] Dispute on key {key_id}: could not look up {user_id}: {e} — will retry")
                return False
            outcome, _detail = await revoke_member_access(
                guild, role, user_id, member, DISPUTE_ACTOR, dm_embed=dispute_dm_embed())
            if outcome in ("db_error", "role_error"):
                print(f"[!] Dispute on key {key_id}: {outcome} for {user_id} — will retry")
                return False
        await asyncio.to_thread(db_mark_dispute_handled, key_id)
        print(f"[keys] Payment dispute on key {key_id}: access ended for {user_id}")
        return True


client = CredBot()


# ── Tracker key commands ───────────────────────────────────────────────────
@app_commands.command(name="claim_key", description="Redeem a Tracker Access key")
@app_commands.describe(key="Your key, e.g. TRK-D-ABCD-EFGH-JKLM")
@app_commands.guild_only()
async def claim_key(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role  = guild.get_role(TRACKER_ROLE_ID) if guild else None
    if role is None:
        print(f"[!] Tracker role {TRACKER_ROLE_ID} not found in guild")
        await interaction.followup.send(
            "The Tracker Access role could not be found. Please contact staff.", ephemeral=True)
        return

    try:
        status, info = await asyncio.to_thread(
            db_claim_key, interaction.user.id, guild.id, key)
    except Exception as e:
        print(f"[!] Key claim DB error: {e}")
        await interaction.followup.send(
            "Something went wrong checking that key. Please try again in a moment.", ephemeral=True)
        return

    if status == "invalid":
        await interaction.followup.send("That key isn't valid. Check it and try again.", ephemeral=True)
        return
    if status == "used":
        await interaction.followup.send("That key has already been used.", ephemeral=True)
        return
    if status == "revoked":
        await interaction.followup.send(
            "That key has been revoked. Contact staff if you think that's a mistake.", ephemeral=True)
        return

    try:
        await interaction.user.add_roles(role, reason=f"Tracker key claimed ({info['plan']})")
    except discord.HTTPException as e:  # includes Forbidden
        print(f"[!] Could not give role to {interaction.user.id}: {e}")
        try:
            await asyncio.to_thread(db_undo_claim, info)
        except Exception as undo_err:
            print(f"[!] CRITICAL: could not release key_id {info['key_id']}: {undo_err}")
        await interaction.followup.send(
            "I couldn't give you the role, but your key has **not** been used. "
            "Please contact staff.", ephemeral=True)
        return

    ts = int(info["expires_at"].timestamp())
    print(f"[keys] {interaction.user.id} claimed a {info['plan']} key — "
          f"expires {info['expires_at'].isoformat()}")
    embed = discord.Embed(
        title="Tracker Access unlocked",
        description=f"Plan: **{info['plan'].title()}**\nExpires: <t:{ts}:F> (<t:{ts}:R>)",
        color=0x7C3AED)
    await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="genkey", description="(Admin) Create Tracker Access keys")
@app_commands.describe(plan="Key length", amount="How many keys (1-25)")
@app_commands.choices(plan=[app_commands.Choice(name="Day", value="day"),
                            app_commands.Choice(name="Week", value="week"),
                            app_commands.Choice(name="Month", value="month")])
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def genkey(interaction: discord.Interaction, plan: app_commands.Choice[str],
                 amount: app_commands.Range[int, 1, 25] = 1):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    try:
        keys = await asyncio.to_thread(db_create_keys, plan.value, amount)
    except Exception as e:
        print(f"[!] genkey failed: {e}")
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        return
    await interaction.response.send_message(
        f"**{amount} × {plan.name}** key(s):\n```\n" + "\n".join(keys) + "\n```", ephemeral=True)

# ── Owner commands (roles in OWNER_ROLE_IDS only) ──────────────────────────
def is_owner(interaction: discord.Interaction) -> bool:
    roles = getattr(interaction.user, "roles", None) or []
    return any(r.id in OWNER_ROLE_IDS for r in roles)

async def require_owner(interaction: discord.Interaction) -> bool:
    """Refuse, privately, unless the caller holds an owner role. True means carry on."""
    if is_owner(interaction):
        return True
    name = interaction.command.qualified_name if interaction.command else "?"
    print(f"[keys] {interaction.user} ({interaction.user.id}) tried /{name} without an owner role")
    await interaction.response.send_message("This command is for owners only.", ephemeral=True)
    return False

async def try_dm(user, embed: discord.Embed) -> bool:
    """DM a user. False if their DMs are closed or Discord refused."""
    try:
        await user.send(embed=embed)
        return True
    except discord.HTTPException:
        return False


owner_gen = app_commands.Group(name="gen", description="Owner tools", guild_only=True)

@owner_gen.command(name="keylicense", description="(Owner) Create a key and DM it to a user")
@app_commands.describe(duration="Key length", user="Who the key is for")
@app_commands.choices(duration=[app_commands.Choice(name="1d (1 day)", value="1d"),
                                app_commands.Choice(name="1w (1 week)", value="1w"),
                                app_commands.Choice(name="1m (1 month, 30 days)", value="1m")])
async def gen_keylicense(interaction: discord.Interaction,
                         duration: app_commands.Choice[str], user: discord.Member):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    if user.bot:
        await interaction.followup.send("Bots can't receive keys.", ephemeral=True)
        return

    plan  = OWNER_KEY_PLANS[duration.value]
    label = PLAN_LABELS[plan]
    try:
        key = await asyncio.to_thread(db_create_owner_key, plan, interaction.user.id)
    except Exception as e:
        print(f"[!] /gen keylicense failed: {e}")
        await interaction.followup.send(
            "I couldn't create the key. Check the bot logs.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Your Tracker Access key",
        description=f"Staff have issued you a **{label}** key.",
        color=0x7C3AED)
    embed.add_field(name="Activation key", value=f"```\n{key}\n```", inline=False)
    embed.add_field(name="How to redeem",
                    value="Run `/claim_key` in the server and paste the key.", inline=False)
    embed.add_field(
        name="Timeline",
        value=(f"Your {label} starts when you claim the key, not before, and an unclaimed "
               f"key doesn't expire. When your time is up we remove the role and send you "
               f"a message."),
        inline=False)
    delivered = await try_dm(user, embed)
    print(f"[keys] {interaction.user.id} issued a {plan} key to {user.id} "
          f"(DM {'sent' if delivered else 'failed'})")

    status = (f"Key sent to {user.mention} by DM." if delivered else
              f"I couldn't DM {user.mention} (their DMs are closed). Pass the key to them yourself.")
    await interaction.followup.send(
        f"{status}\n**{label}** key:\n```\n{key}\n```It stays unclaimed until they run `/claim_key`.",
        ephemeral=True)


REVOKE_ERRORS = {
    "db_error":   "I couldn't update the database, so nothing was changed.",
    "role_error": "I couldn't remove the role, so nothing was changed. Check that I have "
                  "Manage Roles and that my role sits above Tracker Access.",
}

class _DisputeActor:
    """Stands in for the staff member in revoke_member_access when the shop reports a chargeback."""
    id = 0
    def __str__(self):
        return "a payment dispute"

DISPUTE_ACTOR = _DisputeActor()

def dispute_dm_embed():
    return discord.Embed(
        title="Your access has ended",
        description=("The payment for one of your **Tracker Access** keys was disputed with the "
                     "card issuer, so your access has ended and the role was removed. "
                     "If you think that's a mistake, open a support ticket."),
        color=0xEF4444)

async def revoke_member_access(guild, role, user_id: int, member, actor, dm_embed=None):
    """End every live Tracker access window for user_id and take the Tracker role off them.
    member is None when they are no longer in the server. dm_embed replaces the usual
    "staff ended your access" message. Returns (outcome, detail):
      ("db_error", None)    nothing was changed
      ("role_error", None)  Discord refused the role removal; windows were put back, nothing changed
      ("nothing", None)     they had no access window and no role
      ("ok", {"ended": n, "had_role": bool, "delivered": bool})"""
    try:
        ended = await asyncio.to_thread(db_end_user_access, user_id, guild.id)
    except Exception as e:
        print(f"[!] revoke DB error for {user_id}: {e}")
        return "db_error", None

    had_role = member is not None and role in member.roles
    if had_role:
        try:
            await member.remove_roles(role, reason=f"Tracker access revoked by {actor}")
        except discord.HTTPException as e:  # includes Forbidden
            print(f"[!] Could not remove role from {user_id}: {e}")
            for access_id in ended:         # put the windows back: nothing changed
                try:
                    await asyncio.to_thread(db_reactivate, access_id)
                except Exception as undo_err:
                    print(f"[!] CRITICAL: could not reopen access {access_id}: {undo_err}")
            return "role_error", None

    if not ended and not had_role:
        return "nothing", None

    delivered = False
    if member is not None:
        embed = dm_embed or discord.Embed(
            title="Your access has ended",
            description=("Staff have ended your **Tracker Access** and removed the role. "
                         "If you think that's a mistake, contact staff."),
            color=0xEF4444)
        delivered = await try_dm(member, embed)
    print(f"[keys] {actor.id} revoked {user_id}: role={'removed' if had_role else 'absent'}, "
          f"windows={len(ended)}, DM {'sent' if delivered else 'not sent'}")
    return "ok", {"ended": len(ended), "had_role": had_role, "delivered": delivered}

def revoke_summary(title: str, detail: dict, in_server: bool = True) -> str:
    if detail["had_role"]:
        role_line = "yes"
    else:
        role_line = "they did not have it" if in_server else "they are no longer in the server"
    if detail["delivered"]:
        dm_line = "sent"
    else:
        dm_line = ("not delivered (their DMs are closed)" if in_server
                   else "not sent (they are no longer in the server)")
    return (f"{title}\n"
            f"Role removed: {role_line}\n"
            f"Key windows ended: {detail['ended']}\n"
            f"DM: {dm_line}")

async def revoke_redeemed_key(interaction: discord.Interaction, user_id):
    """/revoke key on a key somebody already redeemed: end that person's access and take
    the Tracker role off them. Returns the message for the owner."""
    if not user_id:
        return "That key was already redeemed, but I couldn't tell by whom. Use `/revoke user`."
    guild = interaction.guild
    role  = guild.get_role(TRACKER_ROLE_ID)
    if role is None:
        print(f"[!] Tracker role {TRACKER_ROLE_ID} not found in guild")
        return "The Tracker Access role could not be found."
    try:
        # fetch, don't use the cache: without the members intent cached roles can be stale
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        member = None                       # they left the server: no role left to remove
    except discord.HTTPException as e:
        print(f"[!] Could not look up member {user_id}: {e}")
        return "I couldn't look that user up on Discord, so nothing was changed."

    who = f"<@{user_id}>"
    outcome, detail = await revoke_member_access(guild, role, user_id, member, interaction.user)
    if outcome == "nothing":
        return f"That key was redeemed by {who}, but they have no Tracker Access left to remove."
    if outcome != "ok":
        return REVOKE_ERRORS[outcome]
    return revoke_summary(f"Revoked the key redeemed by {who}.", detail, in_server=member is not None)


owner_revoke = app_commands.Group(name="revoke", description="Owner tools", guild_only=True)

@owner_revoke.command(name="key", description="(Owner) Revoke a key; if it was redeemed, remove their role")
@app_commands.describe(key="The key to revoke, e.g. TRK-D-ABCD-EFGH-JKLM")
async def revoke_key_cmd(interaction: discord.Interaction, key: str):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        status, detail = await asyncio.to_thread(db_revoke_key, key)
    except Exception as e:
        print(f"[!] /revoke key failed: {e}")
        await interaction.followup.send(
            "I couldn't check that key. Check the bot logs.", ephemeral=True)
        return

    if status == "invalid":
        msg = "No key matches that text. Check it and try again."
    elif status == "revoked":
        msg = "That key has already been revoked."
    elif status == "claimed":
        try:
            msg = await revoke_redeemed_key(interaction, detail)
        except Exception as e:
            print(f"[!] /revoke key (redeemed) failed: {e}")
            msg = "Something went wrong ending their access. Check the bot logs."
    else:
        msg = f"Revoked the **{PLAN_LABELS.get(detail, detail)}** key. It can no longer be redeemed."
    print(f"[keys] {interaction.user.id} ran /revoke key -> {status}")
    await interaction.followup.send(msg, ephemeral=True)


@owner_revoke.command(name="user", description="(Owner) End a user's Tracker Access now")
@app_commands.describe(user="Whose access to end")
async def revoke_user_cmd(interaction: discord.Interaction, user: discord.Member):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role  = guild.get_role(TRACKER_ROLE_ID)
    if role is None:
        print(f"[!] Tracker role {TRACKER_ROLE_ID} not found in guild")
        await interaction.followup.send(
            "The Tracker Access role could not be found.", ephemeral=True)
        return

    outcome, detail = await revoke_member_access(guild, role, user.id, user, interaction.user)
    if outcome == "nothing":
        msg = f"{user.mention} has no Tracker Access to revoke."
    elif outcome == "ok":
        msg = revoke_summary(f"Revoked {user.mention}.", detail)
    else:
        msg = REVOKE_ERRORS[outcome]
    await interaction.followup.send(msg, ephemeral=True)


# ── Extend access (owners) ──────────────────────────────────────────────────
def hours_label(hours: int) -> str:
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    if hours >= 24 and hours % 24 == 0:
        days = hours // 24
        text += f" ({days} day{'s' if days != 1 else ''})"
    return text

def access_end_text(dt) -> str:
    ts = int(dt.timestamp())
    return f"<t:{ts}:F> (<t:{ts}:R>)"

def extend_dm_embed(hours: int, end) -> discord.Embed:
    return discord.Embed(
        title="Your access was extended",
        description=(f"Staff added **{hours_label(hours)}** to your **Tracker Access**.\n"
                     f"It now ends {access_end_text(end)}."),
        color=0x7C3AED)

async def extend_windows(interaction: discord.Interaction, hours: int, user_id: int | None = None):
    """Add hours to live access windows (everyone's, or just user_id's). Returns
    {user_id: latest new end time}, empty when nobody had live access, or None when the
    database refused (nothing changed)."""
    try:
        rows = await asyncio.to_thread(db_extend_access, interaction.guild.id, hours, user_id)
    except Exception as e:
        print(f"[!] /extend failed: {e}")
        return None
    ends = {}
    for uid, end in rows:
        if uid not in ends or end > ends[uid]:
            ends[uid] = end
    scope = f"user {user_id}" if user_id is not None else "everyone"
    print(f"[keys] {interaction.user.id} extended {scope} by {hours}h: "
          f"{len(rows)} window(s), {len(ends)} user(s)")
    return ends

async def dm_extension(user_id: int, hours: int, end) -> bool:
    """Tell someone their access was extended. False if they can't be reached."""
    try:
        user = await client.fetch_user(user_id)
    except discord.HTTPException:          # includes NotFound
        return False
    return await try_dm(user, extend_dm_embed(hours, end))


owner_extend = app_commands.Group(name="extend", description="Owner tools", guild_only=True)

@owner_extend.command(name="all", description="(Owner) Add hours to everyone's current Tracker Access")
@app_commands.describe(hours=f"Hours to add, 1 to {EXTEND_MAX_HOURS}",
                       notify="DM each person their new end time (off by default, it can be a lot of DMs)")
async def extend_all_cmd(interaction: discord.Interaction,
                         hours: app_commands.Range[int, 1, EXTEND_MAX_HOURS], notify: bool = False):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    ends = await extend_windows(interaction, hours)
    if ends is None:
        await interaction.followup.send(
            "I couldn't update the database, so nothing was changed.", ephemeral=True)
        return
    if not ends:
        await interaction.followup.send(
            "Nobody has live Tracker Access right now, so nothing was extended.", ephemeral=True)
        return

    n = len(ends)
    lines = [f"Added **{hours_label(hours)}** to **{n}** {'person' if n == 1 else 'people'}'s Tracker Access.",
             f"Earliest end now: {access_end_text(min(ends.values()))}",
             f"Latest end now: {access_end_text(max(ends.values()))}"]
    if notify:
        sent = 0
        for i, (uid, end) in enumerate(ends.items()):
            if i:
                await asyncio.sleep(EXTEND_DM_GAP)
            if await dm_extension(uid, hours, end):
                sent += 1
        lines.append(f"DMs: {sent} sent, {n - sent} not delivered (DMs closed or no longer in the server)")
    else:
        lines.append("DMs: not sent")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@owner_extend.command(name="user", description="(Owner) Add hours to one person's current Tracker Access")
@app_commands.describe(user="Whose access to extend", hours=f"Hours to add, 1 to {EXTEND_MAX_HOURS}",
                       notify="DM them their new end time")
async def extend_user_cmd(interaction: discord.Interaction, user: discord.Member,
                          hours: app_commands.Range[int, 1, EXTEND_MAX_HOURS], notify: bool = True):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    ends = await extend_windows(interaction, hours, user.id)
    if ends is None:
        await interaction.followup.send(
            "I couldn't update the database, so nothing was changed.", ephemeral=True)
        return
    if user.id not in ends:
        await interaction.followup.send(
            f"{user.mention} has no live Tracker Access to extend. "
            f"To give them time, use `/gen keylicense`.", ephemeral=True)
        return

    end   = ends[user.id]
    lines = [f"Extended {user.mention} by **{hours_label(hours)}**.",
             f"Their access now ends {access_end_text(end)}."]
    if notify:
        delivered = await try_dm(user, extend_dm_embed(hours, end))
        lines.append("DM: sent" if delivered else "DM: not delivered (their DMs are closed)")
    else:
        lines.append("DM: not sent")
    if not any(r.id == TRACKER_ROLE_ID for r in getattr(user, "roles", [])):
        lines.append("Heads up: they don't have the Tracker Access role right now, so the extra "
                     "time won't help until it is given back.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ── Order lookup (owners) ───────────────────────────────────────────────────
ORDER_REF_RE = re.compile(r"cs_(?:test|live)_[A-Za-z0-9]{10,200}")

def order_search_terms(raw: str):
    """(order ref, key) to match. A quoted ID or a pasted receipt link still works."""
    q = raw.strip().strip("`'\"<>")
    m = ORDER_REF_RE.search(q)
    return (m.group(0) if m else q), q.upper()

def db_find_order(raw: str):
    """Find a sold key by its order ID or by the key itself. A dict, or None if nothing matches."""
    ref, key = order_search_terms(raw)
    if not key:
        return None
    db = SessionLocal()
    try:
        row = (db.query(LicenseKey)
                 .filter(or_(LicenseKey.order_ref == ref, LicenseKey.key == key))
                 .first())
        if row is None:
            return None
        info = {"id": row.id, "key": row.key, "plan": row.plan, "order_ref": row.order_ref,
                "created_at": aware(row.created_at), "claimed_by": row.claimed_by,
                "claimed_at": aware(row.claimed_at), "buyer_email": row.buyer_email, "pay": None,
                "dispute_at": None, "dispute_handled_at": None}
        try:   # the shop adds these columns, so they can be missing until it has started once
            r = db.execute(
                sql_text("SELECT pay_currency, pay_subtotal, pay_tax, pay_discount, pay_total "
                         "FROM license_keys WHERE id = :id"), {"id": info["id"]}).first()
            if r is not None and r[4] is not None:
                info["pay"] = {"currency": r[0], "subtotal": r[1], "tax": r[2],
                               "discount": r[3], "total": r[4]}
        except Exception as e:
            db.rollback()
            print(f"[keys] payment columns not readable yet: {e}")
        try:   # same for the dispute columns
            r = db.execute(
                sql_text("SELECT dispute_at, dispute_handled_at FROM license_keys WHERE id = :id"),
                {"id": info["id"]}).first()
            if r is not None:
                info["dispute_at"], info["dispute_handled_at"] = aware(r[0]), aware(r[1])
        except Exception as e:
            db.rollback()
            print(f"[keys] dispute columns not readable yet: {e}")
        return info
    finally:
        db.close()

def money_text(cents: int, currency) -> str:
    cur    = (currency or "usd").lower()
    amount = f"{abs(cents) / 100:,.2f}"
    symbol = {"usd": "$", "gbp": "£", "eur": "€"}.get(cur)
    shown  = f"{symbol}{amount}" if symbol else f"{amount} {cur.upper()}"
    return f"-{shown}" if cents < 0 else shown

def discord_time(dt) -> str:
    return f"<t:{int(dt.timestamp())}:f>" if dt else "unknown"

def order_source(order_ref) -> str:
    """Where a key came from: a Stripe order, an owner's /gen, or /genkey."""
    if not order_ref:
        return "Created with /genkey"
    if order_ref.startswith("owner:"):
        parts = order_ref.split(":")
        who = f"<@{parts[1]}>" if len(parts) == 3 and parts[1].isdigit() else "an owner"
        return f"Issued by {who} with /gen keylicense"
    return f"Website order\n`{order_ref}`"

@app_commands.command(name="order", description="(Owner) Look up an order by its order ID or key")
@app_commands.describe(order_id="The order ID from the buyer (starts with cs_), or the key itself")
@app_commands.guild_only()
async def order_lookup(interaction: discord.Interaction, order_id: str):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        info = await asyncio.to_thread(db_find_order, order_id)
    except Exception as e:
        print(f"[!] /order failed: {e}")
        await interaction.followup.send("I couldn't search the orders. Check the bot logs.",
                                        ephemeral=True)
        return
    print(f"[keys] {interaction.user.id} ran /order -> {'found' if info else 'no match'}")
    if info is None:
        await interaction.followup.send(
            "No order or key matches that. Check the ID and try again.", ephemeral=True)
        return

    if info["claimed_by"] == REVOKED_BY:
        status = "Revoked, it can't be redeemed"
    elif info["claimed_by"] is not None:
        status = (f"Redeemed by <@{info['claimed_by']}>"
                  + (f" on {discord_time(info['claimed_at'])}" if info["claimed_at"] else ""))
    else:
        status = "Not redeemed yet"
    if info.get("dispute_at"):
        status += f"\n⚠️ Payment disputed {discord_time(info['dispute_at'])}."
        if info["claimed_by"] not in (None, REVOKED_BY):
            status += (" Their access has been ended." if info.get("dispute_handled_at")
                       else " The bot is ending their access.")

    pay = info["pay"]
    if pay:
        lines = []
        if pay["subtotal"] is not None:
            lines.append(f"Subtotal: {money_text(pay['subtotal'], pay['currency'])}")
        if pay["discount"]:
            lines.append(f"Discount: {money_text(-pay['discount'], pay['currency'])}")
        if pay["tax"]:
            lines.append(f"Tax / VAT: {money_text(pay['tax'], pay['currency'])}")
        lines.append(f"**Total: {money_text(pay['total'], pay['currency'])} "
                     f"{(pay['currency'] or 'usd').upper()}**")
        payment = "\n".join(lines)
    else:
        payment = "No amounts saved for this key."

    embed = discord.Embed(title="Order lookup", color=0x7C3AED)
    embed.add_field(name="Source", value=order_source(info["order_ref"]), inline=False)
    embed.add_field(name="Key", value=f"```\n{info['key']}\n```", inline=False)
    embed.add_field(name="Plan", value=PLAN_LABELS.get(info["plan"], info["plan"]), inline=True)
    embed.add_field(name="Created", value=discord_time(info["created_at"]), inline=True)
    embed.add_field(name="Status", value=status, inline=False)
    embed.add_field(name="Payment", value=payment, inline=False)
    if info["buyer_email"]:
        embed.add_field(name="Buyer email", value=info["buyer_email"], inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Key info (owners) ───────────────────────────────────────────────────────
def key_source_short(order_ref) -> str:
    """Where a key came from, in a few words (order_source has the long version)."""
    if not order_ref:
        return "/genkey"
    if order_ref.startswith("owner:"):
        parts = order_ref.split(":")
        return f"Owner key by <@{parts[1]}>" if len(parts) == 3 and parts[1].isdigit() else "Owner key"
    return "Website order"

def keyinfo_access_text(info: dict) -> str:
    if info["live_ends"]:
        return "\n".join(f"Live, ends {access_end_text(end)}" for end in info["live_ends"])
    if info["last_end"]:
        return f"No live access.\nLast ended {access_end_text(info['last_end'])}"
    return "No live access."

def keyinfo_role_text(in_server, has_role: bool, live: bool) -> str:
    if in_server is None:
        return "Couldn't check (Discord lookup failed)"
    if not in_server:
        return "Not in this server, but their access is still live" if live else "Not in this server"
    if has_role:
        return ("Has the Tracker Access role" if live else
                "Has the Tracker Access role, but no live access. `/revoke user` takes it off.")
    return ("Doesn't have the Tracker Access role, but has live access" if live else
            "Doesn't have the Tracker Access role")

def keyinfo_keys_text(info: dict) -> str:
    total = info["key_total"]
    if not total:
        return "None redeemed"
    plans = info["plans"]
    parts = [f"{plans[p]} × {PLAN_LABELS.get(p, p)}" for p in plans]
    span  = sum((KEY_PLANS[p] * n for p, n in plans.items() if p in KEY_PLANS), timedelta())
    head  = f"**{total}** redeemed: {', '.join(parts)}"
    if span.days:
        head += f" ({span.days} day{'s' if span.days != 1 else ''} of key time)"
    lines = []
    for k in info["keys"]:
        when = f" · <t:{int(k['claimed_at'].timestamp())}:d>" if k["claimed_at"] else ""
        lines.append(f"`{k['key']}` · {PLAN_LABELS.get(k['plan'], k['plan'])}{when} · {key_source_short(k['order_ref'])}")
    older = total - len(lines)
    while lines and len("\n".join([head, *lines, "…"])) > 1000:   # keep inside Discord's field limit
        lines.pop()
        older = total - len(lines)
    if older:
        lines.append(f"…and {older} older")
    return "\n".join([head, *lines])

@app_commands.command(name="keyinfo", description="(Owner) See a user's keys and Tracker Access")
@app_commands.describe(user="Who to look up")
@app_commands.guild_only()
async def keyinfo_cmd(interaction: discord.Interaction, user: discord.User):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    try:
        info = await asyncio.to_thread(db_user_key_info, user.id, guild.id)
    except Exception as e:
        print(f"[!] /keyinfo failed: {e}")
        await interaction.followup.send("I couldn't look that up. Check the bot logs.", ephemeral=True)
        return

    in_server, has_role = None, False
    try:
        # fetch, don't use the cache: without the members intent cached roles can be stale
        member    = await guild.fetch_member(user.id)
        in_server = True
        has_role  = any(r.id == TRACKER_ROLE_ID for r in member.roles)
    except discord.NotFound:
        in_server = False
    except discord.HTTPException as e:
        print(f"[!] Could not look up member {user.id}: {e}")
    print(f"[keys] {interaction.user.id} ran /keyinfo on {user.id}")

    embed = discord.Embed(title="Key info", description=f"{user.mention} (`{user.id}`)", color=0x7C3AED)
    embed.add_field(name="Tracker Access", value=keyinfo_access_text(info), inline=False)
    embed.add_field(name="Role", value=keyinfo_role_text(in_server, has_role, bool(info["live_ends"])), inline=False)
    embed.add_field(name="Keys redeemed", value=keyinfo_keys_text(info), inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


client.tree.add_command(claim_key)
client.tree.add_command(genkey)
client.tree.add_command(owner_gen)
client.tree.add_command(owner_revoke)
client.tree.add_command(owner_extend)
client.tree.add_command(order_lookup)
client.tree.add_command(keyinfo_cmd)
client.run(TOKEN)
