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

# ── Encryption (matches backend/app/security.py exactly) ──────────
# Stored account passwords are encrypted at rest with Fernet, not plaintext —
# this bot must decrypt them the same way the web app's own API does before
# ever showing one to a user.
ENCRYPTION_KEY = os.environ.get("ACCOUNT_ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ACCOUNT_ENCRYPTION_KEY environment variable is required (same key "
        "used by the web app's backend — accounts are encrypted with it)."
    )
_fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)


def decrypt_secret(token: str) -> str:
    """Decrypt a stored password. Falls back to a clear placeholder instead
    of crashing if a row somehow isn't valid Fernet ciphertext (e.g. a
    leftover from before encryption was correctly wired in everywhere)."""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, ValueError):
        return "⚠ corrupted — could not decrypt this password"


# ── Cooldown config ───────────────────────────────────────────────
COOLDOWN_ROLES_PATH = os.path.join(os.path.dirname(__file__), "cooldown_roles.json")

def load_cooldown_roles():
    """Load the {role_id: cooldown_seconds} mapping from disk."""
    try:
        with open(COOLDOWN_ROLES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Normalize keys to int so they can be matched against Discord role IDs.
        return {int(role_id): int(seconds) for role_id, seconds in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[!] Could not load cooldown roles: {e}")
        return {}

COOLDOWN_ROLES = load_cooldown_roles()

# Maps a Discord user ID to the UNIX timestamp (seconds) when their cooldown ends.
active_cooldowns = {}

def get_user_cooldown(member):
    """Return the lowest cooldown (in seconds) among the roles a member has.
    A lower cooldown means a more privileged role wins. Returns None if the
    member has no configured cooldown role.
    """
    cooldowns = [
        COOLDOWN_ROLES[role.id]
        for role in getattr(member, "roles", [])
        if role.id in COOLDOWN_ROLES
    ]
    return min(cooldowns) if cooldowns else None

def get_remaining_cooldown(user_id):
    """Return seconds left on a user's cooldown, or 0 if none is active.
    Expired cooldowns are cleaned up so the user can run the command again.
    """
    ends_at = active_cooldowns.get(user_id)
    if ends_at is None:
        return 0
    remaining = ends_at - time.time()
    if remaining <= 0:
        active_cooldowns.pop(user_id, None)
        return 0
    return remaining

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

def count_alts():
    """Return the current number of stocked alts in the database."""
    db = SessionLocal()
    try:
        return db.query(Account).count()
    finally:
        db.close()

class Account(Base):
    """A registered third-party account (the thing being managed)."""
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), nullable=False)
    password = Column(String(500), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

# Create tables if they don't exist
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
        # Update the channel name on startup and keep it refreshed every 30 min.
        await self.update_alt_count_channel()
        if not self.refresh_alt_count.is_running():
            self.refresh_alt_count.start()

    async def update_alt_count_channel(self, count=None):
        """Rename the voice channel to reflect the number of stocked alts."""
        if not VOICE_CHANNEL_ID:
            return
        if count is None:
            count = count_alts()
        channel = self.get_channel(VOICE_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(VOICE_CHANNEL_ID)
            except discord.HTTPException as e:
                print(f"[!] Could not fetch channel {VOICE_CHANNEL_ID}: {e}")
                return
        new_name = f"{count} Alts Stocked"
        if channel.name == new_name:
            return
        try:
            await channel.edit(name=new_name)
            print(f"[*] Updated channel name to '{new_name}'")
        except discord.HTTPException as e:
            print(f"[!] Could not rename channel {VOICE_CHANNEL_ID}: {e}")

    @tasks.loop(minutes=30)
    async def refresh_alt_count(self):
        await self.update_alt_count_channel()

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

@client.tree.command(name="creds", description="Get and delete the next credential from the database")
async def creds_command(interaction: discord.Interaction):
    user_id = interaction.user.id

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
        # Fetch the oldest account (FIFO)
        account = db.query(Account).order_by(Account.id.asc()).first()
        if not account:
            await interaction.response.send_message("No more credentials left.", ephemeral=True)
            return

        plain_password = decrypt_secret(account.password)
        msg = f"```\nusername: {account.username}\npassword: {plain_password}\n```\n-# Save the account! It will get deleted in 15 minutes."

        # Delete it after sending
        db.delete(account)
        db.commit()

        await interaction.response.send_message(msg, ephemeral=True)

        # Apply the cooldown based on the user's lowest-cooldown role.
        cooldown = get_user_cooldown(interaction.user)
        if cooldown is not None:
            active_cooldowns[user_id] = time.time() + cooldown
            print(f"[+] Applied {cooldown}s cooldown to user {user_id}")

        remaining = db.query(Account).count()
        print(f"[+] Sent account ID {account.id} ({remaining} remaining)")

        # Reflect the new stock count on the voice channel name.
        await client.update_alt_count_channel(remaining)

    except Exception as e:
        db.rollback()
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        raise
    finally:
        db.close()

client.run(TOKEN)
