"""Merchant attribution master-data routes: batches, relation versions,
resolution explanation and attribution revisions."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import AttributionBatchIn
from ..security import current_user
from ..services import attribution as attribution_service

router = APIRouter(prefix="/api/attribution", tags=["attribution"])


@router.get("/batches")
def list_batches(
    limit: int = 100, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return attribution_service.list_batches(db, limit)


@router.post("/batches", status_code=201)
def create_batch(
    body: AttributionBatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        return attribution_service.ingest_batch(
            db,
            batch_key=body.batch_key,
            relations=[r.model_dump() for r in body.relations],
            reason=body.reason,
            source=body.source,
            note=body.note,
            visible_at=body.visible_at,
            created_by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    batch = attribution_service.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    return attribution_service.batch_out(batch)


@router.get("/relations")
def list_relations(
    terminal_id: str | None = None,
    merchant_id: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return attribution_service.list_relations(
        db, terminal_id=terminal_id, merchant_id=merchant_id, limit=limit
    )


@router.get("/revisions")
def list_revisions(
    limit: int = 100, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return attribution_service.list_attribution_revisions(db, limit)


@router.get("/resolve")
def resolve(
    terminal_id: str,
    event_time: datetime,
    as_of: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Explain how one terminal resolves at one event time, for a given
    scoring instant: chosen relation, source batch, effective interval,
    confidence and every candidate not used."""
    if as_of is None:
        return attribution_service.explain(
            db, terminal_id=terminal_id, event_time=event_time
        )
    visible_seq = attribution_service.visible_seq_at(db, as_of)
    result = attribution_service.resolve_one(db, terminal_id, event_time, visible_seq)
    return {
        "terminal_id": terminal_id,
        "event_time": event_time.isoformat(),
        "scoring_instant": as_of.isoformat(),
        "visible_batch_seq": visible_seq,
        "resolution": result,
    }
