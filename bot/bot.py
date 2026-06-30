import discord
from discord import app_commands
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

TOKEN = os.environ.get("TOKEN")

# ── Database setup ────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


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
        db = SessionLocal()
        try:
            total = db.query(Account).count()
            print(f"[*] Bot is ready. {total} accounts in database.")
        finally:
            db.close()


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

        msg = f"```\nusername: {account.username}\npassword: {account.password}\n```"

        # Delete it after sending
        db.delete(account)
        db.commit()

        await interaction.response.send_message(msg)

        remaining = db.query(Account).count()
        print(f"[+] Sent account ID {account.id} ({remaining} remaining)")

    except Exception as e:
        db.rollback()
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        raise
    finally:
        db.close()


client.run(TOKEN)