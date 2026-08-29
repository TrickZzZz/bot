from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from .database import Base, engine
from . import routers_auth, routers_accounts

Base.metadata.create_all(bind=engine)

# Run each migration in its own transaction so a failed one
# (column already exists) doesn't block the rest — critical for PostgreSQL
_MIGRATIONS = [
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS account_type VARCHAR(100) NOT NULL DEFAULT '+30 days old'",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS cookie TEXT DEFAULT ''",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS region VARCHAR(10) DEFAULT ''",
    # Widen cookie column — Roblox cookies exceed VARCHAR(1000)
    "ALTER TABLE accounts ALTER COLUMN cookie TYPE TEXT",
    # Widen password column for Fernet-encrypted values
    "ALTER TABLE accounts ALTER COLUMN password TYPE VARCHAR(1000)",
]
for _sql in _MIGRATIONS:
    try:
        with engine.begin() as _conn:
            _conn.execute(text(_sql))
    except Exception:
        pass  # Column already exists or DB doesn't support IF NOT EXISTS

app = FastAPI(title="DeltaCore Account Manager", version="2.0.0")

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routers_auth.router)
app.include_router(routers_accounts.router)


@app.get("/health")
def health():
    return {"status": "ok"}
