import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timezone
import json
import os
import time
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base
from cryptography.fernet import Fernet, InvalidToken

load_dotenv()

TOKEN = os.environ.get("TOKEN")
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", "1521166172182024475"))

# ── Per-type stock channels ──────────────────────────────────────────
# Each entry renames a SEPARATE voice channel to show that specific type's
# live count, alongside (not replacing) the single VOICE_CHANNEL_ID total.
TYPE_CHANNELS_PATH = os.path.join(os.path.dirname(__file__), "type_channels.json")

SHORT_LABEL = {
    "+30 days old": "30d",
    "+1 year old": "1 Year",
    "5+ years old": "5+ Year",
    "dump": "Dump",
}

def load_type_channels():
    """Load the {account_type: channel_id} mapping from disk. Entries with
    a missing/placeholder channel ID are skipped rather than crashing."""
    try:
        with open(TYPE_CHANNELS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for account_type, channel_id in raw.items():
            if account_type.startswith("_"):
                continue  # skip comment/metadata keys
            try:
                out[account_type] = int(channel_id)
            except (TypeError, ValueError):
                print(f"[!] Skipping type channel for '{account_type}' — invalid channel ID: {channel_id!r}")
        return out
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load type channels: {e}")
        return {}

TYPE_CHANNELS = load_type_channels()

# ── Encryption (matches backend/app/security.py exactly) ──────────
ENCRYPTION_KEY = os.environ.get("ACCOUNT_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ACCOUNT_ENCRYPTION_KEY environment variable is required (same key "
        "used by the web app's backend — accounts are encrypted with it)."
    )
_fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)


def decrypt_secret(token: str) -> str:
    """Decrypt a stored password. Falls back to a clear placeholder instead
    of crashing if a row somehow isn't valid Fernet ciphertext."""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return "⚠ corrupted — could not decrypt this password"


# ── Account type categories ─────────────────────────────────────────
# Same labels used everywhere else in the project (generator, vault, etc.)
ACCOUNT_TYPES = ["+30 days old", "+1 year old", "5+ years old", "dump"]

# Types every user can request regardless of role.
DEFAULT_TYPES = ["+30 days old"]


# ── Cooldown config ───────────────────────────────────────────────
COOLDOWN_ROLES_PATH = os.path.join(os.path.dirname(__file__), "cooldown_roles.json")

def load_cooldown_roles():
    """Load the {role_id: cooldown_seconds} mapping from disk."""
    try:
        with open(COOLDOWN_ROLES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(role_id): int(seconds) for role_id, seconds in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load cooldown roles: {e}")
        return {}

COOLDOWN_ROLES = load_cooldown_roles()

active_cooldowns = {}

def get_user_cooldown(member):
    """Return the lowest cooldown (in seconds) among the roles a member has."""
    cooldowns = [
        COOLDOWN_ROLES[role.id]
        for role in getattr(member, "roles", [])
        if role.id in COOLDOWN_ROLES
    ]
    return min(cooldowns) if cooldowns else None

def get_remaining_cooldown(user_id):
    """Return seconds left on a user's cooldown, or 0 if none is active."""
    ends_at = active_cooldowns.get(user_id)
    if ends_at is None:
        return 0
    remaining = ends_at - time.time()
    if remaining <= 0:
        active_cooldowns.pop(user_id, None)
        return 0
    return remaining


# ── Account type permissions ────────────────────────────────────────
TYPE_ROLES_PATH = os.path.join(os.path.dirname(__file__), "type_roles.json")

def load_type_roles():
    """Load the {role_id: [account_type, ...]} mapping from disk. Each role
    UNLOCKS the listed types on top of DEFAULT_TYPES, which everyone gets
    regardless of role."""
    try:
        with open(TYPE_ROLES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for role_id, types in raw.items():
            if role_id.startswith("_"):
                continue  # skip comment/metadata keys
            out[int(role_id)] = [t for t in types if t in ACCOUNT_TYPES]
        return out
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load type roles: {e}")
        return {}

TYPE_ROLES = load_type_roles()

def get_allowed_types(member) -> list:
    """Every account type this member is allowed to request — the union of
    DEFAULT_TYPES plus whatever every role they hold individually unlocks."""
    allowed = set(DEFAULT_TYPES)
    for role in getattr(member, "roles", []):
        if role.id in TYPE_ROLES:
            allowed.update(TYPE_ROLES[role.id])
    # preserve ACCOUNT_TYPES' canonical ordering rather than set's arbitrary order
    return [t for t in ACCOUNT_TYPES if t in allowed]


# ── Database setup ────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def utcnow():
    return datetime.now(timezone.utc)

def count_alts(account_type=None):
    """Return the current number of stocked alts, optionally filtered by type."""
    db = SessionLocal()
    try:
        q = db.query(Account)
        if account_type:
            q = q.filter(Account.account_type == account_type)
        return q.count()
    finally:
        db.close()

class Account(Base):
    """A registered third-party account (the thing being managed)."""
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), nullable=False)
    password = Column(String(500), nullable=False)
    account_type = Column(String(50), nullable=True, default="+30 days old")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

