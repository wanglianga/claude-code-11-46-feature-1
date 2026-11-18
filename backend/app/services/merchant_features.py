"""Merchant-level feature engine over bi-temporal attribution.

Chosen trade-off
----------------
For merchant features we **materialize expanded window snapshots**: each
(feature, merchant, as_of) that has been scored stores its value plus one row
per transaction in the window, carrying the attribution version that was
*visible at that scoring instant*. Repeat online reads therefore hit an
indexed snapshot instead of rescanning transactions and joining relations on
every peak request.

The price is paid on master revisions: a correction batch must locate exactly
the snapshots whose expanded entries touch a changed terminal inside a changed
effective interval and recompute those snapshots (see
`attribution.compute_impact`). Non-materialized windows simply compute on
demand with the visibility of their own `as_of`, so historical reads stay
correct either way.

Two layers of correctness
-------------------------
* system time: transactions are filtered to the window, then each is resolved
  with relation versions visible at the *scoring* instant (`as_of`), never with
  today's master view;
* business time: the relation's effective interval must contain the
  transaction's event time.

Transactions that cannot be attributed unambiguously are excluded from the
value and reported with an explicit reason and their candidate list — the
engine never picks one relation for the operator.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    Feature,
    MerchantAttribution,
    MerchantSnapshotEntry,
    MerchantWindowRevision,
    MerchantWindowSnapshot,
    Transaction,
)
from . import attribution as attribution_service

# Alias kept for readable imports in the attribution impact code.
MerchantWindowRevisionRecord = MerchantWindowRevision

MERCHANT_SOURCE = "merchant_transactions"
MERCHANT_DIMS = {"terminal_id", "store_id", "merchant_id", "account_id", "txn_id"}
MERCHANT_AGGS = {"COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX"}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _window_bounds(as_of: datetime, window_seconds: int) -> tuple[datetime, datetime]:
    return as_of - timedelta(seconds=window_seconds), as_of


def _empty_value(aggregation: str):
    return 0 if aggregation in ("COUNT", "COUNT_DISTINCT", "SUM") else None


def _aggregate(entries: list[dict], feature: Feature):
    included = [e for e in entries if e["included"]]
    if feature.aggregation == "COUNT":
        return len(included)
    if feature.aggregation == "COUNT_DISTINCT":
        return len({e.get(feature.field or "terminal_id") for e in included})
    values = [float(e["amount"]) for e in included if e.get("amount") is not None]
    if not values:
        return 0 if feature.aggregation == "SUM" else None
    if feature.aggregation == "SUM":
        return sum(values)
    if feature.aggregation == "AVG":
        return sum(values) / len(values)
    if feature.aggregation == "MIN":
        return min(values)
    if feature.aggregation == "MAX":
        return max(values)
    raise ValueError(f"unsupported merchant aggregation {feature.aggregation}")


def _merchant_terminals(db: Session, merchant_id: str) -> list[str]:
    """Every terminal that has any relation version — current, retired or from
    a not-yet-visible batch — pointing at this merchant.

    A merchant window only needs to look at these terminals: a transaction on
    a terminal with no relation to this merchant can never contribute to this
    merchant's feature, and reporting it as `needs_review` on every merchant
    read would be noise. Terminals that *moved away* stay in the set, so their
    historical transactions remain discoverable and attributable to history.
    """
    return list(
        db.execute(
            select(MerchantAttribution.terminal_id)
            .where(MerchantAttribution.merchant_id == merchant_id)
            .distinct()
        )
        .scalars()
        .all()
    )


def _window_transactions(
    db: Session, merchant_id: str, as_of: datetime, window_seconds: int
) -> list[Transaction]:
    """Transactions in the window on terminals related to this merchant.

    The window is event-time, so the entries of a migrated terminal remain
    discoverable after a correction. Terminal-less transactions cannot resolve
    to any merchant level and are outside a merchant feature's population."""
    terminals = _merchant_terminals(db, merchant_id)
    if not terminals:
        return []
    start, end = _window_bounds(as_of, window_seconds)
    return list(
        db.execute(
            select(Transaction)
            .where(
                Transaction.event_time > start,
                Transaction.event_time <= end,
                Transaction.terminal_id.in_(terminals),
            )
            .order_by(Transaction.event_time, Transaction.txn_id)
        )
        .scalars()
        .all()
    )


