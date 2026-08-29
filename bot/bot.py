import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timezone
import os
import io
import time
import json
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from cryptography.fernet import Fernet, InvalidToken

load_dotenv()

TOKEN            = os.environ.get("TOKEN")
VOICE_CHANNEL_ID = int(os.environ.get("VOICE_CHANNEL_ID", "1521166172182024475"))

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

        pw     = decrypt_secret(account.password)
        cookie = account.cookie or ""

        db.delete(account)
        db.commit()

        if is_premium:
            premium_cooldowns[user_id] = time.time() + PREMIUM_COOLDOWN
        else:
            cd = get_user_cooldown(interaction.user)
            if cd:
                active_cooldowns[user_id] = time.time() + cd

        remaining_total = db.query(Account).count()
        print(f"[+] Sent {account.username} ({account_type}"
              f"{', ' + account.region if account.region else ''}) — {remaining_total} left")

        await client.update_stock_channel(remaining_total)

    except Exception as e:
        db.rollback()
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
        raise
    finally:
        db.close()

    embed = discord.Embed(color=0x7C3AED)
    embed.set_author(name=account.username,
                     icon_url="https://www.roblox.com/favicon.ico")
    embed.add_field(name="Username", value=f"`{account.username}`", inline=True)
    embed.add_field(name="Password", value=f"||`{pw}`||",           inline=True)
    embed.add_field(name="Type",     value=account_type,            inline=True)
    if account.region:
        embed.add_field(name="Region", value=account.region, inline=True)
    embed.set_footer(text="DeltaCore Alt Generator  \u2022  Save this — it will not be shown again")

    msg_kwargs: dict = {"embed": embed, "ephemeral": True}

    if cookie:
        cookie_block = f"**Cookie** (inject with Cookie-Editor):\n||```\n{cookie}\n```||"
        if len(cookie_block) + 300 > 1900:
            f_obj = io.BytesIO(
                f"username: {account.username}\npassword: {pw}\ncookie: {cookie}\n".encode())
            msg_kwargs["file"]    = discord.File(f_obj, filename=f"{account.username}.txt")
            msg_kwargs["content"] = "Cookie is too long for chat — attached as a file."
        else:
            msg_kwargs["content"] = cookie_block

    await interaction.response.send_message(**msg_kwargs)


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
        super().__init__(
            placeholder="Select a region (optional)...",
            options=options, min_values=1, max_values=1,
            custom_id=f"region_sel_{account_type.replace(' ','_').replace('+','').replace('/','')}"
        )

    async def callback(self, interaction: discord.Interaction):
        region = None if self.values[0] == "any" else self.values[0]
        await do_generate(interaction, self.account_type, region)


class RegionView(discord.ui.View):
    def __init__(self, account_type: str):
        super().__init__(timeout=60)
        self.add_item(RegionSelect(account_type))


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

    @discord.ui.button(label="30d", style=discord.ButtonStyle.primary,
                       custom_id="panel_30d", row=0)
    async def btn_30d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "+30 days old")

    @discord.ui.button(label="1 Year", style=discord.ButtonStyle.primary,
                       custom_id="panel_1y", row=0)
    async def btn_1y(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "+1 year old")

    @discord.ui.button(label="5 Years", style=discord.ButtonStyle.secondary,
                       custom_id="panel_5y", row=1)
    async def btn_5y(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "5+ years old")

    @discord.ui.button(label="Dump", style=discord.ButtonStyle.secondary,
                       custom_id="panel_dump", row=1)
    async def btn_dump(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_region_picker(interaction, "dump")


def build_panel_embed(counts: dict) -> discord.Embed:
    embed = discord.Embed(
        title="DeltaCore Vault",
        description=(
            "Press a button to generate an account.\n"
            "A region selector will appear — pick one or choose **Any region**.\n\u200b"
        ),
        color=0x7C3AED,
    )
    embed.add_field(
        name="30d Accounts",
        value=f"**{counts.get('+30 days old', 0):,}** in stock",
        inline=True,
    )
    embed.add_field(
        name="1 Year Accounts",
        value=f"**{counts.get('+1 year old', 0):,}** in stock",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(
        name="5 Year Accounts",
        value=f"**{counts.get('5+ years old', 0):,}** in stock\nPremium roles only",
        inline=True,
    )
    embed.add_field(
        name="Dump Accounts",
        value=f"**{counts.get('dump', 0):,}** in stock\nPremium roles only",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.set_footer(text="DeltaCore Alt Generator  \u2022  Save your account — it is deleted after delivery")
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
        if not self.refresh_stock.is_running():
            self.refresh_stock.start()

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

    @tasks.loop(minutes=30)
    async def refresh_stock(self):
        await self.update_stock_channel()

    @refresh_stock.before_loop
    async def before_refresh(self):
        await self.wait_until_ready()


client = CredBot()


# ── Slash commands ─────────────────────────────────────────────────────────
@client.tree.command(name="panel", description="Send the account generator panel")
@app_commands.checks.has_permissions(manage_messages=True)
async def cmd_panel(interaction: discord.Interaction):
    counts = count_by_type()
    await interaction.channel.send(embed=build_panel_embed(counts), view=PanelView())
    await interaction.response.send_message("Panel sent.", ephemeral=True)

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
client.run(TOKEN)