# Create tables if they don't exist (does NOT alter existing tables —
# migration for the account_type column itself lives in the web backend's
# main.py, which both services share the same database with).
Base.metadata.create_all(bind=engine)

# ── Discord Bot ──────────────────────────────────────────────────
class CredBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"[*] Synced commands. Logged in as {self.user}")

    async def on_ready(self):
        total = count_alts()
        print(f"[*] Bot is ready. {total} accounts in database.")
        await self.update_alt_count_channel()
        await self.update_type_channels()
        if not self.refresh_alt_count.is_running():
            self.refresh_alt_count.start()

    async def _rename_channel(self, channel_id: int, new_name: str):
        """Shared rename logic used by both the total-count channel and
        each per-type channel."""
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as e:
                print(f"[!] Could not fetch channel {channel_id}: {e}")
                return
        if channel.name == new_name:
            return
        try:
            await channel.edit(name=new_name)
            print(f"[*] Updated channel name to '{new_name}'")
        except discord.HTTPException as e:
            print(f"[!] Could not rename channel {channel_id}: {e}")

    async def update_alt_count_channel(self, count=None):
        """Rename the single TOTAL-across-all-types voice channel."""
        if not VOICE_CHANNEL_ID:
            return
        if count is None:
            count = count_alts()
        await self._rename_channel(VOICE_CHANNEL_ID, f"{count} Alts Stocked")

    async def update_type_channels(self, only_type: str = None):
        """Rename each configured per-type voice channel to show that
        specific type's own live count. Pass only_type to refresh just one
        (e.g. right after a /creds pull) instead of re-checking every type."""
        types_to_update = [only_type] if only_type else list(TYPE_CHANNELS.keys())
        for account_type in types_to_update:
            channel_id = TYPE_CHANNELS.get(account_type)
            if not channel_id:
                continue
            count = count_alts(account_type)
            label = SHORT_LABEL.get(account_type, account_type)
            await self._rename_channel(channel_id, f"{count} {label} Alts Stocked")

    @tasks.loop(minutes=30)
    async def refresh_alt_count(self):
        await self.update_alt_count_channel()
        await self.update_type_channels()

    @refresh_alt_count.before_loop
    async def before_refresh_alt_count(self):
        await self.wait_until_ready()

client = CredBot()

def format_duration(seconds):
    """Human-friendly duration, e.g. 90 -> '1m 30s'."""
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)

TYPE_CHOICES = [app_commands.Choice(name=t, value=t) for t in ACCOUNT_TYPES]

@client.tree.command(name="creds", description="Get and delete the next credential from the database")
@app_commands.describe(account_type="Which account type do you want?")
@app_commands.choices(account_type=TYPE_CHOICES)
async def creds_command(interaction: discord.Interaction, account_type: app_commands.Choice[str]):
    user_id = interaction.user.id
    requested_type = account_type.value

    # Role-based permission check — normal users only get DEFAULT_TYPES;
    # specific roles unlock premium types on top (see type_roles.json).
    allowed = get_allowed_types(interaction.user)
    if requested_type not in allowed:
        await interaction.response.send_message(
            f"You don't have permission to request **{requested_type}** accounts.\n"
            f"Your allowed types: {', '.join(allowed)}",
            ephemeral=True,
        )
        return

    # Block the request if the user is still on cooldown.
    remaining = get_remaining_cooldown(user_id)
    if remaining > 0:
        await interaction.response.send_message(
            f"You're on cooldown. Try again in {format_duration(remaining)}.",
            ephemeral=True,
        )
        return

    db = SessionLocal()
    try:
        # Fetch the oldest account of the requested type (FIFO within that type)
        account = (
            db.query(Account)
            .filter(Account.account_type == requested_type)
            .order_by(Account.id.asc())
            .first()
        )
        if not account:
            await interaction.response.send_message(
                f"No **{requested_type}** credentials left.", ephemeral=True
            )
            return

        plain_password = decrypt_secret(account.password)
        msg = (
            f"```\nusername: {account.username}\npassword: {plain_password}\n```\n"
            f"-# {requested_type} — Save the account! It will get deleted in 15 minutes."
        )

        db.delete(account)
        db.commit()

        await interaction.response.send_message(msg, ephemeral=True)

        cooldown = get_user_cooldown(interaction.user)
        if cooldown is not None:
            active_cooldowns[user_id] = time.time() + cooldown
            print(f"[+] Applied {cooldown}s cooldown to user {user_id}")

        remaining_total = db.query(Account).count()
        print(f"[+] Sent account ID {account.id} ({requested_type}, {remaining_total} total remaining)")

        await client.update_alt_count_channel(remaining_total)
        await client.update_type_channels(only_type=requested_type)

    except Exception as e:
        db.rollback()
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        raise
    finally:
        db.close()

client.run(TOKEN)
