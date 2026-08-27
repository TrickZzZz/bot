from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

from .database import Base, engine
from . import routers_auth, routers_accounts

Base.metadata.create_all(bind=engine)

# Migrate existing databases — add new columns if they don't exist yet
_MIGRATIONS = [
    "ALTER TABLE accounts ADD COLUMN account_type VARCHAR(100) NOT NULL DEFAULT '+30 days old'",
    "ALTER TABLE accounts ADD COLUMN cookie TEXT DEFAULT ''",
    "ALTER TABLE accounts ADD COLUMN region VARCHAR(10) DEFAULT ''",
]
with engine.connect() as _conn:
    for _sql in _MIGRATIONS:
        try:
            _conn.execute(text(_sql))
            _conn.commit()
        except Exception:
            pass  # Column already exists

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
