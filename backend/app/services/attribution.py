"""Merchant attribution master data: bi-temporal relations, batches and the
precise impact scope of a correction.

Two time axes
-------------
* business time: a relation is effective on ``[effective_from, effective_to)``;
* system time: a batch with a monotonic ``seq`` becomes visible at
  ``visible_at``. A scoring read at instant ``T`` may only use batches whose
  ``visible_at <= T``.

Resolution rules (never "pick one")
-----------------------------------
For a terminal at a transaction's event time, among relation versions visible
to the scoring instant:

* no effective relation            -> ``needs_review / no_relation``
* two or more effective relations  -> ``needs_review / overlapping_intervals``
  (covers "one transaction hits two valid stores" and "overlapping intervals")
* exactly one but confidence=low   -> ``needs_review / low_confidence``
* exactly one high/medium          -> resolved

A ``supersede`` relation truncates overlapping earlier versions by writing a
*new* version row (old interval preserved for history) in the same batch.
``coexist`` leaves earlier versions untouched, so genuine overlap surfaces as
``needs_review`` rather than being silently overwritten.

Master data is never mutated in place: corrections add versions + attribution
revisions, and only then recompute exactly the materialized merchant windows
that contain an affected transaction inside a changed interval.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    AttributionBatch,
    AttributionRevision,
    MerchantAttribution,
    Transaction,
)

RESOLVED = "resolved"
NEEDS_REVIEW = "needs_review"

REASON_LABELS = {
    "no_relation": "终端在评分时点可见的主档中无有效归属关系",
    "overlapping_intervals": "同一交易同时命中多条有效关系（跨门店/跨商户或区间重叠），待核对",
    "low_confidence": "仅存在低置信映射，未纳入计算，待核对",
    "no_terminal": "交易缺少终端号，无法关联商户层级",
}

RELATION_MODES = {"supersede", "coexist"}
CONFIDENCES = {"high", "medium", "low"}


def _aware(dt: datetime | str | None) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return _aware(dt).isoformat() if dt is not None else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def visible_seq_at(db: Session, instant: datetime | None) -> int:
    """Highest batch seq visible at a scoring instant (system time)."""
    instant = _aware(instant) or now_utc()
    return int(
        db.execute(
            select(func.coalesce(func.max(AttributionBatch.seq), 0)).where(
                AttributionBatch.visible_at <= instant
            )
        ).scalar_one()
    )


def touched_seq_at(
    db: Session, merchant_id: str, instant: datetime | None
) -> int:
    """Highest visible batch seq that touches any terminal related to this
    merchant. This is the only thing that can change a merchant window's
    attribution, so snapshots key their cache validity on it."""
    instant = _aware(instant) or now_utc()
    return int(
        db.execute(
            select(func.coalesce(func.max(AttributionBatch.seq), 0))
            .select_from(AttributionBatch)
            .join(AttributionRevision, AttributionRevision.batch_id == AttributionBatch.id)
            .where(
                AttributionBatch.visible_at <= instant,
                AttributionRevision.terminal_id.in_(
                    select(MerchantAttribution.terminal_id).where(
                        MerchantAttribution.merchant_id == merchant_id
                    )
                ),
            )
        ).scalar_one()
    )


def latest_seq(db: Session) -> int:
    return int(
        db.execute(select(func.coalesce(func.max(AttributionBatch.seq), 0))).scalar_one()
    )


def get_batch(db: Session, batch_id: int) -> AttributionBatch | None:
    return db.get(AttributionBatch, batch_id)


def _relation_view(rel: MerchantAttribution, batch: AttributionBatch, event_time: datetime) -> dict:
    start = _aware(rel.effective_from)
    end = _aware(rel.effective_to)
    contains = start <= event_time and (end is None or event_time < end)
    return {
        "attribution_id": rel.id,
        "terminal_id": rel.terminal_id,
        "store_id": rel.store_id,
        "merchant_id": rel.merchant_id,
        "effective_from": _iso(start),
        "effective_to": _iso(end),
        "confidence": rel.confidence,
        "batch_key": batch.batch_key,
        "batch_seq": batch.seq,
        "source": batch.source,
        "note": rel.note,
        "interval_contains_event_time": contains,
    }


def load_relations_for_terminals(
    db: Session, terminal_ids: list[str], visible_seq: int
) -> dict[str, list[dict]]:
    """Load all relation versions visible to a scoring instant, grouped by
    terminal. Rows carry raw datetimes for interval math plus display fields."""
    out: dict[str, list[dict]] = {}
    if not terminal_ids:
        return out
    rows = db.execute(
        select(MerchantAttribution, AttributionBatch)
        .join(AttributionBatch, AttributionBatch.id == MerchantAttribution.batch_id)
        .where(
            MerchantAttribution.terminal_id.in_(set(terminal_ids)),
            AttributionBatch.seq <= visible_seq,
            # System-time retirement: a version superseded by a LATER batch is
            # still visible to scoring instants that cannot see that batch.
            or_(
                MerchantAttribution.retired_at_seq.is_(None),
                MerchantAttribution.retired_at_seq > visible_seq,
            ),
        )
        .order_by(
            MerchantAttribution.terminal_id,
            MerchantAttribution.effective_from,
            AttributionBatch.seq,
            MerchantAttribution.id,
        )
    ).all()
    for rel, batch in rows:
        out.setdefault(rel.terminal_id, []).append(
            {
                "id": rel.id,
                "terminal_id": rel.terminal_id,
                "store_id": rel.store_id,
                "merchant_id": rel.merchant_id,
                "from_dt": _aware(rel.effective_from),
                "to_dt": _aware(rel.effective_to),
                "confidence": rel.confidence,
                "batch_id": batch.id,
                "batch_key": batch.batch_key,
                "batch_seq": batch.seq,
                "source": batch.source,
            }
        )
    return out


def resolve_terminal(rels: list[dict], event_time: datetime) -> dict:
    """Pure resolution of one transaction's terminal at its event time.

    ``rels`` are the versions already filtered to the scoring instant's
    visibility. Returns a dict with status/reason/chosen attribution and the
    full candidate list (including valid-but-not-chosen relations).
    """
    event_time = _aware(event_time)
    valid = [r for r in rels if r["from_dt"] <= event_time and (r["to_dt"] is None or event_time < r["to_dt"])]

    # Collapse only *exact* duplicate rows (same store, merchant and interval)
    # produced by re-applying an identical relation. Any genuine overlap —
    # same or different store — stays visible and therefore forces review.
    dedup: dict[tuple[str, str, datetime, datetime | None], dict] = {}
    for r in valid:
        key = (r["store_id"], r["merchant_id"], r["from_dt"], r["to_dt"])
        cur = dedup.get(key)
        if cur is None or (r["batch_seq"], r["id"]) > (cur["batch_seq"], cur["id"]):
            dedup[key] = r

    candidates = [
        {
            "attribution_id": r["id"],
            "store_id": r["store_id"],
            "merchant_id": r["merchant_id"],
            "effective_from": _iso(r["from_dt"]),
            "effective_to": _iso(r["to_dt"]),
            "confidence": r["confidence"],
            "batch_key": r["batch_key"],
            "batch_seq": r["batch_seq"],
            "source": r["source"],
        }
        for r in sorted(valid, key=lambda x: (x["merchant_id"], x["store_id"], x["batch_seq"], x["id"]))
    ]
    chosen_list = sorted(dedup.values(), key=lambda x: (x["merchant_id"], x["store_id"]))

    base = {
        "terminal_id": rels[0]["terminal_id"] if rels else None,
        "event_time": _iso(event_time),
        "candidates": candidates,
    }

    if not dedup:
        return {
            **base,
            "status": NEEDS_REVIEW,
            "reason": "no_relation",
            "reason_label": REASON_LABELS["no_relation"],
            "store_id": None,
            "merchant_id": None,
            "attribution_id": None,
        }

    if len(dedup) > 1:
        stores = sorted({r["store_id"] for r in chosen_list})
        merchants = sorted({r["merchant_id"] for r in chosen_list})
        conflict = "multiple_stores" if len(stores) > 1 else "multiple_merchants"
        return {
            **base,
            "status": NEEDS_REVIEW,
            "reason": "overlapping_intervals",
            "reason_label": REASON_LABELS["overlapping_intervals"],
            "conflict_type": conflict,
            "conflicting": [
                {
                    "attribution_id": r["id"],
                    "store_id": r["store_id"],
                    "merchant_id": r["merchant_id"],
                    "effective_from": _iso(r["from_dt"]),
                    "effective_to": _iso(r["to_dt"]),
                    "confidence": r["confidence"],
                    "batch_key": r["batch_key"],
                }
                for r in chosen_list
            ],
            "store_id": None,
            "merchant_id": None,
            "attribution_id": None,
        }

    r = chosen_list[0]
    resolved = {
        "attribution_id": r["id"],
        "store_id": r["store_id"],
        "merchant_id": r["merchant_id"],
        "effective_from": _iso(r["from_dt"]),
        "effective_to": _iso(r["to_dt"]),
        "confidence": r["confidence"],
        "batch_key": r["batch_key"],
        "batch_seq": r["batch_seq"],
    }
    if r["confidence"] == "low":
        return {
            **base,
            "status": NEEDS_REVIEW,
            "reason": "low_confidence",
            "reason_label": REASON_LABELS["low_confidence"],
            **resolved,
        }
    return {**base, "status": RESOLVED, "reason": None, "reason_label": None, **resolved}


def resolve_one(
    db: Session, terminal_id: str, event_time: datetime, visible_seq: int
) -> dict:
    """Convenience resolution used by explanations and the sample archive."""
    if not terminal_id:
        return {
            "terminal_id": None,
            "event_time": _iso(event_time),
            "status": NEEDS_REVIEW,
            "reason": "no_terminal",
            "reason_label": REASON_LABELS["no_terminal"],
            "candidates": [],
            "store_id": None,
            "merchant_id": None,
            "attribution_id": None,
        }
    rels = load_relations_for_terminals(db, [terminal_id], visible_seq).get(terminal_id, [])
    return resolve_terminal(rels, event_time)


# ---------------------------------------------------------------------------
# Batch ingestion
# ---------------------------------------------------------------------------
def _insert_version(
    db: Session,
    batch: AttributionBatch,
    *,
    terminal_id: str,
    store_id: str,
    merchant_id: str,
    effective_from: datetime,
    effective_to: datetime | None,
    confidence: str,
    note: str = "",
    supersedes_id: int | None = None,
) -> MerchantAttribution:
    row = MerchantAttribution(
        batch_id=batch.id,
        terminal_id=terminal_id,
        store_id=store_id,
        merchant_id=merchant_id,
        effective_from=effective_from,
        effective_to=effective_to,
        confidence=confidence,
        status="active",
        supersedes_id=supersedes_id,
        note=note,
    )
    db.add(row)
    db.flush()
    return row


def _overlaps(ex_from: datetime, ex_to: datetime | None, frm: datetime, to: datetime | None) -> bool:
    ex_to_cmp = ex_to or datetime.max.replace(tzinfo=timezone.utc)
    to_cmp = to or datetime.max.replace(tzinfo=timezone.utc)
    return ex_from < to_cmp and frm < ex_to_cmp


def _supersede_overlaps(
    db: Session,
    batch: AttributionBatch,
    terminal_id: str,
    frm: datetime,
    to: datetime | None,
    confidence: str,
) -> list[int]:
    """Truncate earlier active versions overlapping [frm, to) by writing new
    version rows. Returns the ids of the impacted original versions."""
    existing = list(
        db.execute(
            select(MerchantAttribution)
            .where(
                MerchantAttribution.terminal_id == terminal_id,
                MerchantAttribution.retired_at_seq.is_(None),
            )
            .order_by(MerchantAttribution.effective_from, MerchantAttribution.id)
        )
        .scalars()
        .all()
    )
    impacted = []
    for ex in existing:
        ex_from = _aware(ex.effective_from)
        ex_to = _aware(ex.effective_to)
        if not _overlaps(ex_from, ex_to, frm, to):
            continue
        impacted.append(ex.id)
        # Bookkeeping status; temporal visibility is governed solely by
        # retired_at_seq so history stays readable to earlier scoring instants.
        ex.status = "superseded"
        ex.retired_at_seq = batch.seq
        created_ids = []
        if ex_from < frm:
            # keep the prefix [ex_from, frm)
            prefix = _insert_version(
                db,
                batch,
                terminal_id=terminal_id,
                store_id=ex.store_id,
                merchant_id=ex.merchant_id,
                effective_from=ex_from,
                effective_to=frm,
                confidence=ex.confidence,
                note=ex.note,
                supersedes_id=ex.id,
            )
            created_ids.append(prefix.id)
        # keep the tail [to or +inf, ex_to), only if it extends past the new
        # relation's end and the new relation is bounded
        if to is not None and ex_to is not None and ex_to > to:
            tail = _insert_version(
                db,
                batch,
                terminal_id=terminal_id,
                store_id=ex.store_id,
                merchant_id=ex.merchant_id,
                effective_from=to,
                effective_to=ex_to,
                confidence=ex.confidence,
                note=ex.note,
                supersedes_id=ex.id,
            )
            created_ids.append(tail.id)
        db.add(
            AttributionRevision(
                batch_id=batch.id,
                terminal_id=terminal_id,
                kind="truncated" if created_ids else "retracted",
                old_attribution_id=ex.id,
                new_attribution_id=created_ids[0] if created_ids else None,
                detail={
                    "old": {
                        "store_id": ex.store_id,
                        "merchant_id": ex.merchant_id,
                        "effective_from": _iso(ex_from),
                        "effective_to": _iso(ex_to),
                    },
                    "new_interval": [_iso(frm), _iso(to)],
                    "replacement_ids": created_ids,
                },
            )
        )
    db.flush()
    return impacted


def ingest_batch(
    db: Session,
    *,
    batch_key: str,
    relations: list[dict],
    reason: str = "batch_correction",
    source: str = "merchant_master",
    note: str = "",
    visible_at: datetime | None = None,
    created_by: str = "system",
) -> dict:
    """Ingest one master-data batch and compute its exact impact.

    Relation rows: terminal_id, store_id, merchant_id, effective_from,
    effective_to (optional), confidence, mode ('supersede'|'coexist'), note.
    """
    if not relations:
        raise ValueError("a batch must contain at least one relation")
    if db.execute(
        select(AttributionBatch.id).where(AttributionBatch.batch_key == batch_key)
    ).scalar_one_or_none() is not None:
        raise ValueError(f"batch_key {batch_key} already exists")

    visible_at = _aware(visible_at) or now_utc()
    seq = latest_seq(db) + 1
    batch = AttributionBatch(
        batch_key=batch_key,
        seq=seq,
        reason=reason,
        source=source,
        note=note,
        visible_at=visible_at,
        created_by=created_by,
    )
    db.add(batch)
    db.flush()

    changed_terminals: set[str] = set()
    intervals: list[tuple[str, datetime, datetime | None]] = []
    original_version_ids: list[int] = []
    created_revision_ids: dict[str, int] = {}

    for spec in relations:
        terminal_id = spec["terminal_id"]
        store_id = spec["store_id"]
        merchant_id = spec["merchant_id"]
        frm = _aware(spec["effective_from"])
        to = _aware(spec.get("effective_to"))
        confidence = spec.get("confidence", "high")
        mode = spec.get("mode", "supersede")
        if mode not in RELATION_MODES:
            raise ValueError(f"relation mode must be one of {sorted(RELATION_MODES)}")
        if confidence not in CONFIDENCES:
            raise ValueError(f"confidence must be one of {sorted(CONFIDENCES)}")
        if to is not None and to <= frm:
            raise ValueError(f"terminal {terminal_id}: effective_to must be after effective_from")

        if mode == "supersede":
            original_version_ids.extend(
                _supersede_overlaps(db, batch, terminal_id, frm, to, confidence)
            )
        row = _insert_version(
            db,
            batch,
            terminal_id=terminal_id,
            store_id=store_id,
            merchant_id=merchant_id,
            effective_from=frm,
            effective_to=to,
            confidence=confidence,
            note=spec.get("note", ""),
        )
        rev = AttributionRevision(
            batch_id=batch.id,
            terminal_id=terminal_id,
            kind="created",
            new_attribution_id=row.id,
            detail={
                "store_id": store_id,
                "merchant_id": merchant_id,
                "effective_from": _iso(frm),
                "effective_to": _iso(to),
                "confidence": confidence,
                "mode": mode,
            },
        )
        db.add(rev)
        db.flush()
        created_revision_ids.setdefault(terminal_id, rev.id)
        changed_terminals.add(terminal_id)
        intervals.append((terminal_id, frm, to))

    db.flush()

    # Impact is computed against the state immediately before this batch.
    impact = compute_impact(
        db,
        batch,
        sorted(changed_terminals),
        intervals,
        seq_before=seq - 1,
        attribution_revision_ids=created_revision_ids,
    )
    batch.impact_json = impact
    db.commit()
    return {
        "id": batch.id,
        "batch_key": batch.batch_key,
        "seq": batch.seq,
        "reason": batch.reason,
        "source": batch.source,
        "visible_at": _iso(batch.visible_at),
        "note": batch.note,
        "impact": impact,
    }


def _resolve_all(
    db: Session, terminals: list[str], visible_seq: int, txns: list[Transaction]
) -> dict[str, dict]:
    rel_map = load_relations_for_terminals(db, terminals, visible_seq)
    return {
        txn.txn_id: resolve_terminal(rel_map.get(txn.terminal_id, []), txn.event_time)
        for txn in txns
    }


def _signature(res: dict) -> tuple:
    return (res.get("status"), res.get("merchant_id"), res.get("store_id"), res.get("attribution_id"))


def compute_impact(
    db: Session,
    batch: AttributionBatch,
    terminals: list[str],
    intervals: list[tuple[str, datetime, datetime | None]],
    *,
    seq_before: int,
    attribution_revision_ids: dict[str, int] | None = None,
) -> dict:
    """Exact impact scope of a batch:
    * transactions whose resolved attribution changed (before vs after batch);
    * materialized merchant-window snapshots that contain an affected
      transaction inside a changed interval and are visible at scoring instants
      ``as_of >= batch.visible_at``; those windows are recomputed here.
    """
    from . import merchant_features  # local import avoids a cycle

    if not terminals:
        return {
            "batch_id": batch.id,
            "changed_terminals": [],
            "affected_transactions": [],
            "affected_transaction_count": 0,
            "affected_snapshots": 0,
            "revisions": 0,
        }

    # --- affected transactions (business-time interval filter) -------------
    candidates = list(
        db.execute(
            select(Transaction).where(Transaction.terminal_id.in_(terminals))
        )
        .scalars()
        .all()
    )
    by_terminal: dict[str, list[Transaction]] = {}
    for txn in candidates:
        if txn.terminal_id:
            by_terminal.setdefault(txn.terminal_id, []).append(txn)

    affected_txns: list[dict] = []
    affected_ids: set[str] = set()
    for terminal_id in terminals:
        txns = by_terminal.get(terminal_id, [])
        if not txns:
            continue
        rels_before = load_relations_for_terminals(db, [terminal_id], seq_before)
        rels_after = load_relations_for_terminals(db, [terminal_id], batch.seq)
        before_map = rels_before.get(terminal_id, [])
        after_map = rels_after.get(terminal_id, [])
        for txn in txns:
            if not _in_any_interval(txn.terminal_id, _aware(txn.event_time), intervals):
                continue
            old = resolve_terminal(before_map, txn.event_time)
            new = resolve_terminal(after_map, txn.event_time)
            if _signature(old) == _signature(new):
                continue
            affected_ids.add(txn.txn_id)
            affected_txns.append(
                {
                    "txn_id": txn.txn_id,
                    "terminal_id": txn.terminal_id,
                    "account_id": txn.account_id,
                    "event_time": _iso(txn.event_time),
                    "old": {
                        "status": old.get("status"),
                        "merchant_id": old.get("merchant_id"),
                        "store_id": old.get("store_id"),
                        "attribution_id": old.get("attribution_id"),
                        "reason": old.get("reason"),
                    },
                    "new": {
                        "status": new.get("status"),
                        "merchant_id": new.get("merchant_id"),
                        "store_id": new.get("store_id"),
                        "attribution_id": new.get("attribution_id"),
                        "reason": new.get("reason"),
                    },
                }
            )

    # --- affected materialized snapshots -----------------------------------
    revision_ids = attribution_revision_ids or {}
    snapshot_revision_count = 0
    touched_snapshots = 0
    window_revisions: list[dict] = []
    from ..models import MerchantSnapshotEntry, MerchantWindowSnapshot

    if affected_ids:
        snap_ids = list(
            db.execute(
                select(MerchantSnapshotEntry.snapshot_id)
                .join(
                    MerchantWindowSnapshot,
                    MerchantWindowSnapshot.id == MerchantSnapshotEntry.snapshot_id,
                )
                .where(
                    MerchantSnapshotEntry.txn_id.in_(affected_ids),
                    MerchantWindowSnapshot.as_of >= _aware(batch.visible_at),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        for snap_id in snap_ids:
            snapshot = db.get(MerchantWindowSnapshot, snap_id)
            if snapshot is None:
                continue
            touched_snapshots += 1
            old_value = dict(snapshot.value or {})
            merchant_features.refresh_snapshot(db, snapshot, force=True)
            if old_value != snapshot.value:
                snapshot_revision_count += 1
                terminal_for_rev = next(
                    (
                        t
                        for t in terminals
                        if any(e["txn_id"] in affected_ids and e["terminal_id"] == t for e in affected_txns)
                    ),
                    None,
                )
                db.add(
                    merchant_features.MerchantWindowRevisionRecord(
                        feature_id=snapshot.feature_id,
                        merchant_id=snapshot.merchant_id,
                        as_of=snapshot.as_of,
                        old_value=old_value,
                        new_value=dict(snapshot.value or {}),
                        attribution_revision_id=revision_ids.get(terminal_for_rev),
                        batch_id=batch.id,
                        affected_txns=sorted(affected_ids),
                    )
                )
            window_revisions.append(
                {
                    "snapshot_id": snapshot.id,
                    "merchant_id": snapshot.merchant_id,
                    "as_of": _iso(snapshot.as_of),
                    "old_value": old_value,
                    "new_value": dict(snapshot.value or {}),
                    "changed": old_value != snapshot.value,
                }
            )
        db.flush()

    return {
        "batch_id": batch.id,
        "batch_key": batch.batch_key,
        "visible_at": _iso(batch.visible_at),
        "changed_terminals": terminals,
        "affected_transaction_count": len(affected_txns),
        "affected_transactions": affected_txns[:200],
        "affected_snapshots": touched_snapshots,
        "revisions": snapshot_revision_count,
        "windows": window_revisions[:100],
    }


def _in_any_interval(
    terminal_id: str, event_time: datetime, intervals: list[tuple[str, datetime, datetime | None]]
) -> bool:
    for t, frm, to in intervals:
        if t != terminal_id:
            continue
        if frm <= event_time and (to is None or event_time < to):
            return True
    return False


# ---------------------------------------------------------------------------
# Reads for operators
# ---------------------------------------------------------------------------
def explain(
    db: Session,
    *,
    terminal_id: str,
    event_time: datetime,
    visible_seq: int | None = None,
) -> dict:
    """Full explainability for one terminal/event: chosen relation, source
    batch, effective interval, confidence and every candidate not used."""
    event_time = _aware(event_time)
    if visible_seq is None:
        visible_seq = latest_seq(db)
    rels = load_relations_for_terminals(db, [terminal_id], visible_seq)
    res = resolve_terminal(rels.get(terminal_id, []), event_time)
    rel_map = rels.get(terminal_id, [])
    return {
        "terminal_id": terminal_id,
        "event_time": _iso(event_time),
        "visible_batch_seq": visible_seq,
        "resolution": res,
        "all_visible_relations": [
            {
                **_relation_view_light(r),
                "interval_contains_event_time": (
                    r["from_dt"] <= event_time and (r["to_dt"] is None or event_time < r["to_dt"])
                ),
            }
            for r in rel_map
        ],
    }


def _relation_view_light(r: dict) -> dict:
    return {
        "attribution_id": r["id"],
        "store_id": r["store_id"],
        "merchant_id": r["merchant_id"],
        "effective_from": _iso(r["from_dt"]),
        "effective_to": _iso(r["to_dt"]),
        "confidence": r["confidence"],
        "batch_key": r["batch_key"],
        "batch_seq": r["batch_seq"],
        "source": r["source"],
    }


def list_batches(db: Session, limit: int = 100) -> list[dict]:
    rows = (
        db.execute(select(AttributionBatch).order_by(AttributionBatch.seq.desc()).limit(min(limit, 500)))
        .scalars()
        .all()
    )
    return [batch_out(b) for b in rows]


def batch_out(batch: AttributionBatch) -> dict:
    return {
        "id": batch.id,
        "batch_key": batch.batch_key,
        "seq": batch.seq,
        "reason": batch.reason,
        "source": batch.source,
        "note": batch.note,
        "visible_at": _iso(batch.visible_at),
        "created_by": batch.created_by,
        "created_at": _iso(batch.created_at),
        "impact": batch.impact_json,
    }


def list_relations(
    db: Session,
    *,
    terminal_id: str | None = None,
    merchant_id: str | None = None,
    limit: int = 200,
) -> list[dict]:
    q = select(MerchantAttribution, AttributionBatch).join(
        AttributionBatch, AttributionBatch.id == MerchantAttribution.batch_id
    )
    if terminal_id:
        q = q.where(MerchantAttribution.terminal_id == terminal_id)
    if merchant_id:
        q = q.where(MerchantAttribution.merchant_id == merchant_id)
    q = q.order_by(
        MerchantAttribution.terminal_id, MerchantAttribution.effective_from, AttributionBatch.seq
    ).limit(min(limit, 1000))
    rows = db.execute(q).all()
    return [
        {
            "attribution_id": rel.id,
            "terminal_id": rel.terminal_id,
            "store_id": rel.store_id,
            "merchant_id": rel.merchant_id,
            "effective_from": _iso(rel.effective_from),
            "effective_to": _iso(rel.effective_to),
            "confidence": rel.confidence,
            "status": rel.status,
            "retired_at_batch_seq": rel.retired_at_seq,
            "supersedes_id": rel.supersedes_id,
            "batch_key": batch.batch_key,
            "batch_seq": batch.seq,
            "batch_visible_at": _iso(batch.visible_at),
            "source": batch.source,
            "note": rel.note,
        }
        for rel, batch in rows
    ]


def list_attribution_revisions(db: Session, limit: int = 100) -> list[dict]:
    rows = (
        db.execute(
            select(AttributionRevision, AttributionBatch)
            .join(AttributionBatch, AttributionBatch.id == AttributionRevision.batch_id)
            .order_by(AttributionRevision.id.desc())
            .limit(min(limit, 500))
        )
        .all()
    )
    return [
        {
            "id": rev.id,
            "batch_id": batch.id,
            "batch_key": batch.batch_key,
            "batch_seq": batch.seq,
            "terminal_id": rev.terminal_id,
            "kind": rev.kind,
            "old_attribution_id": rev.old_attribution_id,
            "new_attribution_id": rev.new_attribution_id,
            "detail": rev.detail,
            "created_at": _iso(rev.created_at),
        }
        for rev, batch in rows
    ]
