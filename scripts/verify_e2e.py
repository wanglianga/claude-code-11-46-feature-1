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
  8. merchant attribution revision impact: terminal migration scope, overlap /
     low-confidence review reasons, archived samples and scoring evidence stay
     immutable, peak reads hit expanded snapshots, offline/online alignment

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


def txn_merchant(i, account, event_time, amount, terminal_id):
    return {
        "txn_id": f"MTXN-E2E-{RUN}-{i}",
        "account_id": account,
        "event_time": event_time,
        "amount": amount,
        "currency": "CNY",
        "type": "purchase",
        "status": "completed",
        "merchant_id": "",
        "terminal_id": terminal_id,
    }


def merchant_vals(token, merchant, as_of, **params):
    q = urllib.parse.urlencode({"entity_id": merchant, "as_of": as_of, **params})
    resp = req("GET", f"/api/online/merchant-features?{q}", token=token)
    return {f["name"]: f["value"] for f in resp["features"]}, resp


def merchant_feature(resp, name):
    return next(f for f in resp["features"] if f["name"] == name)


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

    # --- 8. merchant attribution revision impact -----------------------------
    run_attribution_checks(token, now, t)

    print(f"\n全部 {len(passed)} 项端到端验收检查通过。")


def run_attribution_checks(token, now, t):
    # Merchant windows are scoped to terminals related to the merchant under
    # test, and every resource id below is unique to this run, so re-running
    # against an accumulated database stays isolated.
    run = uuid.uuid4().hex[:6]
    m_old, m_new = f"M-E2E-{run}-A", f"M-E2E-{run}-B"
    term = f"T-E2E-{run}"
    overlap_merchant = f"M-E2E-{run}-C"
    low_term = f"T-E2E-{run}-LOW"
    low_merchant = f"M-E2E-{run}-LOW"
    acc = f"ACC-E2E-M-{run}"

    # initial master batch for this run's terminal
    base_from = t(days=-30)
    req("POST", "/api/attribution/batches", {
        "batch_key": f"B-E2E-BASE-{run}", "reason": "initial",
        "visible_at": t(days=-30),
        "relations": [{"terminal_id": term, "store_id": f"S-{run}-01",
                       "merchant_id": m_old, "effective_from": base_from,
                       "confidence": "high"}],
    }, token)

    # Three transactions: two before the migration, one after. All are ingested
    # before the correcting batch, and M_old's window is materialized now, so
    # the batch must revise exactly the already-seen post-migration window.
    migrate_at = t(hours=-3)
    req("POST", "/api/ingest/transactions", [
        FIRST_TXN := txn_merchant("1", acc, t(hours=-6), 100.0, term),
        txn_merchant("2", acc, t(hours=-5), 50.0, term),
        txn_merchant("3", acc, t(hours=-1), 70.0, term),
    ], token)
    vals, _ = merchant_vals(token, m_old, t(hours=-4))
    check("迁移前同商户终端数=1", vals["merchant_terminal_count_7d"] == 1,
          f"got {vals.get('merchant_terminal_count_7d')}")
    check("迁移前同商户交易笔数=2", vals["merchant_txn_count_7d"] == 2)
    # Materialize M_old's current 7d window too (contains all three txns).
    pre_mig, _ = merchant_vals(token, m_old, t(minutes=0))
    check("纠错前窗口包含迁移后交易", pre_mig["merchant_txn_count_7d"] == 3,
          f"got {pre_mig.get('merchant_txn_count_7d')}")

    migration = req("POST", "/api/attribution/batches", {
        "batch_key": f"B-E2E-MIG-{run}", "reason": "terminal_migration",
        "note": "e2e 终端迁店", "visible_at": migrate_at,
        "relations": [{"terminal_id": term, "store_id": f"S-{run}-02",
                       "merchant_id": m_new, "effective_from": migrate_at,
                       "confidence": "high", "mode": "supersede"}],
    }, token)
    impact = req("GET", f"/api/attribution/batches/{migration['id']}", token=token)["impact"]
    check("迁店影响范围仅包含迁移后交易",
          impact["affected_transaction_count"] == 1
          and [x["txn_id"] for x in impact["affected_transactions"]] == [f"MTXN-E2E-{RUN}-3"],
          f"impact={impact['affected_transactions']}")
    check("纠错精确重算受影响已展开窗口",
          impact["affected_snapshots"] >= 1 and impact["revisions"] >= 1,
          f"snapshots={impact['affected_snapshots']} revisions={impact['revisions']}")

    old_vals, _ = merchant_vals(token, m_old, t(minutes=0))
    new_vals, _ = merchant_vals(token, m_new, t(minutes=0))
    check("迁店后原商户不再计入新交易", old_vals["merchant_txn_count_7d"] == 2,
          f"got {old_vals.get('merchant_txn_count_7d')}")
    check("迁店后新商户计入迁移后交易", new_vals["merchant_txn_count_7d"] == 1)

    revs = req("GET", "/api/merchant/window-revisions?limit=200", token=token)
    check("主档修订生成窗口修订审计",
          any(r["merchant_id"] == m_old and r["batch_id"] == migration["id"] for r in revs))

    # archived sample + evidence immutability across a backdated correction
    sample = req("POST", "/api/training/samples", {
        "sample_key": f"S-E2E-M-{run}", "entity_id": acc,
        "decision_time": t(hours=-4), "transaction_id": FIRST_TXN["txn_id"],
    }, token)
    scored = req("GET", f"/api/online/merchant-features?entity_id={m_old}&as_of={urllib.parse.quote(t(hours=-4))}&record_evidence=true", token=token)
    ev_id = scored["evidence"]["id"]
    ev_before = req("GET", f"/api/evidence/{ev_id}", token=token)
    trace_before = req("GET", f"/api/training/samples/{sample['id']}/trace", token=token)
    archive_before = trace_before["merchant_attribution"]["archive"]

    req("POST", "/api/attribution/batches", {
        "batch_key": f"B-E2E-CORR-{run}", "reason": "batch_correction",
        "visible_at": t(days=-10),
        "relations": [{"terminal_id": term, "store_id": f"S-{run}-00",
                       "merchant_id": f"M-E2E-{run}-Z", "effective_from": t(days=-60),
                       "confidence": "high"}],
    }, token)
    ev_after = req("GET", f"/api/evidence/{ev_id}", token=token)
    trace_after = req("GET", f"/api/training/samples/{sample['id']}/trace", token=token)
    check("评分证据的摘要与载荷不被主档修订改写",
          ev_after["digest"] == ev_before["digest"]
          and ev_after["payload"] == ev_before["payload"]
          and ev_after["integrity"]["immutable"] is True)
    check("归档训练样本的归属视图不变",
          trace_after["merchant_attribution"]["archive"] == archive_before
          and archive_before["merchant_id"] == m_old)
    check("样本稳定性检查报告差异但不改写",
          trace_after["merchant_attribution"]["stability"]["stable"] is False)
    check("异常评分可追溯关系来源与有效区间",
          all(k in ev_after["payload"]["attribution_context"]["transactions"][0]["attribution"]
              for k in ("batch_key", "effective_from", "candidates")))

    # overlapping valid relations -> needs_review, not arbitrary choice
    overlap_from, overlap_to = t(hours=-8), t(hours=-2)
    req("POST", "/api/attribution/batches", {
        "batch_key": f"B-E2E-OVL-{run}", "reason": "batch_correction",
        "visible_at": overlap_from,
        "relations": [{"terminal_id": term, "store_id": f"S-{run}-03",
                       "merchant_id": overlap_merchant, "effective_from": overlap_from,
                       "effective_to": overlap_to, "confidence": "medium",
                       "mode": "coexist"}],
    }, token)
    ovl_vals, ovl_resp = merchant_vals(token, overlap_merchant, t(hours=-4))
    ovl_feat = merchant_feature(ovl_resp, "merchant_terminal_count_7d")
    check("区间重叠返回待核对而非任选归属",
          ovl_feat["status"] == "needs_review"
          and ovl_feat["needs_review_reasons"].get("overlapping_intervals", 0) >= 1
          and ovl_feat["value"] == 0,
          f"feat={ovl_feat.get('status')} reasons={ovl_feat.get('needs_review_reasons')}")
    item = next(i for i in ovl_feat["review_items"]
                if i["txn_id"] == f"MTXN-E2E-{RUN}-2")
    cand_merchants = {c["merchant_id"] for c in item["candidates"]}
    check("待核对项列出全部候选关系（含新重叠商户与既有纠错归属）",
          len(cand_merchants) >= 2 and overlap_merchant in cand_merchants,
          f"candidates={sorted(cand_merchants)}")

    # low-confidence-only -> needs_review
    req("POST", "/api/attribution/batches", {
        "batch_key": f"B-E2E-LOW-{run}", "reason": "batch_correction",
        "visible_at": t(days=-2),
        "relations": [{"terminal_id": low_term, "store_id": f"S-{run}-LOW",
                       "merchant_id": low_merchant, "effective_from": t(days=-30),
                       "confidence": "low"}],
    }, token)
    req("POST", "/api/ingest/transactions", [txn_merchant("L", acc, t(hours=-1), 20.0, low_term)], token)
    low_vals, low_resp = merchant_vals(token, low_merchant, t(minutes=0))
    low_feat = merchant_feature(low_resp, "merchant_terminal_count_7d")
    check("仅低置信映射返回待核对原因",
          low_feat["status"] == "needs_review"
          and low_feat["needs_review_reasons"].get("low_confidence") == 1
          and low_feat["value"] == 0)

    # peak query: a scoring instant not read before materializes once, then
    # every repeat read at that same instant hits the expanded snapshot
    peak_as_of = t(minutes=-17)
    _, p1 = merchant_vals(token, m_new, peak_as_of)
    _, p2 = merchant_vals(token, m_new, peak_as_of)
    check("高峰重复查询命中已展开归属快照",
          merchant_feature(p1, "merchant_terminal_count_7d")["cache"] == "miss"
          and merchant_feature(p2, "merchant_terminal_count_7d")["cache"] == "hit",
          f"p1={merchant_feature(p1, 'merchant_terminal_count_7d')['cache']} "
          f"p2={merchant_feature(p2, 'merchant_terminal_count_7d')['cache']}")

    # offline/online same caliber at the same scoring instant
    snap_id = merchant_feature(p2, "merchant_terminal_count_7d")["snapshot_id"]
    snap = req("GET", f"/api/merchant/snapshots/{snap_id}", token=token)
    check("离线/在线使用同一归属版本",
          snap["summary"]["visible_batch_seq"]
          == merchant_feature(p2, "merchant_terminal_count_7d")["visible_batch_seq"])


if __name__ == "__main__":
    main()
