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
TYPE_CHANNELS_PATH = os.path.join(os.path.dirname(__file__), "type_channels.json")

SHORT_LABEL = {
    "+30 days old": "30d",
    "+1 year old": "1 Year",
    "5+ years old": "5+ Year",
    "dump": "Dump",
}

def load_type_channels():
    try:
        with open(TYPE_CHANNELS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for account_type, channel_id in raw.items():
            if account_type.startswith("_"):
                continue
            try:
                out[account_type] = int(channel_id)
            except (TypeError, ValueError):
                print(f"[!] Skipping type channel for '{account_type}' — invalid channel ID: {channel_id!r}")
        return out
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load type channels: {e}")
        return {}

TYPE_CHANNELS = load_type_channels()

# ── Encryption ──────────────────────────────────────────────────────
ENCRYPTION_KEY = os.environ.get("ACCOUNT_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ACCOUNT_ENCRYPTION_KEY environment variable is required (same key "
        "used by the web app's backend — accounts are encrypted with it)."
    )
_fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)


def decrypt_secret(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return "⚠ corrupted — could not decrypt this password"


# ── Account type categories ─────────────────────────────────────────
ACCOUNT_TYPES = ["+30 days old", "+1 year old", "5+ years old", "dump"]
DEFAULT_TYPES  = ["+30 days old"]


# ── Cooldown config ───────────────────────────────────────────────
COOLDOWN_ROLES_PATH = os.path.join(os.path.dirname(__file__), "cooldown_roles.json")

def load_cooldown_roles():
    try:
        with open(COOLDOWN_ROLES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(role_id): int(seconds) for role_id, seconds in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load cooldown roles: {e}")
        return {}

COOLDOWN_ROLES  = load_cooldown_roles()
active_cooldowns = {}

def get_user_cooldown(member):
    cooldowns = [
        COOLDOWN_ROLES[role.id]
        for role in getattr(member, "roles", [])
        if role.id in COOLDOWN_ROLES
    ]
    return min(cooldowns) if cooldowns else None

def get_remaining_cooldown(user_id):
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
    try:
        with open(TYPE_ROLES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        out = {}
        for role_id, types in raw.items():
            if role_id.startswith("_"):
                continue
            out[int(role_id)] = [t for t in types if t in ACCOUNT_TYPES]
        return out
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load type roles: {e}")
        return {}

TYPE_ROLES = load_type_roles()

def get_allowed_types(member) -> list:
    allowed = set(DEFAULT_TYPES)
    for role in getattr(member, "roles", []):
        if role.id in TYPE_ROLES:
            allowed.update(TYPE_ROLES[role.id])
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
    db = SessionLocal()
    try:
        q = db.query(Account)
        if account_type:
            q = q.filter(Account.account_type == account_type)
        return q.count()
    finally:
        db.close()

class Account(Base):
    __tablename__ = "accounts"
    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String(150), nullable=False)
    password     = Column(String(500), nullable=False)
    account_type = Column(String(50), nullable=True, default="+30 days old")
    created_at   = Column(DateTime(timezone=True), default=utcnow)
    updated_at   = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

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
        if not VOICE_CHANNEL_ID:
            return
        if count is None:
            count = count_alts()
        await self._rename_channel(VOICE_CHANNEL_ID, f"{count} Alts Stocked 🇬🇧")

    async def update_type_channels(self, only_type: str = None):
        types_to_update = [only_type] if only_type else list(TYPE_CHANNELS.keys())
        for account_type in types_to_update:
            channel_id = TYPE_CHANNELS.get(account_type)
            if not channel_id:
                continue
            count = count_alts(account_type)
            label = SHORT_LABEL.get(account_type, account_type)
            await self._rename_channel(channel_id, f"{count} {label} Alts Stocked 🇬🇧")

    @tasks.loop(minutes=30)
    async def refresh_alt_count(self):
        await self.update_alt_count_channel()
        await self.update_type_channels()

    @refresh_alt_count.before_loop
    async def before_refresh_alt_count(self):
        await self.wait_until_ready()

client = CredBot()

def format_duration(seconds):
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if secs or not parts: parts.append(f"{secs}s")
    return " ".join(parts)

async def _handle_generate(interaction: discord.Interaction, requested_type: str):
    user_id = interaction.user.id

    allowed = get_allowed_types(interaction.user)
    if requested_type not in allowed:
        await interaction.response.send_message(
            f"You don't have permission to request **{requested_type}** accounts.\n"
            f"Your allowed types: {', '.join(allowed)}",
            ephemeral=True,
        )
        return

    remaining = get_remaining_cooldown(user_id)
    if remaining > 0:
        await interaction.response.send_message(
            f"You're on cooldown. Try again in {format_duration(remaining)}.",
            ephemeral=True,
        )
        return

    db = SessionLocal()
    try:
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


generate_group = app_commands.Group(name="generate", description="Get and delete the next credential from the database")

@generate_group.command(name="30d", description="Get a +30 days old account")
async def generate_30d(interaction: discord.Interaction):
    await _handle_generate(interaction, "+30 days old")

@generate_group.command(name="1y", description="Get a +1 year old account")
async def generate_1y(interaction: discord.Interaction):
    await _handle_generate(interaction, "+1 year old")

client.tree.add_command(generate_group)

client.run(TOKEN)
