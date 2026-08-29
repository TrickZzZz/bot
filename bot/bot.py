import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timezone
import os
import time
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from cryptography.fernet import Fernet, InvalidToken
import json

load_dotenv()

TOKEN            = os.environ.get("TOKEN")
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", "1521166172182024475"))

# ── Encryption ───────────────────────────────────────────────────────────
ENCRYPTION_KEY = os.environ.get("ACCOUNT_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError("ACCOUNT_ENCRYPTION_KEY environment variable is required.")
_fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)

def decrypt_secret(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return "⚠ corrupted — could not decrypt"

# ── Account types ────────────────────────────────────────────────────────
ACCOUNT_TYPES = ["+30 days old", "+1 year old", "5+ years old", "dump"]
DEFAULT_TYPES  = ["+30 days old"]

# ── Cooldown config ──────────────────────────────────────────────────────
COOLDOWN_ROLES_PATH = os.path.join(os.path.dirname(__file__), "cooldown_roles.json")

def load_cooldown_roles():
    try:
        with open(COOLDOWN_ROLES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(role_id): int(seconds) for role_id, seconds in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}

COOLDOWN_ROLES   = load_cooldown_roles()
active_cooldowns = {}

def get_user_cooldown(member):
    cooldowns = [COOLDOWN_ROLES[role.id] for role in getattr(member, "roles", []) if role.id in COOLDOWN_ROLES]
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

# ── Account type permissions ─────────────────────────────────────────────
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
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}

TYPE_ROLES = load_type_roles()

def get_allowed_types(member) -> list:
    allowed = set(DEFAULT_TYPES)
    for role in getattr(member, "roles", []):
        if role.id in TYPE_ROLES:
            allowed.update(TYPE_ROLES[role.id])
    return [t for t in ACCOUNT_TYPES if t in allowed]

# ── Database ─────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def utcnow():
    return datetime.now(timezone.utc)

class Account(Base):
    __tablename__ = "accounts"
    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String(150), nullable=False)
    password     = Column(String(500), nullable=False)
    account_type = Column(String(50), nullable=True, default="+30 days old")
    cookie       = Column(Text, nullable=True, default="")
    region       = Column(String(10), nullable=True, default="")
    created_at   = Column(DateTime(timezone=True), default=utcnow)
    updated_at   = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

Base.metadata.create_all(bind=engine)

def count_alts():
    db = SessionLocal()
    try:
        return db.query(Account).count()
    finally:
        db.close()

# ── Discord bot ──────────────────────────────────────────────────────────
class CredBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"[*] Synced commands — logged in as {self.user}")

    async def on_ready(self):
        print(f"[*] Bot ready. {count_alts()} accounts in database.")
        await self.update_stock_channel()
        if not self.refresh_stock.is_running():
            self.refresh_stock.start()

    async def update_stock_channel(self, count=None):
        if not VOICE_CHANNEL_ID:
            return
        if count is None:
            count = count_alts()
        channel = self.get_channel(VOICE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(VOICE_CHANNEL_ID)
            except discord.HTTPException as e:
                print(f"[!] Could not fetch channel: {e}")
                return
        new_name = f"{count} Alts Stocked"
        if channel.name == new_name:
            return
        try:
            await channel.edit(name=new_name)
            print(f"[*] Channel updated: {new_name}")
        except discord.HTTPException as e:
            print(f"[!] Could not rename channel: {e}")

    @tasks.loop(minutes=30)
    async def refresh_stock(self):
        await self.update_stock_channel()

    @refresh_stock.before_loop
    async def before_refresh(self):
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

# ── Generate handler ─────────────────────────────────────────────────────
async def _handle_generate(interaction: discord.Interaction, requested_type: str, requested_region: str = None):
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
        query = db.query(Account).filter(Account.account_type == requested_type)
        if requested_region:
            query = query.filter(Account.region == requested_region.upper())
        account = query.order_by(Account.id.asc()).first()

        if not account:
            region_msg = f" from **{requested_region.upper()}**" if requested_region else ""
            await interaction.response.send_message(
                f"No **{requested_type}**{region_msg} accounts left.", ephemeral=True
            )
            return

        plain_password = decrypt_secret(account.password)
        cookie         = account.cookie or ""
        region_tag     = f" `{account.region}`" if account.region else ""

        cred_block = f"```\nusername: {account.username}\npassword: {plain_password}\n```"

        if cookie:
            cookie_block = (
                f"\n**Cookie** (inject with Cookie-Editor):\n"
                f"||```\n{cookie}\n```||"
            )
        else:
            cookie_block = ""

        msg = (
            cred_block
            + cookie_block
            + f"\n-# {requested_type}{region_tag} — Save everything! Deleted in 15 minutes."
        )

        db.delete(account)
        db.commit()

        if len(msg) > 1900:
            import io
            file_content = f"username: {account.username}\npassword: {plain_password}\ncookie: {cookie}\n"
            file_obj     = io.BytesIO(file_content.encode())
            discord_file = discord.File(file_obj, filename=f"{account.username}.txt")
            short_msg    = (
                f"```\nusername: {account.username}\npassword: {plain_password}\n```\n"
                f"-# Cookie attached as file — {requested_type}{region_tag}. Deleted in 15 minutes."
            )
            await interaction.response.send_message(short_msg, file=discord_file, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

        cooldown = get_user_cooldown(interaction.user)
        if cooldown is not None:
            active_cooldowns[user_id] = time.time() + cooldown

        remaining_total = db.query(Account).count()
        print(f"[+] Sent {account.username} ({requested_type}{', ' + account.region if account.region else ''}) — {remaining_total} remaining")

        await client.update_stock_channel(remaining_total)

    except Exception as e:
        db.rollback()
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        raise
    finally:
        db.close()

# ── Slash commands ───────────────────────────────────────────────────────
generate_group = app_commands.Group(name="generate", description="Get an account from the vault")

@generate_group.command(name="30d", description="Get a +30 days old account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def generate_30d(interaction: discord.Interaction, region: str = None):
    await _handle_generate(interaction, "+30 days old", region)

@generate_group.command(name="1y", description="Get a +1 year old account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def generate_1y(interaction: discord.Interaction, region: str = None):
    await _handle_generate(interaction, "+1 year old", region)

@generate_group.command(name="5y", description="Get a 5+ years old account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def generate_5y(interaction: discord.Interaction, region: str = None):
    await _handle_generate(interaction, "5+ years old", region)

@generate_group.command(name="dump", description="Get a dump account")
@app_commands.describe(region="Region code e.g. GB, DE, US (leave blank for any)")
async def generate_dump(interaction: discord.Interaction, region: str = None):
    await _handle_generate(interaction, "dump", region)

client.tree.add_command(generate_group)
client.run(TOKEN)
