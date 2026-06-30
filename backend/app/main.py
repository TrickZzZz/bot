from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv

load_dotenv()

from .database import Base, engine
from . import routers_auth, routers_accounts


Base.metadata.create_all(bind=engine)

app = FastAPI(title="Account Manager API", version="1.0.0")


ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173")


app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGINS],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routers_auth.router)
app.include_router(routers_accounts.router)


@app.get("/health")
def health():
    return {"status": "ok"}
