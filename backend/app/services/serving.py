"""Online feature serving.

Every request resolves the feature-set version ONCE (the atomic online
pointer, or an explicitly requested version) and then reads all features from
that single complete version — a scoring input is never assembled from fields
of two different releases, even while a publish is in flight.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Feature,
    FeatureSetMember,
    FeatureSetVersion,
    OnlinePointer,
    SourceStatus,
    Transaction,
)
from .compute import SourceUnavailable, compute_feature_value


def resolve_set_version(
    db: Session, set_name: str, set_version: int | None
) -> FeatureSetVersion:
    """Pin the complete version for this request. Read exactly once."""
    if set_version is not None:
        row = db.execute(
            select(FeatureSetVersion).where(
                FeatureSetVersion.name == set_name,
                FeatureSetVersion.version == set_version,
            )
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(f"feature set {set_name} v{set_version} not found")
        return row
    pointer = db.get(OnlinePointer, set_name)
    if pointer is None:
        raise KeyError(f"feature set {set_name} has no published version")
    return db.get(FeatureSetVersion, pointer.set_version_id)


def _set_members(db: Session, set_version_id: int) -> list[Feature]:
    return list(
        db.execute(
            select(Feature)
            .join(FeatureSetMember, FeatureSetMember.feature_id == Feature.id)
            .where(FeatureSetMember.set_version_id == set_version_id)
            .order_by(FeatureSetMember.position)
        )
        .scalars()
        .all()
    )


def _source_snapshot(db: Session, source: str) -> dict:
    row = db.get(SourceStatus, source)
    if row is None:
        return {"source": source, "status": "unknown", "last_success_at": None}
    return {
        "source": source,
        "status": row.status,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "message": row.message,
    }


def read_online_features(
    db: Session,
    set_name: str,
    entity_id: str,
    as_of: datetime | None,
    set_version: int | None = None,
) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    version_row = resolve_set_version(db, set_name, set_version)
    features = _set_members(db, version_row.id)

    out_features = []
    sources_touched: dict[str, dict] = {}
    for feature in features:
        src_name = (
            "account_profiles" if feature.source == "account_profile" else feature.source
        )
        if src_name not in sources_touched:
            sources_touched[src_name] = _source_snapshot(db, src_name)
        item = {
            "name": feature.name,
            "version": feature.version,
            "as_of": as_of.isoformat(),
            "status": "ok",
            "value": None,
        }
        try:
            item["value"] = compute_feature_value(db, feature, entity_id, as_of)
        except SourceUnavailable as exc:
            # Missing upstream data is reported, never disguised as zero.
            item["status"] = "unavailable"
            item["value"] = None
            item["reason"] = exc.message
            item["source"] = {
                "source": exc.source,
                "status": exc.status,
                "last_success_at": exc.last_success_at.isoformat()
                if exc.last_success_at
                else None,
            }
        out_features.append(item)

    from . import attribution as attribution_service

    return {
        "feature_set": {"name": version_row.name, "version": version_row.version},
        "entity_id": entity_id,
        "as_of": as_of.isoformat(),
        "features": out_features,
        "sources": list(sources_touched.values()),
        "visible_batch_seq": attribution_service.visible_seq_at(db, as_of),
        "served_at": datetime.now(timezone.utc).isoformat(),
    }


def read_merchant_features(
    db: Session,
    set_name: str,
    merchant_id: str,
    as_of: datetime | None,
    set_version: int | None = None,
) -> dict:
    """Merchant-level online read backed by expanded attribution snapshots.

    Each feature is pinned to the same complete set version (resolved once).
    The attribution view is the one visible at `as_of`, and every unresolved
    transaction is reported with its explicit reason and candidate list.
    """
    from . import merchant_features

    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    version_row = resolve_set_version(db, set_name, set_version)
    features = _set_members(db, version_row.id)
    # Pin ONE attribution visibility for the whole response: every feature
    # resolves terminals against the same system-time view.
    from . import attribution as attribution_service

    visible_seq = attribution_service.visible_seq_at(db, as_of)

    out_features = []
    cache_stats = {"hit": 0, "miss": 0}
    for feature in features:
        item = {
            "name": feature.name,
            "version": feature.version,
            "as_of": as_of.isoformat(),
            "status": "ok",
            "value": None,
        }
        if feature.source != "merchant_transactions":
            try:
                item["value"] = compute_feature_value(db, feature, merchant_id, as_of)
            except SourceUnavailable as exc:
                item["status"] = "unavailable"
                item["reason"] = exc.message
            out_features.append(item)
            continue

        read = merchant_features.read_online_merchant_feature(
            db, feature, merchant_id, as_of, visible_seq=visible_seq
        )
        cache_stats[read["cache"]] += 1
        summary = read["summary"]
        item.update(
            {
                "value": read["value"],
                "snapshot_id": read["snapshot_id"],
                "cache": read["cache"],
                "visible_batch_seq": read["visible_batch_seq"],
                "attribution_summary": summary,
                "needs_review": summary["needs_review_transactions"],
                "needs_review_reasons": summary["needs_review_reasons"],
                "included_attributions": summary["included_attributions"],
                "source_batches": summary["source_batches"],
            }
        )
        if summary["needs_review_transactions"]:
            # Low-confidence / overlapping / missing attribution is a review
            # reason, never a silently chosen relation.
            item["status"] = "needs_review"
            blocked = [
                e for e in merchant_features.snapshot_entries(db, read["snapshot_id"])
                if e.status == "needs_review"
            ]
            item["review_items"] = [
                {
                    "txn_id": e.txn_id,
                    "terminal_id": e.terminal_id,
                    "event_time": e.event_time.isoformat(),
                    "reason": e.reason,
                    "reason_label": (e.detail or {}).get("reason_label"),
                    "candidates": (e.detail or {}).get("candidates", []),
                }
                for e in blocked[:50]
            ]
        out_features.append(item)

    return {
        "feature_set": {"name": version_row.name, "version": version_row.version},
        "entity_type": "merchant",
        "entity_id": merchant_id,
        "as_of": as_of.isoformat(),
        "features": out_features,
        "snapshot_cache": cache_stats,
        "visible_batch_seq": visible_seq,
        "read_path": "expanded_snapshot",
        "served_at": datetime.now(timezone.utc).isoformat(),
    }


def merchant_attribution_context(
    db: Session, merchant_id: str, as_of: datetime, set_name: str = "merchant_risk"
) -> dict:
    """Attribution references for every transaction inside the merchant's
    scoring window, resolved at the scoring instant."""
    from . import merchant_features

    as_of_aware = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    version_row = resolve_set_version(db, set_name, None)
    features = [f for f in _set_members(db, version_row.id)
                if f.source == "merchant_transactions"]
    if not features:
        return {"visible_batch_seq": 0, "transactions": [], "total_transactions": 0}
    max_window = max((f.window_seconds or 0) for f in features)
    start = as_of_aware - timedelta(seconds=max_window)
    txns = db.execute(
        select(Transaction)
        .where(Transaction.event_time > start, Transaction.event_time <= as_of_aware)
        .order_by(Transaction.event_time, Transaction.txn_id)
    ).scalars().all()
    from . import attribution as attribution_service

    visible_seq = attribution_service.visible_seq_at(db, as_of_aware)
    terminals = sorted({t.terminal_id for t in txns if t.terminal_id})
    rel_map = attribution_service.load_relations_for_terminals(db, terminals, visible_seq)
    out = []
    for txn in txns:
        if txn.terminal_id:
            res = attribution_service.resolve_terminal(
                rel_map.get(txn.terminal_id, []), txn.event_time
            )
        else:
            res = {
                "status": "needs_review",
                "reason": "no_terminal",
                "reason_label": attribution_service.REASON_LABELS["no_terminal"],
                "candidates": [],
            }
        out.append(
            {
                "txn_id": txn.txn_id,
                "terminal_id": txn.terminal_id,
                "event_time": txn.event_time.isoformat(),
                "attribution": {
                    "status": res.get("status"),
                    "reason": res.get("reason"),
                    "reason_label": res.get("reason_label"),
                    "merchant_id": res.get("merchant_id"),
                    "store_id": res.get("store_id"),
                    "attribution_id": res.get("attribution_id"),
                    "effective_from": res.get("effective_from"),
                    "effective_to": res.get("effective_to"),
                    "confidence": res.get("confidence"),
                    "batch_key": res.get("batch_key"),
                    "batch_seq": res.get("batch_seq"),
                    "candidates": res.get("candidates", []),
                },
            }
        )
    return {
        "visible_batch_seq": visible_seq,
        "transactions": out[:200],
        "truncated": len(out) > 200,
        "total_transactions": len(out),
    }
