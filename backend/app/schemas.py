from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field


# --- Auth ---
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    created_at: datetime

    class Config:
        from_attributes = True


# --- Accounts ---
class AccountBase(BaseModel):
    username:     str           = Field(min_length=1, max_length=150)
    account_type: str           = Field(default="+30 days old", max_length=100)
    cookie:       Optional[str] = Field(default="", max_length=2000)
    region:       Optional[str] = Field(default="", max_length=10)


class AccountCreate(AccountBase):
    password: str = Field(min_length=1, max_length=500)


class AccountUpdate(BaseModel):
    username:     Optional[str] = Field(default=None, min_length=1, max_length=150)
    password:     Optional[str] = Field(default=None, min_length=1, max_length=500)
    account_type: Optional[str] = Field(default=None, max_length=100)
    cookie:       Optional[str] = Field(default=None, max_length=2000)
    region:       Optional[str] = Field(default=None, max_length=10)


class AccountOut(AccountBase):
    id:         int
    password:   str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BulkImportItem(BaseModel):
    username:     str           = Field(min_length=1, max_length=150)
    password:     str           = Field(min_length=1, max_length=500)
    account_type: Optional[str] = Field(default="+30 days old", max_length=100)
    cookie:       Optional[str] = Field(default="", max_length=2000)
    region:       Optional[str] = Field(default="", max_length=10)


class BulkImportRequest(BaseModel):
    accounts: List[BulkImportItem]


class BulkImportResult(BaseModel):
    created: int
    failed:  int
    errors:  List[str] = []
