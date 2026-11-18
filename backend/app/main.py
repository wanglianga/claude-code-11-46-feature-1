"""FastAPI application entrypoint. Creates schema and seeds the demo catalog
(users, source statuses, the default fraud_scoring feature set) idempotently."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .config import settings
from .db import Base, SessionLocal, engine
from .models import AttributionBatch, Feature, SourceStatus, User
from .routers import (
    attribution,
    auth,
    compute,
    evidence,
    features,
    ingest,
    merchant,
    online,
    simulate,
    training,
)
from .security import hash_password
from .services.features import publish_feature_set

SEED_USERS = [
    ("analyst", "analyst123", "analyst"),
    ("admin", "admin123", "admin"),
]

SEED_SOURCES = ["transactions", "account_profiles", "labels", "merchant_master"]

# Merchant-level features. `merchant_terminal_count_7d` (近七日同商户终端数) is
# the one that depends on the terminal→store→merchant attribution.
SEED_MERCHANT_FEATURES = [
    dict(name="merchant_txn_count_7d", source="merchant_transactions", aggregation="COUNT",
         window_seconds=604800, description="近7天同商户交易笔数", owner="risk-ops"),
    dict(name="merchant_terminal_count_7d", source="merchant_transactions",
         aggregation="COUNT_DISTINCT", field="terminal_id", window_seconds=604800,
         description="近七日同商户终端数（依赖终端→门店→商户归属）", owner="risk-ops"),
    dict(name="merchant_amount_sum_1d", source="merchant_transactions", aggregation="SUM",
         field="amount", window_seconds=86400, description="近24小时同商户交易金额合计",
         owner="risk-ops"),
    dict(name="merchant_txn_count_1h", source="merchant_transactions", aggregation="COUNT",
         window_seconds=3600, description="近1小时同商户交易笔数", owner="risk-ops"),
]

# Initial merchant master batch: two stores under M110 and one under M120.
SEED_ATTRIBUTIONS = [
    dict(terminal_id="T1001", store_id="S-M110-01", merchant_id="M110", confidence="high",
         effective_from="2026-01-01T00:00:00+00:00"),
    dict(terminal_id="T1002", store_id="S-M110-01", merchant_id="M110", confidence="high",
         effective_from="2026-01-01T00:00:00+00:00"),
    dict(terminal_id="T1003", store_id="S-M110-02", merchant_id="M110", confidence="high",
         effective_from="2026-01-01T00:00:00+00:00"),
    dict(terminal_id="T1004", store_id="S-M120-01", merchant_id="M120", confidence="high",
         effective_from="2026-01-01T00:00:00+00:00"),
    dict(terminal_id="T1005", store_id="S-M120-01", merchant_id="M120", confidence="medium",
         effective_from="2026-01-01T00:00:00+00:00"),
]

# Default feature catalog for the fraud_scoring set (all windows are
# event-time sliding windows; profile features read the latest daily snapshot).
SEED_FEATURES = [
    dict(name="txn_count_1h", source="transactions", aggregation="COUNT",
         window_seconds=3600, description="近1小时交易笔数", owner="risk-ml"),
    dict(name="txn_amount_sum_1h", source="transactions", aggregation="SUM", field="amount",
         window_seconds=3600, description="近1小时交易金额合计", owner="risk-ml"),
    dict(name="txn_amount_avg_24h", source="transactions", aggregation="AVG", field="amount",
         window_seconds=86400, description="近24小时平均交易金额", owner="risk-ml"),
    dict(name="txn_amount_max_24h", source="transactions", aggregation="MAX", field="amount",
         window_seconds=86400, description="近24小时最大单笔金额", owner="risk-ml"),
    dict(name="txn_count_7d", source="transactions", aggregation="COUNT",
         window_seconds=604800, description="近7天交易笔数", owner="risk-ml"),
    dict(name="chargeback_count_30d", source="labels", aggregation="COUNT",
         window_seconds=2592000, filter_json={"label_type": "chargeback"},
         description="近30天拒付笔数", owner="risk-ops"),
    dict(name="account_kyc_level", source="account_profile", aggregation="LAST",
         field="kyc_level", value_type="string", description="账户KYC等级（最新日快照）",
         owner="risk-ops"),
    dict(name="account_age_days", source="account_profile", aggregation="LAST",
         field="age_days", description="账户开户天数（最新日快照）", owner="risk-ops"),
    dict(name="account_country", source="account_profile", aggregation="LAST",
         field="country", value_type="string", description="账户国家（最新日快照）",
         owner="risk-ops"),
]


def seed(db) -> None:
    for username, password, role in SEED_USERS:
        if db.execute(select(User).where(User.username == username)).scalar_one_or_none() is None:
            db.add(User(username=username, password_hash=hash_password(password), role=role))
    for source in SEED_SOURCES:
        if db.get(SourceStatus, source) is None:
            db.add(SourceStatus(source=source, status="ok"))
    db.commit()

    # Seed the default feature set only if nothing exists yet.
    if db.execute(select(Feature).limit(1)).scalar_one_or_none() is None:
        for spec in SEED_FEATURES:
            feature = Feature(
                name=spec["name"], version=1,
                description=spec.get("description", ""),
                owner=spec.get("owner", "risk-ml"),
                source=spec["source"], aggregation=spec["aggregation"],
                field=spec.get("field"), window_seconds=spec.get("window_seconds"),
                filter_json=spec.get("filter_json"),
                value_type=spec.get("value_type", "number"),
                status="draft", created_by="seed",
            )
            db.add(feature)
        for spec in SEED_MERCHANT_FEATURES:
            db.add(Feature(
                name=spec["name"], version=1,
                description=spec.get("description", ""),
                owner=spec.get("owner", "risk-ops"),
                entity_key="merchant_id",
                source=spec["source"], aggregation=spec["aggregation"],
                field=spec.get("field"), window_seconds=spec.get("window_seconds"),
                value_type="number", status="draft", created_by="seed",
            ))
        db.commit()
        publish_feature_set(
            db,
            "fraud_scoring",
            [{"name": spec["name"], "version": 1} for spec in SEED_FEATURES],
            note="initial seed release",
            created_by="seed",
        )
        publish_feature_set(
            db,
            "merchant_risk",
            [{"name": spec["name"], "version": 1} for spec in SEED_MERCHANT_FEATURES],
            note="initial merchant attribution release",
            created_by="seed",
        )

    # Seed the initial merchant master batch only if no batch exists yet.
    if db.execute(select(AttributionBatch).limit(1)).scalar_one_or_none() is None:
        from .services import attribution as attribution_service

        attribution_service.ingest_batch(
            db,
            batch_key="B-SEED-MASTER",
            relations=SEED_ATTRIBUTIONS,
            reason="initial",
            source="merchant_master",
            note="初始终端-门店-商户关系基线",
            visible_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            created_by="seed",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _ensure_dev_columns()
    with SessionLocal() as db:
        seed(db)
    yield


def _ensure_dev_columns() -> None:
    """Additive dev-schema compatibility. `create_all` never alters existing
    tables, so a database created by an earlier build would miss the terminal
    column. Production deployments use migrations; this keeps local upgrades
    one-command without dropping data."""
    from sqlalchemy import text

    statements = [
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS terminal_id VARCHAR(64) NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS ix_transactions_terminal_id ON transactions (terminal_id)",
        "ALTER TABLE merchant_attributions ADD COLUMN IF NOT EXISTS retired_at_seq INTEGER",
        "ALTER TABLE merchant_window_snapshots ADD COLUMN IF NOT EXISTS attribution_marker INTEGER NOT NULL DEFAULT 0",
    ]
    with engine.begin() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception:  # pragma: no cover - table may not exist yet
                pass


app = FastAPI(
    title="Anti-Fraud Feature Platform",
    version="1.0.0",
    description="特征计算与在线服务平台：统一离线训练与在线评分的特征口径",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(features.router)
app.include_router(ingest.router)
app.include_router(compute.router)
app.include_router(online.router)
app.include_router(training.router)
app.include_router(simulate.router)
app.include_router(merchant.router)
app.include_router(attribution.router)
app.include_router(evidence.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
