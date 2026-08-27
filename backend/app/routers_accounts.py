from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .database import get_db
from . import models, schemas
from .deps import get_current_user

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _to_out(acc: models.Account) -> schemas.AccountOut:
    return schemas.AccountOut(
        id=acc.id,
        username=acc.username,
        password=acc.password,
        account_type=acc.account_type or "+30 days old",
        cookie=acc.cookie or "",
        region=acc.region or "",
        created_at=acc.created_at,
        updated_at=acc.updated_at,
    )


@router.get("", response_model=List[schemas.AccountOut])
def list_accounts(
    search:       str           = "",
    account_type: Optional[str] = None,
    db:           Session       = Depends(get_db),
):
    query = db.query(models.Account)
    if search:
        like = f"%{search}%"
        query = query.filter(models.Account.username.ilike(like))
    if account_type:
        query = query.filter(models.Account.account_type == account_type)
    return [_to_out(a) for a in query.order_by(models.Account.id.asc()).all()]


@router.get("/types", response_model=List[str])
def list_types(db: Session = Depends(get_db)):
    """Return distinct account types present in the database."""
    rows = db.query(models.Account.account_type).distinct().all()
    return sorted({r[0] for r in rows if r[0]})


@router.get("/{account_id}", response_model=schemas.AccountOut)
def get_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(models.Account).filter(models.Account.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return _to_out(acc)


@router.post("", response_model=schemas.AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(payload: schemas.AccountCreate, db: Session = Depends(get_db)):
    # Idempotent — return existing account if username already present
    existing = db.query(models.Account).filter(models.Account.username == payload.username).first()
    if existing:
        # Update fields in case the caller is refreshing a cookie or changing type
        existing.password     = payload.password
        existing.account_type = payload.account_type or existing.account_type
        existing.cookie       = payload.cookie       if payload.cookie is not None else existing.cookie
        existing.region       = payload.region       if payload.region is not None else existing.region
        db.commit()
        db.refresh(existing)
        return _to_out(existing)

    acc = models.Account(
        username=payload.username,
        password=payload.password,
        account_type=payload.account_type or "+30 days old",
        cookie=payload.cookie or "",
        region=payload.region or "",
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return _to_out(acc)


@router.put("/{account_id}", response_model=schemas.AccountOut)
def update_account(account_id: int, payload: schemas.AccountUpdate, db: Session = Depends(get_db)):
    acc = db.query(models.Account).filter(models.Account.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(acc, field, value)
    db.commit()
    db.refresh(acc)
    return _to_out(acc)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id:   int,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    acc = db.query(models.Account).filter(models.Account.id == account_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    db.delete(acc)
    db.commit()


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def wipe_all_accounts(
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    """Delete every account. Requires auth."""
    db.query(models.Account).delete()
    db.commit()


@router.post("/bulk-import", response_model=schemas.BulkImportResult)
def bulk_import(
    payload:      schemas.BulkImportRequest,
    current_user: models.User = Depends(get_current_user),
    db:           Session     = Depends(get_db),
):
    created = 0
    errors: List[str] = []

    for idx, item in enumerate(payload.accounts):
        try:
            existing = db.query(models.Account).filter(models.Account.username == item.username).first()
            if existing:
                existing.password     = item.password
                existing.account_type = item.account_type or existing.account_type
                existing.cookie       = item.cookie or existing.cookie
                existing.region       = item.region or existing.region
                db.flush()
            else:
                acc = models.Account(
                    username=item.username,
                    password=item.password,
                    account_type=item.account_type or "+30 days old",
                    cookie=item.cookie or "",
                    region=item.region or "",
                )
                db.add(acc)
                db.flush()
            created += 1
        except Exception as e:
            errors.append(f"Row {idx + 1} ({item.username}): {str(e)}")

    db.commit()
    return schemas.BulkImportResult(created=created, failed=len(errors), errors=errors)