def _window_data_version(
    db: Session, merchant_id: str, as_of: datetime, window_seconds: int
) -> int:
    terminals = _merchant_terminals(db, merchant_id)
    if not terminals:
        return 0
    start, end = _window_bounds(as_of, window_seconds)
    return int(
        db.execute(
            select(func.count(Transaction.txn_id)).where(
                Transaction.event_time > start,
                Transaction.event_time <= end,
                Transaction.terminal_id.in_(terminals),
            )
        ).scalar_one()
    )


def _entry_from_resolution(txn: Transaction, res: dict, merchant_id: str) -> dict:
    included = res["status"] == attribution_service.RESOLVED and res.get("merchant_id") == merchant_id
    return {
        "txn_id": txn.txn_id,
        "terminal_id": txn.terminal_id,
        "event_time": _aware(txn.event_time).isoformat(),
        "amount": float(txn.amount) if txn.amount is not None else None,
        "account_id": txn.account_id,
        "merchant_id": res.get("merchant_id"),
        "store_id": res.get("store_id"),
        "attribution_id": res.get("attribution_id"),
        "status": res["status"],
        "reason": res.get("reason"),
        "reason_label": res.get("reason_label"),
        "included": included,
        "candidates": res.get("candidates", []),
    }


def compute_merchant_window(
    db: Session,
    feature: Feature,
    merchant_id: str,
    as_of: datetime,
    *,
    visible_seq: int | None = None,
) -> dict:
    """Point-in-time merchant feature computation. This is the single function
    behind both online snapshot materialization and training replay."""
    as_of = _aware(as_of)
    window_seconds = feature.window_seconds or 0
    if visible_seq is None:
        visible_seq = attribution_service.visible_seq_at(db, as_of)

    txns = _window_transactions(db, merchant_id, as_of, window_seconds)
    terminals = sorted({t.terminal_id for t in txns if t.terminal_id})
    rel_map = attribution_service.load_relations_for_terminals(db, terminals, visible_seq)

    entries: list[dict] = []
    for txn in txns:
        if not txn.terminal_id:
            res = {
                "status": attribution_service.NEEDS_REVIEW,
                "reason": "no_terminal",
                "reason_label": attribution_service.REASON_LABELS["no_terminal"],
                "merchant_id": None,
                "store_id": None,
                "attribution_id": None,
                "candidates": [],
            }
        else:
            res = attribution_service.resolve_terminal(
                rel_map.get(txn.terminal_id, []), txn.event_time
            )
        entries.append(_entry_from_resolution(txn, res, merchant_id))

    value = _aggregate(entries, feature)
    included = [e for e in entries if e["included"]]
    excluded = [e for e in entries if not e["included"]]
    reasons: dict[str, int] = {}
    for e in excluded:
        key = e.get("reason") or "other_merchant"
        if e["status"] == attribution_service.NEEDS_REVIEW:
            reasons[key] = reasons.get(key, 0) + 1

    attrs_used = sorted({e["attribution_id"] for e in included if e["attribution_id"]})
    batches_used = sorted({c["batch_key"] for e in included for c in e["candidates"]
                           if c["attribution_id"] == e["attribution_id"]})
    out_of_scope = [e for e in excluded if e["status"] == attribution_service.RESOLVED]

    summary = {
        "merchant_id": merchant_id,
        "as_of": as_of.isoformat(),
        "window_seconds": window_seconds,
        "visible_batch_seq": visible_seq,
        "considered_transactions": len(entries),
        "included_transactions": len(included),
        "resolved_to_other_merchant": len(out_of_scope),
        "needs_review_transactions": len(excluded) - len(out_of_scope),
        "needs_review_reasons": reasons,
        "included_attributions": attrs_used,
        "source_batches": batches_used,
        "distinct_terminals": len({e["terminal_id"] for e in included if e["terminal_id"]}),
    }
    return {
        "value": value,
        "summary": summary,
        "entries": entries,
        "visible_batch_seq": visible_seq,
        "data_version": len(txns),
    }


