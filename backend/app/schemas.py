"""Request/response schemas."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class FeatureSpecIn(BaseModel):
    name: str
    version: int | None = None
    description: str = ""
    owner: str | None = None
    entity_key: str = "account_id"
    source: str  # transactions | labels | account_profile
    aggregation: str  # COUNT | SUM | AVG | MIN | MAX | LAST
    field: str | None = None
    window_seconds: int | None = None
    filter_json: dict | None = None
    value_type: str = "number"


class PublishMemberIn(BaseModel):
    name: str
    version: int


class PublishIn(BaseModel):
    members: list[PublishMemberIn]
    note: str = ""


class TransactionEventIn(BaseModel):
    txn_id: str
    account_id: str
    event_time: datetime
    amount: float
    currency: str = "CNY"
    type: str = "purchase"
    status: str = "completed"
    merchant_id: str = ""
    terminal_id: str = ""


class LabelEventIn(BaseModel):
    label_id: str
    transaction_id: str
    account_id: str
    label_type: str  # chargeback | manual_review
    label_value: str  # fraud | legit | suspicious
    event_time: datetime


class AccountProfileIn(BaseModel):
    account_id: str
    snapshot_date: date
    kyc_level: str = "unknown"
    country: str = ""
    risk_segment: str = ""
    age_days: int = 0


class SampleIn(BaseModel):
    sample_key: str
    entity_id: str
    decision_time: datetime
    transaction_id: str | None = None


class DatasetIn(BaseModel):
    name: str
    feature_set: str = "fraud_scoring"
    set_version: int | None = None
    sample_ids: list[int] | None = None


class SimulateTransactionsIn(BaseModel):
    accounts: list[str] | None = None
    count: int = 60
    span_hours: int = 48
    duplicate_rate: float = 0.15
    late_rate: float = 0.15
    seed: int = 42


class SimulateAccountsIn(BaseModel):
    accounts: int = 8
    days: int = 7
    seed: int = 42


class SimulateLabelsIn(BaseModel):
    fraud_rate: float = 0.12
    min_delay_days: int = 1
    max_delay_days: int = 5
    seed: int = 42


class SourceOutageIn(BaseModel):
    source: str = Field(pattern="^(transactions|account_profiles|labels)$")
    status: str = Field(pattern="^(ok|degraded|unavailable)$")
    message: str = ""


# ---------------------------------------------------------------------------
# Merchant attribution
# ---------------------------------------------------------------------------
class AttributionRelationIn(BaseModel):
    terminal_id: str
    store_id: str
    merchant_id: str
    effective_from: datetime
    effective_to: datetime | None = None
    confidence: str = "high"  # high | medium | low
    mode: str = "supersede"  # supersede | coexist
    note: str = ""


class AttributionBatchIn(BaseModel):
    batch_key: str
    reason: str = "batch_correction"
    source: str = "merchant_master"
    note: str = ""
    visible_at: datetime | None = None
    relations: list[AttributionRelationIn]


class MerchantScoreIn(BaseModel):
    merchant_id: str
    feature_set: str = "merchant_risk"
    set_version: int | None = None
    as_of: datetime | None = None


class SimulateMerchantTransactionsIn(BaseModel):
    count: int = 60
    span_hours: int = 168
    seed: int = 42


class SimulateMerchantMasterIn(BaseModel):
    seed: int = 42
