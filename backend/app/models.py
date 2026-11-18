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
