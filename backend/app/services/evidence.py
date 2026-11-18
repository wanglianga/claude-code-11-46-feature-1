"""Scoring evidence (immutable) and training-sample attribution archives.

Responsibility split
--------------------
* `attribution` owns relation versions, batches and their impact scope;
* `merchant_features` owns window values, expanded snapshots and window
  revisions;
* this module owns **scoring evidence** and **training-sample attribution
  archives** — both are append-only. They reference attribution versions by
  id and window revisions by id, but nothing here is ever rewritten.

That is what makes "a late master correction cannot rewrite what risk control
already saw" verifiable: archived payload + digest remain byte-identical, while
new revisions and new evidence are appended alongside.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Feature,
    SampleAttributionArchive,
    ScoreEvidence,
    Transaction,
)
from . import attribution as attribution_service

MAX_CONTEXT_TXNS = 200


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def canonical_digest(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def _attribution_ref(res: dict) -> dict:
    return {
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
    }


def build_transaction_attribution_context(
    db: Session, txn_ids: list[str], as_of: datetime
) -> dict:
    """Resolve the terminals of the transactions contributing to a score, using
    exactly the relation versions visible at `as_of` (the scoring instant)."""
    visible_seq = attribution_service.visible_seq_at(db, as_of)
    txns = list(
        db.execute(select(Transaction).where(Transaction.txn_id.in_(set(txn_ids)))).scalars()
    ) if txn_ids else []
    terminals = sorted({t.terminal_id for t in txns if t.terminal_id})
    rel_map = attribution_service.load_relations_for_terminals(db, terminals, visible_seq)
    out = []
    for txn in sorted(txns, key=lambda t: (_aware(t.event_time), t.txn_id)):
        if txn.terminal_id:
            res = attribution_service.resolve_terminal(
                rel_map.get(txn.terminal_id, []), txn.event_time
            )
        else:
            res = {
                "status": attribution_service.NEEDS_REVIEW,
                "reason": "no_terminal",
                "reason_label": attribution_service.REASON_LABELS["no_terminal"],
                "candidates": [],
            }
        out.append(
            {
                "txn_id": txn.txn_id,
                "account_id": txn.account_id,
                "terminal_id": txn.terminal_id,
                "event_time": _aware(txn.event_time).isoformat(),
                "attribution": _attribution_ref(res),
            }
        )
    truncated = len(out) > MAX_CONTEXT_TXNS
    return {
        "visible_batch_seq": visible_seq,
        "transactions": out[:MAX_CONTEXT_TXNS],
        "truncated": truncated,
        "total_transactions": len(out),
    }


def record_score(
    db: Session,
    *,
    entity_type: str,
    entity_id: str,
    feature_set_name: str,
    feature_set_version: int,
    as_of: datetime,
    response: dict,
    attribution_context: dict | None = None,
    created_by: str = "system",
) -> ScoreEvidence:
    """Append immutable evidence for one online scoring call."""
    as_of = _aware(as_of)
    if attribution_context is None:
        attribution_context = build_transaction_attribution_context(db, [], as_of)
    payload = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "feature_set": {"name": feature_set_name, "version": feature_set_version},
        "as_of": as_of.isoformat(),
        "features": response.get("features", []),
        "sources": response.get("sources", []),
        "attribution_context": attribution_context,
    }
    digest = canonical_digest(payload)
    evidence = ScoreEvidence(
        evidence_key=f"EV-{digest[:24]}",
        entity_type=entity_type,
        entity_id=entity_id,
        feature_set_name=feature_set_name,
        feature_set_version=feature_set_version,
        as_of=as_of,
        payload=payload,
        digest=digest,
        visible_batch_seq=attribution_context.get("visible_batch_seq", 0),
        created_by=created_by,
    )
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return evidence


def evidence_out(evidence: ScoreEvidence) -> dict:
    return {
        "id": evidence.id,
        "evidence_key": evidence.evidence_key,
        "entity_type": evidence.entity_type,
        "entity_id": evidence.entity_id,
        "feature_set": {
            "name": evidence.feature_set_name,
            "version": evidence.feature_set_version,
        },
        "as_of": evidence.as_of.isoformat(),
        "digest": evidence.digest,
        "visible_batch_seq": evidence.visible_batch_seq,
        "created_by": evidence.created_by,
        "created_at": evidence.created_at.isoformat() if evidence.created_at else None,
    }


def list_evidence(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    q = select(ScoreEvidence)
    if entity_type:
        q = q.where(ScoreEvidence.entity_type == entity_type)
    if entity_id:
        q = q.where(ScoreEvidence.entity_id == entity_id)
    rows = db.execute(q.order_by(ScoreEvidence.id.desc()).limit(min(limit, 200))).scalars()
    return [evidence_out(e) for e in rows]


def evidence_detail(db: Session, evidence_id: int) -> dict | None:
    evidence = db.get(ScoreEvidence, evidence_id)
    if evidence is None:
        return None
    payload = evidence.payload
    recomputed = canonical_digest(payload)
    out = evidence_out(evidence)
    out["payload"] = payload
    out["integrity"] = {
        "stored_digest": evidence.digest,
        "recomputed_digest": recomputed,
        "immutable": recomputed == evidence.digest,
        "note": "评分证据一经写入不可改写；后续主档修订会生成新的证据与窗口修订。",
    }
    return out


# ---------------------------------------------------------------------------
# Training-sample attribution archive
# ---------------------------------------------------------------------------
def merchant_features_of(db: Session, set_name: str = "merchant_risk") -> list[Feature]:
    from .merchant_features import MERCHANT_SOURCE
    from .serving import _set_members, resolve_set_version

    try:
        version = resolve_set_version(db, set_name, None)
    except KeyError:
        return []
    return [f for f in _set_members(db, version.id) if f.source == MERCHANT_SOURCE]


def archive_sample_attribution(
    db: Session,
    *,
    sample,
    feature_set_name: str = "merchant_risk",
    feature_set_version: int | None = None,
) -> SampleAttributionArchive | None:
    """Archive the merchant attribution view visible at the sample's decision
    time. Idempotent per sample; an existing archive is never modified."""
    existing = db.execute(
        select(SampleAttributionArchive).where(SampleAttributionArchive.sample_id == sample.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    from .serving import resolve_set_version

    try:
        version = resolve_set_version(
            db, feature_set_name, feature_set_version
        )
    except KeyError:
        return None

    decision_time = _aware(sample.decision_time)
    visible_seq = attribution_service.visible_seq_at(db, decision_time)
    txn_ids: list[str] = []
    if sample.transaction_id:
        txn_ids = [sample.transaction_id]
    context = build_transaction_attribution_context(db, txn_ids, decision_time)

    attribution_refs: dict[str, dict] = {}
    for item in context["transactions"]:
        attribution_refs[item["txn_id"]] = {
            "terminal_id": item["terminal_id"],
            "event_time": item["event_time"],
            **item["attribution"],
        }

    merchant_id = None
    for ref in attribution_refs.values():
        if ref.get("status") == attribution_service.RESOLVED and ref.get("merchant_id"):
            merchant_id = ref["merchant_id"]
            break

    values: dict[str, dict] = {}
    if merchant_id:
        from . import merchant_features

        for feature in merchant_features_of(db, feature_set_name):
            result = merchant_features.compute_merchant_window(
                db, feature, merchant_id, decision_time, visible_seq=visible_seq
            )
            values[feature.name] = {
                "value": result["value"],
                "visible_batch_seq": result["visible_batch_seq"],
                "included_attributions": result["summary"]["included_attributions"],
                "source_batches": result["summary"]["source_batches"],
                "needs_review_reasons": result["summary"]["needs_review_reasons"],
            }

    archive = SampleAttributionArchive(
        sample_id=sample.id,
        feature_set_name=version.name,
        feature_set_version=version.version,
        merchant_id=merchant_id,
        as_of=decision_time,
        visible_batch_seq=visible_seq,
        values=values,
        attribution_refs=attribution_refs,
    )
    db.add(archive)
    db.commit()
    db.refresh(archive)
    return archive


def sample_archive_out(archive: SampleAttributionArchive | None) -> dict | None:
    if archive is None:
        return None
    return {
        "archive_id": archive.id,
        "sample_id": archive.sample_id,
        "feature_set": {
            "name": archive.feature_set_name,
            "version": archive.feature_set_version,
        },
        "merchant_id": archive.merchant_id,
        "as_of": archive.as_of.isoformat(),
        "visible_batch_seq": archive.visible_batch_seq,
        "values": archive.values,
        "attribution_refs": archive.attribution_refs,
        "archived_at": archive.archived_at.isoformat() if archive.archived_at else None,
    }


def verify_sample_archive(db: Session, sample, archive: SampleAttributionArchive) -> dict:
    """Recompute the decision-time view and compare it with the archive.

    If a master batch was *backdated* (visible_at <= decision_time), the live
    recomputation legitimately differs. The archive is still the training
    truth; we surface the divergence instead of silently rewriting it.
    """
    from . import merchant_features

    if archive.merchant_id is None:
        return {"stable": True, "reason": "sample has no resolved merchant", "differences": []}

    current_values = {}
    for feature in merchant_features_of(db, archive.feature_set_name):
        result = merchant_features.compute_merchant_window(
            db, feature, archive.merchant_id, _aware(sample.decision_time)
        )
        current_values[feature.name] = result["value"]

    archived_values = {k: v.get("value") for k, v in (archive.values or {}).items()}
    differences = [
        {
            "feature": name,
            "archived": archived_values.get(name),
            "recomputed": current_values.get(name),
        }
        for name in sorted(set(archived_values) | set(current_values))
        if archived_values.get(name) != current_values.get(name)
    ]
    return {
        "stable": not differences,
        "archived_visible_batch_seq": archive.visible_batch_seq,
        "current_visible_batch_seq": attribution_service.visible_seq_at(
            db, _aware(sample.decision_time)
        ),
        "differences": differences,
        "note": "归档样本保持训练时的归属视图不变；差异只说明此后出现了可见时点更早的主档批次。",
    }
