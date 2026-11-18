"""FastAPI application entrypoint. Creates schema and seeds the demo catalog
(users, source statuses, the default fraud_scoring feature set) idempotently."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from .config import settings
from .db import Base, SessionLocal, engine
from .models import Feature, SourceStatus, User
from .routers import auth, compute, features, ingest, online, simulate, training
from .security import hash_password
from .services.features import publish_feature_set

SEED_USERS = [
    ("analyst", "analyst123", "analyst"),
    ("admin", "admin123", "admin"),
]

SEED_SOURCES = ["transactions", "account_profiles", "labels"]

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
        db.commit()
        publish_feature_set(
            db,
            "fraud_scoring",
            [{"name": spec["name"], "version": 1} for spec in SEED_FEATURES],
            note="initial seed release",
            created_by="seed",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    yield


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


@app.get("/api/health")
def health():
    return {"status": "ok"}
