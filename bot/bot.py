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

for _rid in (1518294955611787475, 1518571590516740096, 1522920181104381953):
    TYPE_ROLES[_rid] = ["5+ years old", "dump"]

def get_allowed_types(member):
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
KEY_PLANS       = {"day": timedelta(days=1), "week": timedelta(days=7)}
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
OWNER_KEY_PLANS = {"1d": "day", "1w": "week"}     # /gen keylicense choice -> key plan
PLAN_LABELS     = {"day": "1 day", "week": "1 week"}
REVOKED_BY      = 0   # license_keys.claimed_by sentinel: revoked by an owner, never redeemable

class LicenseKey(Base):
    """One row per sold key. Inserted by the shop (or /genkey), redeemed by /claim_key."""
    __tablename__ = "license_keys"
    id         = Column(Integer, primary_key=True)
    key        = Column(String(64), unique=True, nullable=False, index=True)
    plan       = Column(String(10), nullable=False)             # "day" | "week"
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


# ── Core generate logic ────────────────────────────────────────────────────
async def do_generate(interaction: discord.Interaction, account_type: str, region: str = None):
    user_id = interaction.user.id
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
    def __init__(self, account_type: str):
        self.account_type = account_type
        options = [discord.SelectOption(label="Any region", value="any", default=True)]
        for code, name in REGIONS:
            options.append(discord.SelectOption(label=f"{name} ({code})", value=code))
        # No custom_id — this is a short-lived ephemeral view, not a persistent one.
        super().__init__(
            placeholder="Select a region (optional)...",
            options=options, min_values=1, max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        region = None if self.values[0] == "any" else self.values[0]
        await do_generate(interaction, self.account_type, region)


class RegionView(discord.ui.View):
    def __init__(self, account_type: str):
        super().__init__(timeout=120)
        self.add_item(RegionSelect(account_type))

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
        embed = discord.Embed(
            description=f"Select a region for your **{account_type}** account.\nChoose **Any region** to get the next available.",
            color=0x7C3AED,
        )
        await interaction.response.send_message(embed=embed, view=RegionView(account_type), ephemeral=True)

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


client = CredBot()


# ── Slash commands ─────────────────────────────────────────────────────────
generate_group = app_commands.Group(name="generate", description="Generate an account")

@generate_group.command(name="30d", description="+30 days old account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def gen_30d(interaction: discord.Interaction, region: str = None):
    await do_generate(interaction, "+30 days old", region)

@generate_group.command(name="1y", description="+1 year old account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def gen_1y(interaction: discord.Interaction, region: str = None):
    await do_generate(interaction, "+1 year old", region)

@generate_group.command(name="5y", description="5+ years old account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def gen_5y(interaction: discord.Interaction, region: str = None):
    await do_generate(interaction, "5+ years old", region)

@generate_group.command(name="dump", description="Dump account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def gen_dump(interaction: discord.Interaction, region: str = None):
    await do_generate(interaction, "dump", region)

client.tree.add_command(generate_group)


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
                            app_commands.Choice(name="Week", value="week")])
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
                                app_commands.Choice(name="1w (1 week)", value="1w")])
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


owner_revoke = app_commands.Group(name="revoke", description="Owner tools", guild_only=True)

@owner_revoke.command(name="key", description="(Owner) Stop an unredeemed key from being used")
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
        who = f"<@{detail}>" if detail else "someone"
        msg = f"That key was already redeemed by {who}. Use `/revoke user` to end their access."
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

    try:
        ended = await asyncio.to_thread(db_end_user_access, user.id, guild.id)
    except Exception as e:
        print(f"[!] /revoke user DB error: {e}")
        await interaction.followup.send(
            "I couldn't update the database, so nothing was changed.", ephemeral=True)
        return

    had_role = role in user.roles
    if had_role:
        try:
            await user.remove_roles(role, reason=f"Tracker access revoked by {interaction.user}")
        except discord.HTTPException as e:  # includes Forbidden
            print(f"[!] Could not remove role from {user.id}: {e}")
            for access_id in ended:         # put the windows back: nothing changed
                try:
                    await asyncio.to_thread(db_reactivate, access_id)
                except Exception as undo_err:
                    print(f"[!] CRITICAL: could not reopen access {access_id}: {undo_err}")
            await interaction.followup.send(
                "I couldn't remove the role, so nothing was changed. Check that I have "
                "Manage Roles and that my role sits above Tracker Access.", ephemeral=True)
            return

    if not ended and not had_role:
        await interaction.followup.send(
            f"{user.mention} has no Tracker Access to revoke.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Your access has ended",
        description=("Staff have ended your **Tracker Access** and removed the role. "
                     "If you think that's a mistake, contact staff."),
        color=0xEF4444)
    delivered = await try_dm(user, embed)
    print(f"[keys] {interaction.user.id} revoked {user.id}: role={'removed' if had_role else 'absent'}, "
          f"windows={len(ended)}, DM {'sent' if delivered else 'failed'}")
    await interaction.followup.send(
        f"Revoked {user.mention}.\n"
        f"Role removed: {'yes' if had_role else 'they did not have it'}\n"
        f"Key windows ended: {len(ended)}\n"
        f"DM: {'sent' if delivered else 'not delivered (their DMs are closed)'}",
        ephemeral=True)


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
                "claimed_at": aware(row.claimed_at), "buyer_email": row.buyer_email, "pay": None}
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


client.tree.add_command(claim_key)
client.tree.add_command(genkey)
client.tree.add_command(owner_gen)
client.tree.add_command(owner_revoke)
client.tree.add_command(order_lookup)
client.run(TOKEN)
