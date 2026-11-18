#!/usr/bin/env python3
"""End-to-end acceptance verification against a RUNNING platform.

Exercises the acceptance criteria over HTTP:
  1. duplicate callbacks do not inflate window counts
  2. cross-day-boundary window membership
  3. late events revise only the affected account/windows
  4. delayed chargeback resolves the training label
  5. publishing mid-traffic never mixes versions
  6. offline (training replay) and online reads at the same as_of match
  7. unavailable account source reports status instead of fake zeros

Usage:  python scripts/verify_e2e.py [base_url]
Requires the backend running (default http://127.0.0.1:8000) with a fresh-ish
database (the script uses its own account ids and unique event ids).
"""
import json
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RUN = uuid.uuid4().hex[:8]
ACC = f"ACC-E2E-{RUN}"
OTHER = f"ACC-E2E-OTHER-{RUN}"

passed = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    passed.append(cond)
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"verification failed: {name}")


def req(method, path, body=None, token=None):
    r = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(r) as resp:
        return json.loads(resp.read())


def txn(i, account, event_time, amount):
    return {
        "txn_id": f"TXN-E2E-{RUN}-{i}",
        "account_id": account,
        "event_time": event_time,
        "amount": amount,
        "currency": "CNY",
        "type": "purchase",
        "status": "completed",
        "merchant_id": "M100",
    }


def online_vals(token, account, as_of, **params):
    q = urllib.parse.urlencode({"entity_id": account, "as_of": as_of, **params})
    resp = req("GET", f"/api/online/features?{q}", token=token)
    return {f["name"]: f["value"] for f in resp["features"]}, resp


