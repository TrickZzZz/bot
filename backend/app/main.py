from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from .database import Base, engine
from . import routers_auth, routers_accounts


def ensure_schema_migrations():
    """One-time, idempotent schema patches for columns added after the
    accounts table was first created. Base.metadata.create_all() only
    creates tables that don't exist yet — it does NOT alter an existing
    table to add a new column, so this fills that gap safely.

    Safe to run on every single startup: ADD COLUMN IF NOT EXISTS is a
    no-op once the column already exists, and the backfill only touches
    rows that are still NULL, never overwriting anything already set."""
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS account_type VARCHAR(50)"
        ))
        conn.execute(text(
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS cookie VARCHAR(1000)"
        ))
        conn.execute(text(
            "UPDATE accounts SET account_type = '+30 days old' WHERE account_type IS NULL"
        ))
        conn.commit()


Base.metadata.create_all(bind=engine)
ensure_schema_migrations()

app = FastAPI(title="Account Manager API", version="1.0.0")


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
