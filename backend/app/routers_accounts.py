from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .database import get_db
from . import models, schemas
from .security import encrypt_secret, decrypt_secret
from .deps import require_admin

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _to_out(acc: models.Account) -> schemas.AccountOut:
    try:
        pw = decrypt_secret(acc.password)
    except Exception:
        # This row's password isn't valid Fernet ciphertext — almost certainly
        # stored as plaintext during the earlier broken deploy that skipped
        # encryption. Surface it clearly instead of crashing the whole list;
        # re-saving this account's password through the (now-fixed) update
        # endpoint will re-encrypt it correctly and clear this up.
        pw = "⚠ corrupted — re-save this account's password"
    return schemas.AccountOut(
        id=acc.id,
        username=acc.username,
        password=pw,
        account_type=acc.account_type,
        created_at=acc.created_at,
        updated_at=acc.updated_at,
    )


@router.get("", response_model=List[schemas.AccountOut])
def list_accounts(
    search: str = "",
    account_type: str = "",
    _=Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(models.Account)
    if search:
        like = f"%{search}%"
        query = query.filter(models.Account.username.ilike(like))
    if account_type:
        query = query.filter(models.Account.account_type == account_type)
    accounts = query.order_by(models.Account.id.asc()).all()
    return [_to_out(a) for a in accounts]


@router.get("/{account_id}", response_model=schemas.AccountOut)
def get_account(
    account_id: int,
    _=Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = (
        db.query(models.Account)
        .filter(models.Account.id == account_id)
        .first()
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return _to_out(acc)


@router.post("", response_model=schemas.AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: schemas.AccountCreate,
    _=Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = models.Account(
        username=payload.username,
        password=encrypt_secret(payload.password),
        account_type=payload.account_type,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return _to_out(acc)


@router.put("/{account_id}", response_model=schemas.AccountOut)
def update_account(
    account_id: int,
    payload: schemas.AccountUpdate,
    _=Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = (
        db.query(models.Account)
        .filter(models.Account.id == account_id)
        .first()
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    data = payload.model_dump(exclude_unset=True)
    if "password" in data:
        acc.password = encrypt_secret(data.pop("password"))
    for field, value in data.items():
        setattr(acc, field, value)

    db.commit()
    db.refresh(acc)
    return _to_out(acc)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: int,
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = (
        db.query(models.Account)
        .filter(models.Account.id == account_id)
        .first()
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    db.delete(acc)
    db.commit()
    return None


@router.post("/bulk-import", response_model=schemas.BulkImportResult)
def bulk_import(
    payload: schemas.BulkImportRequest,
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    created = 0
    errors: List[str] = []

    for idx, item in enumerate(payload.accounts):
        try:
            acc = models.Account(
                username=item.username,
                password=encrypt_secret(item.password),
                account_type=item.account_type,
            )
            db.add(acc)
            db.flush()
            created += 1
        except Exception as e:
            errors.append(f"Row {idx + 1} ({item.username}): {str(e)}")

    db.commit()
    return schemas.BulkImportResult(created=created, failed=len(errors), errors=errors)


@router.post("/migrate-encryption")
def migrate_encryption(
    current_user: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """One-time fix for rows whose password was stored as plaintext instead
    of Fernet ciphertext (e.g. from a bulk-import path that predated proper
    encryption). For every account: if the stored value already decrypts
    successfully, it's left untouched — already correct, no risk of double
    encryption. If decryption fails, the raw stored value is treated as
    legacy plaintext and re-encrypted in place. Safe to run more than once;
    already-encrypted rows are always skipped."""
    accounts = db.query(models.Account).all()
    already_ok = 0
    migrated = 0
    failed: List[str] = []

    for acc in accounts:
        try:
            decrypt_secret(acc.password)
            already_ok += 1
            continue
        except Exception:
            pass
        try:
            acc.password = encrypt_secret(acc.password)
            migrated += 1
        except Exception as e:
            failed.append(f"{acc.username} (id={acc.id}): {e}")

    db.commit()
    return {
        "total": len(accounts),
        "already_encrypted": already_ok,
        "migrated": migrated,
        "failed": len(failed),
        "failed_details": failed[:50],  # cap the list so a huge failure count doesn't blow up the response
    }
