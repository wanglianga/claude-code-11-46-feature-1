"""Scoring evidence routes: immutable evidence listing, digest check and
attribution context for one anomalous score."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import current_user
from ..services import evidence as evidence_service

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


@router.get("")
def list_evidence(
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return evidence_service.list_evidence(
        db, entity_type=entity_type, entity_id=entity_id, limit=limit
    )


@router.get("/{evidence_id}")
def evidence_detail(
    evidence_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    detail = evidence_service.evidence_detail(db, evidence_id)
    if detail is None:
        raise HTTPException(404, "evidence not found")
    return detail
