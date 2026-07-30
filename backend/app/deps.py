import os

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .database import get_db
from .security import decode_access_token
from . import models

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    username = decode_access_token(token)
    if username is None:
        raise credentials_exception
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise credentials_exception
    return user


# ── Admin access control ────────────────────────────────────────────────────
# Comma-separated list of usernames allowed to see vault/account data across
# the whole site. If unset, everyone is treated as admin — keeps a fresh
# single-user setup working with zero config. Set this env var the moment
# you add a second (non-admin) account.
_ADMIN_USERNAMES = {
    u.strip().lower() for u in os.environ.get("ADMIN_USERNAMES", "").split(",") if u.strip()
}


def is_admin_username(username: str) -> bool:
    """Same check as is_admin(), for callers that only have a raw username
    string (e.g. a manually-decoded JWT payload) rather than a full User row."""
    if not _ADMIN_USERNAMES:
        return True
    return str(username or "").strip().lower() in _ADMIN_USERNAMES


def is_admin(user: models.User) -> bool:
    return is_admin_username(getattr(user, "username", ""))


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
