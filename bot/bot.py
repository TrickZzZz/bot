import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

TOKEN = os.environ.get("TOKEN")
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", "1521166172182024475"))

# ── Database setup ────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(DATABASE_URL, echo=False)
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


@client.tree.command(name="creds", description="Get and delete the next credential from the database")
async def creds_command(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        # Fetch the oldest account (FIFO)
        account = db.query(Account).order_by(Account.id.asc()).first()

        if not account:
            await interaction.response.send_message("No more credentials left.", ephemeral=True)
            return

        msg = f"```\nusername: {account.username}\npassword: {account.password}\n```\n-# Save the account! It will get deleted in 15 minutes."

        # Delete it after sending
        db.delete(account)
        db.commit()

        await interaction.response.send_message(msg, ephemeral=True)

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