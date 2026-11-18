"""SQLAlchemy models for the anti-fraud feature platform.

Design notes
------------
* Canonical input records (Transaction / LabelEvent / AccountProfile) are stored
  once, deduplicated by their business id.
* The streaming compute engine maintains, per feature version and entity, a
  window-bounded contribution state (`WindowEntry`) plus materialized window
  results at event-time boundaries (`Checkpoint`).
* Late-arriving events only recompute the checkpoints of the affected
  (feature, entity, as_of range); every change is audited in `Revision`.
* Online serving pins one complete `FeatureSetVersion` per request via the
  atomically-updated `OnlinePointer`, so a scoring input never mixes fields
  from two releases.

Merchant attribution (bi-temporal)
----------------------------------
Acquirer / merchant-master corrections re-assign a terminal (终端) to a
different store (门店) and therefore to a different merchant (商户). Fraud
analysis must never retroactively rewrite what risk control saw at scoring
time, so attribution is modelled on two independent time axes:

* **business time** — the effective interval ``[effective_from, effective_to)``
  of a terminal→store→merchant relation (`MerchantAttribution`);
* **system time** — which master-data *batch* first made the relation visible
  (`AttributionBatch`, monotonic sequence). A scoring read at ``as_of`` uses
  link versions whose batch was already visible *at that instant*.

Resolving an attribution therefore takes both ``(effective interval contains
transaction time)`` and ``(visible at scoring time)``. When two relations are
simultaneously valid, or only a low-confidence relation exists, the resolver
returns an explicit ``needs_review`` reason instead of picking one.
"""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="analyst")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Feature catalog
# ---------------------------------------------------------------------------
class Feature(Base):
    """One version of a feature definition (immutable once published)."""

    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_feature_name_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(64))
    entity_key: Mapped[str] = mapped_column(String(64), default="account_id")
    # transactions | labels | account_profile
    source: Mapped[str] = mapped_column(String(32))
    # COUNT | SUM | AVG | MIN | MAX | LAST
    aggregation: Mapped[str] = mapped_column(String(16))
    # numeric field for SUM/AVG/MIN/MAX (transactions); attribute name for LAST (account_profile)
    field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # window length in seconds; NULL for LAST (profile snapshot) features
    window_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # optional equality filter on transaction attributes, e.g. {"type": "purchase"}
    filter_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    value_type: Mapped[str] = mapped_column(String(16), default="number")  # number | string
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | published | deprecated
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeatureSetVersion(Base):
    """An immutable release of a named feature set (a complete scoring input)."""

    __tablename__ = "feature_set_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_set_name_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="published")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeatureSetMember(Base):
    __tablename__ = "feature_set_members"
    __table_args__ = (UniqueConstraint("set_version_id", "feature_id", name="uq_set_member"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    set_version_id: Mapped[int] = mapped_column(ForeignKey("feature_set_versions.id"), index=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)


class OnlinePointer(Base):
    """Current published version per feature-set name. Single atomic row update
    on publish -> a request either sees the old complete version or the new one."""

    __tablename__ = "online_pointers"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    set_version_id: Mapped[int] = mapped_column(ForeignKey("feature_set_versions.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Source data
# ---------------------------------------------------------------------------
class SourceStatus(Base):
    __tablename__ = "source_status"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)  # transactions | account_profiles | labels
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | degraded | unavailable
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProcessedEvent(Base):
    """Ingestion log + idempotency store. Unique (source, event_id) makes
    duplicate callbacks a no-op."""

    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("source", "event_id", name="uq_processed_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(128))
    entity_id: Mapped[str] = mapped_column(String(128), default="")
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # applied | late_applied
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_attempts: Mapped[int] = mapped_column(Integer, default=0)  # repeated callbacks seen
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"

    txn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    amount: Mapped[float] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    type: Mapped[str] = mapped_column(String(32), default="purchase")
    status: Mapped[str] = mapped_column(String(32), default="completed")
    merchant_id: Mapped[str] = mapped_column(String(64), default="")
    # Terminal (POS) that produced the transaction; the merchant master maps
    # terminal -> store -> merchant. Empty for legacy terminals without one.
    terminal_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LabelEvent(Base):
    """Chargeback / manual-review outcomes. They typically confirm days after
    the transaction, so they arrive late and trigger revisions of label-sourced
    window features; they also resolve training-sample labels."""

    __tablename__ = "label_events"

    label_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(128), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    label_type: Mapped[str] = mapped_column(String(32))  # chargeback | manual_review
    label_value: Mapped[str] = mapped_column(String(32))  # fraud | legit | suspicious
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # business effective time
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccountProfile(Base):
    """Daily account snapshots fed by the upstream account system."""

    __tablename__ = "account_profiles"
    __table_args__ = (UniqueConstraint("account_id", "snapshot_date", name="uq_profile_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    kyc_level: Mapped[str] = mapped_column(String(16), default="unknown")
    country: Mapped[str] = mapped_column(String(8), default="")
    risk_segment: Mapped[str] = mapped_column(String(32), default="")
    age_days: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Streaming compute state
# ---------------------------------------------------------------------------
class WindowEntry(Base):
    """Maintained per-(feature, entity) window contribution state. Only events
    relevant to the feature (post-filter) are kept, deduplicated by event id.
    Online reads aggregate this indexed, window-bounded state instead of
    scanning the raw transaction table."""

    __tablename__ = "window_entries"
    __table_args__ = (
        UniqueConstraint("feature_id", "entity_id", "event_id", name="uq_window_entry"),
        Index("ix_window_entries_lookup", "feature_id", "entity_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_id: Mapped[str] = mapped_column(String(128))
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)


class Checkpoint(Base):
    """Materialized window result for a (feature, entity) at an event-time
    boundary. Recomputed in-place when a late event affects it (audited via
    Revision)."""

    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("feature_id", "entity_id", "as_of", name="uq_checkpoint"),
        Index("ix_checkpoints_lookup", "feature_id", "entity_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[dict] = mapped_column(JSONB)  # {"value": <number|null>}
    revision_seq: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Revision(Base):
    """Audit record produced when a late event changes an already-materialized
    checkpoint. Scoped to exactly one (feature, entity, as_of)."""

    __tablename__ = "revisions"
    __table_args__ = (Index("ix_revisions_scope", "feature_id", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    cause_event_id: Mapped[str] = mapped_column(String(128))
    checkpoint_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Watermark(Base):
    """Compute progress per (feature, entity)."""

    __tablename__ = "watermarks"
    __table_args__ = (UniqueConstraint("feature_id", "entity_id", name="uq_watermark"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"))
    entity_id: Mapped[str] = mapped_column(String(128))
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # max event_time applied
    events_applied: Mapped[int] = mapped_column(Integer, default=0)
    late_events: Mapped[int] = mapped_column(Integer, default=0)
    last_applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
class TrainingSample(Base):
    __tablename__ = "training_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_key: Mapped[str] = mapped_column(String(128), unique=True)
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    decision_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transaction_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_value: Mapped[str | None] = mapped_column(String(32), nullable=True)
    label_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | confirmed | none
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Dataset(Base):
    """A built training set: rows computed with the same definitions and the
    same maintained state that online serving reads."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    feature_set_name: Mapped[str] = mapped_column(String(128))
    feature_set_version: Mapped[int] = mapped_column(Integer)
    rows: Mapped[list] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Merchant attribution (bi-temporal master data)
# ---------------------------------------------------------------------------
class AttributionBatch(Base):
    """One master-data correction batch. `seq` is a monotonic system-time
    order; `visible_at` is the instant from which a scoring read may see the
    batch's relation versions. Relation rows are immutable versions — ending
    an interval creates a new (truncated / retracted) version in a later
    batch instead of mutating history."""

    __tablename__ = "attribution_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    seq: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # terminal_migration | batch_correction | store_merge | acquirer_recoding | initial
    reason: Mapped[str] = mapped_column(String(32), default="batch_correction")
    source: Mapped[str] = mapped_column(String(32), default="merchant_master")
    note: Mapped[str] = mapped_column(Text, default="")
    visible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    impact_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MerchantAttribution(Base):
    """Immutable version of a terminal -> store -> merchant relation.

    Effective interval is right-open: ``[effective_from, effective_to)``.
    `status` is system-time bookkeeping (active / superseded / retracted);
    resolution deduplicates versions per (store, merchant) by the highest
    batch seq visible at the scoring instant, so a later version overrides an
    earlier one only for scoring times that can see the later batch.
    """

    __tablename__ = "merchant_attributions"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "terminal_id", "store_id", "merchant_id", "effective_from",
            name="uq_attribution_version",
        ),
        Index("ix_attribution_terminal_interval", "terminal_id", "effective_from"),
        Index("ix_attribution_merchant", "merchant_id", "effective_from"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("attribution_batches.id"), index=True)
    terminal_id: Mapped[str] = mapped_column(String(64), index=True)
    store_id: Mapped[str] = mapped_column(String(64))
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), default="high")  # high | medium | low
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | superseded | retracted
    # System-time retirement: the batch seq that superseded/retracted this
    # version. The row stays visible to scoring instants whose visible batch
    # seq is lower than this value — history is never rewritten.
    retired_at_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchant_attributions.id"), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttributionRevision(Base):
    """Attribution-module audit: which relation version a batch created,
    truncated or retracted. Feature-window revisions reference these rows by
    id, keeping the two responsibilities separately versioned."""

    __tablename__ = "attribution_revisions"
    __table_args__ = (Index("ix_attribution_revision_terminal", "terminal_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("attribution_batches.id"), index=True)
    terminal_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # created | truncated | retracted
    old_attribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchant_attributions.id"), nullable=True
    )
    new_attribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchant_attributions.id"), nullable=True
    )
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Merchant feature materialization (expanded attribution snapshots)
# ---------------------------------------------------------------------------
class MerchantWindowSnapshot(Base):
    """Chosen trade-off: each (feature, merchant, as_of) that has been scored
    keeps an expanded attribution snapshot so repeat online reads at the same
    scoring instant are index lookups instead of window scans + attribution
    joins. The price is paid on master revisions, which must locate exactly
    the snapshots touching a changed terminal interval and recompute them."""

    __tablename__ = "merchant_window_snapshots"
    __table_args__ = (
        UniqueConstraint("feature_id", "merchant_id", "as_of", name="uq_merchant_snapshot"),
        Index("ix_merchant_snapshot_lookup", "merchant_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[dict] = mapped_column(JSONB)  # {"value": <number|null>}
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    visible_batch_seq: Mapped[int] = mapped_column(Integer, default=0)
    # Highest batch seq visible at this as_of that touches one of this
    # merchant's terminals. Only this marker invalidates the cache, so another
    # merchant's correction never forces this snapshot to recompute.
    attribution_marker: Mapped[int] = mapped_column(Integer, default=0)
    data_version: Mapped[int] = mapped_column(Integer, default=0)  # transaction row count
    revision_seq: Mapped[int] = mapped_column(Integer, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MerchantSnapshotEntry(Base):
    """Expanded, per-transaction part of a snapshot. This is what makes master
    revisions precise: a batch can find the exact snapshots whose expanded
    entries touch a changed terminal inside the changed effective interval."""

    __tablename__ = "merchant_snapshot_entries"
    __table_args__ = (
        Index("ix_merchant_entry_terminal", "terminal_id", "event_time"),
        Index("ix_merchant_entry_snapshot", "snapshot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("merchant_window_snapshots.id"), index=True
    )
    txn_id: Mapped[str] = mapped_column(String(128))
    terminal_id: Mapped[str] = mapped_column(String(64), default="")
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    merchant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("merchant_attributions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="resolved")
    reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    included: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class MerchantWindowRevision(Base):
    """Feature-window revision module record: a master correction changed an
    already-materialized merchant window. References the attribution revision
    (and batch) that caused it — never rewrites evidence or archived samples."""

    __tablename__ = "merchant_window_revisions"
    __table_args__ = (
        Index("ix_merchant_window_revision_scope", "feature_id", "merchant_id", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    feature_id: Mapped[int] = mapped_column(ForeignKey("features.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict] = mapped_column(JSONB)
    attribution_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("attribution_revisions.id"), nullable=True
    )
    batch_id: Mapped[int] = mapped_column(ForeignKey("attribution_batches.id"), index=True)
    affected_txns: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Scoring evidence & archived sample attribution
# ---------------------------------------------------------------------------
class ScoreEvidence(Base):
    """Immutable online scoring evidence. Once written it is never recomputed:
    a later master correction produces new evidence and feature-window
    revisions, but the payload (and digest) of this row stays byte-identical."""

    __tablename__ = "score_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    evidence_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(16))  # account | merchant
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    feature_set_name: Mapped[str] = mapped_column(String(128))
    feature_set_version: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))
    visible_batch_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SampleAttributionArchive(Base):
    """Training-sample explanation module: the attribution view *as it was
    visible at the sample's decision time*, archived when the sample is
    created. Later master batches add new rows but never touch this one."""

    __tablename__ = "sample_attribution_archives"
    __table_args__ = (UniqueConstraint("sample_id", name="uq_sample_archive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("training_samples.id"), index=True)
    feature_set_name: Mapped[str] = mapped_column(String(128))
    feature_set_version: Mapped[int] = mapped_column(Integer)
    merchant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visible_batch_seq: Mapped[int] = mapped_column(Integer, default=0)
    values: Mapped[dict] = mapped_column(JSONB, default=dict)
    attribution_refs: Mapped[dict] = mapped_column(JSONB, default=dict)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
