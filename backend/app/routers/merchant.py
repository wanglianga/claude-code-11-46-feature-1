"""Merchant-level feature routes: attribution-aware online reads, the
explanation of one scoring result, and the expanded snapshot view."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Feature, MerchantWindowSnapshot, Transaction, User
from ..security import current_user
from ..services import attribution as attribution_service
from ..services import evidence as evidence_service
from ..services import merchant_features, serving

router = APIRouter(prefix="/api", tags=["merchant"])


@router.get("/online/merchant-features")
def merchant_online_features(
    entity_id: str,
    feature_set: str = "merchant_risk",
    set_version: int | None = None,
    as_of: datetime | None = None,
    record_evidence: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    try:
        resp = serving.read_merchant_features(db, feature_set, entity_id, as_of, set_version)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    if record_evidence:
        as_of_dt = datetime.fromisoformat(resp["as_of"])
        context = serving.merchant_attribution_context(db, entity_id, as_of_dt, feature_set)
        evidence = evidence_service.record_score(
            db,
            entity_type="merchant",
            entity_id=entity_id,
            feature_set_name=resp["feature_set"]["name"],
            feature_set_version=resp["feature_set"]["version"],
            as_of=as_of_dt,
            response=resp,
            attribution_context={**context, "merchant_id": entity_id},
            created_by=user.username,
        )
        resp["evidence"] = {
            "id": evidence.id,
            "evidence_key": evidence.evidence_key,
            "digest": evidence.digest,
        }
    return resp


@router.get("/merchant/snapshots/{snapshot_id}")
def snapshot_detail(
    snapshot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    snapshot = db.get(MerchantWindowSnapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(404, "snapshot not found")
    feature = db.get(Feature, snapshot.feature_id)
    entries = merchant_features.snapshot_entries(db, snapshot_id)
    return {
        "snapshot_id": snapshot.id,
        "feature": {"name": feature.name, "version": feature.version} if feature else None,
        "merchant_id": snapshot.merchant_id,
        "as_of": snapshot.as_of.isoformat(),
        "window_start": snapshot.window_start.isoformat(),
        "value": snapshot.value,
        "summary": snapshot.summary,
        "visible_batch_seq": snapshot.visible_batch_seq,
        "revision_seq": snapshot.revision_seq,
        "entries": [
            {
                "txn_id": e.txn_id,
                "terminal_id": e.terminal_id,
                "event_time": e.event_time.isoformat(),
                "merchant_id": e.merchant_id,
                "store_id": e.store_id,
                "attribution_id": e.attribution_id,
                "status": e.status,
                "reason": e.reason,
                "included": e.included,
                "detail": e.detail,
            }
            for e in entries
        ],
    }


@router.get("/merchant/window-revisions")
def window_revisions(
    merchant_id: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return merchant_features.merchant_window_revisions(
        db, merchant_id=merchant_id, limit=limit
    )


@router.get("/merchant/transactions/{txn_id}/attribution")
def transaction_attribution(
    txn_id: str,
    at: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(404, "transaction not found")
    instant = at or datetime.now().astimezone()
    explain = attribution_service.explain(
        db, terminal_id=txn.terminal_id, event_time=txn.event_time
    )
    as_of_resolution = attribution_service.resolve_one(
        db, txn.terminal_id, txn.event_time, attribution_service.visible_seq_at(db, instant)
    )
    return {
        "txn_id": txn.txn_id,
        "account_id": txn.account_id,
        "terminal_id": txn.terminal_id,
        "event_time": txn.event_time.isoformat(),
        "event_time_resolution": explain,
        "resolution_when_scored_at": as_of_resolution,
    }


@router.get("/merchant/feature-set")
def merchant_feature_set(
    db: Session = Depends(get_db), user: User = Depends(current_user)
):
    found = db.execute(select(Feature).where(Feature.source == "merchant_transactions")).scalars().all()
    return [
        {
            "id": f.id,
            "name": f.name,
            "version": f.version,
            "aggregation": f.aggregation,
            "field": f.field,
            "window_seconds": f.window_seconds,
            "entity_key": f.entity_key,
            "description": f.description,
            "status": f.status,
        }
        for f in found
    ]