def compute_merchant_value(db: Session, feature: Feature, merchant_id: str, as_of: datetime):
    return compute_merchant_window(db, feature, merchant_id, as_of)["value"]


# ---------------------------------------------------------------------------
# Snapshot materialization
# ---------------------------------------------------------------------------
def get_snapshot(
    db: Session, feature: Feature, merchant_id: str, as_of: datetime
) -> MerchantWindowSnapshot | None:
    return db.execute(
        select(MerchantWindowSnapshot).where(
            MerchantWindowSnapshot.feature_id == feature.id,
            MerchantWindowSnapshot.merchant_id == merchant_id,
            MerchantWindowSnapshot.as_of == _aware(as_of),
        )
    ).scalar_one_or_none()


def snapshot_is_valid(
    db: Session, snapshot: MerchantWindowSnapshot, feature: Feature, merchant_id: str
) -> bool:
    as_of = _aware(snapshot.as_of)
    if snapshot.attribution_marker != attribution_service.touched_seq_at(
        db, merchant_id, as_of
    ):
        return False
    if snapshot.data_version != _window_data_version(
        db, merchant_id, as_of, feature.window_seconds or 0
    ):
        return False
    return snapshot.value is not None


def _write_entries(db: Session, snapshot: MerchantWindowSnapshot, entries: list[dict]) -> None:
    db.execute(
        delete(MerchantSnapshotEntry).where(MerchantSnapshotEntry.snapshot_id == snapshot.id)
    )
    for e in entries:
        db.add(
            MerchantSnapshotEntry(
                snapshot_id=snapshot.id,
                txn_id=e["txn_id"],
                terminal_id=e["terminal_id"] or "",
                event_time=datetime.fromisoformat(e["event_time"]),
                merchant_id=e["merchant_id"],
                store_id=e["store_id"],
                attribution_id=e["attribution_id"],
                status=e["status"],
                reason=e.get("reason"),
                included=e["included"],
                detail=e,
            )
        )
    db.flush()


def upsert_snapshot(
    db: Session,
    feature: Feature,
    merchant_id: str,
    as_of: datetime,
    *,
    visible_seq: int | None = None,
) -> MerchantWindowSnapshot:
    as_of = _aware(as_of)
    window_start, _ = _window_bounds(as_of, feature.window_seconds or 0)
    result = compute_merchant_window(
        db, feature, merchant_id, as_of, visible_seq=visible_seq
    )
    snapshot = get_snapshot(db, feature, merchant_id, as_of)
    if snapshot is None:
        snapshot = MerchantWindowSnapshot(
            feature_id=feature.id,
            merchant_id=merchant_id,
            as_of=as_of,
            window_start=window_start,
            value={"value": result["value"]},
            summary=result["summary"],
            visible_batch_seq=result["visible_batch_seq"],
            attribution_marker=attribution_service.touched_seq_at(
                db, merchant_id, as_of
            ),
            data_version=result["data_version"],
        )
        # Isolate the insert in a SAVEPOINT: a concurrent request may have won
        # the race for the same (feature, merchant, as_of); in that case we
        # adopt the existing row instead of failing.
        try:
            with db.begin_nested():
                db.add(snapshot)
        except Exception:
            snapshot = get_snapshot(db, feature, merchant_id, as_of)
            if snapshot is None:
                raise
            return upsert_snapshot(
                db, feature, merchant_id, as_of, visible_seq=visible_seq
            )
        db.flush()
    else:
        snapshot.value = {"value": result["value"]}
        snapshot.summary = result["summary"]
        snapshot.visible_batch_seq = result["visible_batch_seq"]
        snapshot.attribution_marker = attribution_service.touched_seq_at(
            db, merchant_id, snapshot.as_of
        )
        snapshot.data_version = result["data_version"]
        snapshot.revision_seq += 1
    _write_entries(db, snapshot, result["entries"])
    db.flush()
    return snapshot