def main():
    token = req("POST", "/api/auth/login", {"username": "analyst", "password": "analyst123"})["token"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t = lambda **kw: (now + timedelta(**kw)).isoformat()

    # account profile for real profile-feature values
    req("POST", "/api/ingest/account-profiles", [{
        "account_id": ACC, "snapshot_date": now.date().isoformat(),
        "kyc_level": "enhanced", "country": "CN", "risk_segment": "low", "age_days": 365,
    }], token)

    # --- 1. duplicates + 2. cross-day + 3. late revision --------------------
    e1 = txn(1, ACC, t(hours=-2), 100.0)          # outside the 1h window at as_of
    e2 = txn(2, ACC, t(minutes=-50), 200.0)       # inside
    e3 = txn(3, ACC, t(minutes=-45), 300.0)       # inside
    req("POST", "/api/ingest/transactions", [e1, e2, e3], token)
    dup = req("POST", "/api/ingest/transactions", [e2, e2], token)  # repeated callback
    check("重复回调被识别", dup["duplicates"] == 2)
    late = req("POST", "/api/ingest/transactions", [txn(4, ACC, t(hours=-2, minutes=-30), 50.0)], token)
    check("迟到事件被标记", late["late"] == 1)
    req("POST", "/api/ingest/transactions", [txn(5, OTHER, t(hours=-1), 999.0)], token)

    vals, _ = online_vals(token, ACC, t(minutes=-30))
    check("重复回调不增加窗口计数", vals["txn_count_1h"] == 2, f"got {vals['txn_count_1h']}")
    vals_7d, _ = online_vals(token, ACC, t(minutes=-30))
    check("窗口含迟到事件后计数正确", vals_7d["txn_count_7d"] == 4)

    other_vals, _ = online_vals(token, OTHER, t(minutes=-30))
    check("迟到修订不影响其他账户", other_vals["txn_count_7d"] == 1)

    revisions = req("GET", "/api/compute/revisions?limit=200", token=token)
    rev_scope = {r["entity_id"] for r in revisions if RUN in r["cause_event_id"] or r["entity_id"] in (ACC, OTHER)}
    check("修订只涉及受影响账户", rev_scope == {ACC}, f"got {rev_scope}")

    # cross-day: events 23:45 / 00:15 within one 1h window
    day = now.date() - timedelta(days=1)
    e6 = txn(6, ACC, f"{day}T23:45:00+00:00", 10.0)
    e7 = txn(7, ACC, f"{day + timedelta(days=1)}T00:15:00+00:00", 20.0)
    req("POST", "/api/ingest/transactions", [e6, e7], token)
    vals_cd, _ = online_vals(token, ACC, f"{day + timedelta(days=1)}T00:30:00+00:00")
    check("跨日窗口边界正确", vals_cd["txn_count_1h"] == 2, f"got {vals_cd['txn_count_1h']}")

    # --- 4. delayed chargeback ----------------------------------------------
    sample = req("POST", "/api/training/samples", {
        "sample_key": f"S-E2E-{RUN}", "entity_id": ACC,
        "decision_time": t(minutes=-30), "transaction_id": e1["txn_id"],
    }, token)
    check("标签到达前样本为 pending", sample["label_status"] == "pending")
    req("POST", "/api/ingest/labels", [{
        "label_id": f"LBL-E2E-{RUN}", "transaction_id": e1["txn_id"], "account_id": ACC,
        "label_type": "chargeback", "label_value": "fraud", "event_time": t(hours=-1),
    }], token)
    samples = req("GET", "/api/training/samples", token=token)
    s = next(x for x in samples if x["sample_key"] == f"S-E2E-{RUN}")
    check("延迟拒付确认后标签为 confirmed/fraud",
          s["label_status"] == "confirmed" and s["label_value"] == "fraud")

    # --- 6. offline/online consistency ---------------------------------------
    decision_time = t(minutes=-30)
    trace = req("GET", f"/api/training/samples/{s['id']}/trace", token=token)
    offline = {f["name"]: f["value"] for f in trace["features"]}
    online, _ = online_vals(token, ACC, decision_time)
    check("离线样本与在线读取同口径一致", offline == online,
          f"offline={offline} online={online}")
    count_trace = next(f for f in trace["features"] if f["name"] == "txn_count_1h")
    check("样本可追溯输入记录", all("txn_id" in i for i in count_trace["inputs"]))

    # --- 5. publish mid-traffic ----------------------------------------------
    feat_ver = 100 + (uuid.uuid4().int % 800)  # unique per run, re-runnable
    req("POST", "/api/features", {
        "name": "txn_count_1h", "version": feat_ver, "source": "transactions",
        "aggregation": "COUNT", "window_seconds": 7200, "owner": "e2e",
    }, token)
    pub = req("POST", "/api/feature-sets/fraud_scoring/publish", {
        "members": [{"name": "txn_count_1h", "version": feat_ver},
                    {"name": "txn_amount_sum_1h", "version": 1}],
        "note": "e2e release",
    }, token)
    new_v = pub["version"]
    resp_new = req("GET", f"/api/online/features?entity_id={ACC}", token=token)
    check("发布后默认读取新完整版本",
          resp_new["feature_set"]["version"] == new_v
          and {(f["name"], f["version"]) for f in resp_new["features"]}
          == {("txn_count_1h", feat_ver), ("txn_amount_sum_1h", 1)})
    resp_old = req("GET", f"/api/online/features?entity_id={ACC}&set_version=1", token=token)
    check("显式钉住旧版本仍完整可读",
          resp_old["feature_set"]["version"] == 1 and len(resp_old["features"]) == 9)
    # restore pointer to v1 for subsequent checks
    sets = req("GET", "/api/feature-sets", token=token)
    v1_members = [{"name": m["name"], "version": m["version"]}
                  for m in next(x for x in sets if x["name"] == "fraud_scoring" and x["version"] == 1)["members"]]
    req("POST", "/api/feature-sets/fraud_scoring/publish", {"members": v1_members, "note": "e2e restore"}, token)

    # --- 7. source outage -----------------------------------------------------
    req("POST", "/api/sources/outage", {
        "source": "account_profiles", "status": "unavailable", "message": "e2e outage drill",
    }, token)
    resp_out = req("GET", f"/api/online/features?entity_id={ACC}", token=token)
    kyc = next(f for f in resp_out["features"] if f["name"] == "account_kyc_level")
    check("账户源中断返回状态而非零值",
          kyc["status"] == "unavailable" and kyc["value"] is None)
    cnt = next(f for f in resp_out["features"] if f["name"] == "txn_count_1h")
    check("交易类特征不受账户源影响", cnt["status"] == "ok")
    req("POST", "/api/sources/outage", {"source": "account_profiles", "status": "ok", "message": ""}, token)

    print(f"\n全部 {len(passed)} 项端到端验收检查通过。")


if __name__ == "__main__":
    main()