def refresh_snapshot(
    db: Session, snapshot: MerchantWindowSnapshot, *, force: bool = False
) -> MerchantWindowSnapshot:
    """Recompute one materialized window after a master-data correction."""
    feature = db.get(Feature, snapshot.feature_id)
    if feature is None:
        return snapshot
    as_of = _aware(snapshot.as_of)
    result = compute_merchant_window(db, feature, snapshot.merchant_id, as_of)
    snapshot.value = {"value": result["value"]}
    snapshot.summary = result["summary"]
    snapshot.visible_batch_seq = result["visible_batch_seq"]
    snapshot.attribution_marker = attribution_service.touched_seq_at(
        db, snapshot.merchant_id, as_of
    )
    snapshot.data_version = result["data_version"]
    if force:
        snapshot.revision_seq += 1
    _write_entries(db, snapshot, result["entries"])
    db.flush()
    return snapshot


def read_online_merchant_feature(
    db: Session,
    feature: Feature,
    merchant_id: str,
    as_of: datetime,
    *,
    visible_seq: int | None = None,
) -> dict:
    """Snapshot-backed read. Cache hit = index lookup; miss = compute once and
    materialize (paying the attribution-expansion cost exactly once).

    `visible_seq` pins the attribution version for the whole request, so all
    features in one scoring response resolve terminals against the same batch
    visibility — never a mix of two system-time views.
    """
    as_of = _aware(as_of)
    if visible_seq is None:
        visible_seq = attribution_service.visible_seq_at(db, as_of)
    snapshot = get_snapshot(db, feature, merchant_id, as_of)
    cache = "miss"
    if snapshot is not None and snapshot_is_valid(db, snapshot, feature, merchant_id):
        cache = "hit"
    else:
        snapshot = upsert_snapshot(
            db, feature, merchant_id, as_of, visible_seq=visible_seq
        )
        db.commit()
    return {
        "value": (snapshot.value or {}).get("value"),
        "cache": cache,
        "snapshot_id": snapshot.id,
        "summary": snapshot.summary,
        "visible_batch_seq": snapshot.visible_batch_seq,
    }


def snapshot_entries(db: Session, snapshot_id: int) -> list[MerchantSnapshotEntry]:
    return list(
        db.execute(
            select(MerchantSnapshotEntry)
            .where(MerchantSnapshotEntry.snapshot_id == snapshot_id)
            .order_by(MerchantSnapshotEntry.event_time, MerchantSnapshotEntry.txn_id)
        )
        .scalars()
        .all()
    )


def merchant_window_revisions(
    db: Session,
    *,
    merchant_id: str | None = None,
    feature_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    q = select(MerchantWindowRevision, Feature).join(
        Feature, Feature.id == MerchantWindowRevision.feature_id
    )
    if merchant_id:
        q = q.where(MerchantWindowRevision.merchant_id == merchant_id)
    if feature_id:
        q = q.where(MerchantWindowRevision.feature_id == feature_id)
    rows = db.execute(q.order_by(MerchantWindowRevision.id.desc()).limit(min(limit, 500))).all()
    return [
        {
            "id": rev.id,
            "feature": feature.name,
            "feature_version": feature.version,
            "merchant_id": rev.merchant_id,
            "as_of": rev.as_of.isoformat(),
            "old_value": rev.old_value,
            "new_value": rev.new_value,
            "batch_id": rev.batch_id,
            "attribution_revision_id": rev.attribution_revision_id,
            "affected_txns": rev.affected_txns,
            "created_at": rev.created_at.isoformat() if rev.created_at else None,
        }
        for rev, feature in rows
    ]
