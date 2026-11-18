"""Acceptance tests for merchant attribution revision impact analysis.

Scenarios from the task:
* terminal migration (终端迁店): a terminal moves store/merchant with a new
  effective interval; history before the move keeps the old attribution, and
  the correction produces an impact scope + new feature-window revisions
  instead of rewriting anything.
* batch master correction (批量主档纠错): several terminals corrected at once;
  only the affected windows are recomputed, other merchants are untouched.
* adjacent overlapping intervals (相邻区间重叠): a transaction that hits two
  valid stores must return needs_review with its candidates, not one arbitrary
  attribution.
* low-confidence-only mapping must also return needs_review.
* peak query (高峰查询): repeated reads at the same scoring instant are served
  from the expanded snapshot; both offline and online use the same attribution
  version.
* archived training samples and existing scoring evidence are never rewritten
  by a later master batch; the anomaly trace links relation source, effective
  interval and excluded candidates.
"""
import threading
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.services import merchant_features, serving

from .conftest import make_txn

UTC = timezone.utc
BASE = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)


def _iso(dt):
    return dt.isoformat()


def _mktxn(txn_id, account, event_time, amount, terminal_id, merchant_id="UNSEEN"):
    if isinstance(event_time, datetime):
        event_time = _iso(event_time)
    return make_txn(txn_id, account, event_time, amount, terminal_id=terminal_id,
                    merchant_id=merchant_id)


def _ingest(client, auth, events):
    r = client.post("/api/ingest/transactions", json=events, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _batch(client, auth, body):
    r = client.post("/api/attribution/batches", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _merchant_read(client, auth, merchant_id, as_of, **params):
    q = {"entity_id": merchant_id, "as_of": as_of, **params}
    r = client.get("/api/online/merchant-features", params=q, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _values(resp):
    return {f["name"]: f["value"] for f in resp["features"]}


def _feature(resp, name):
    return next(f for f in resp["features"] if f["name"] == name)


# ---------------------------------------------------------------------------
# 1. Seed resolution + "近七日同商户终端数"
# ---------------------------------------------------------------------------
def test_seed_master_and_terminal_count(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    # Three terminals of M110 (T1001/T1002 in store 01, T1003 in store 02).
    _ingest(client, auth, [
        _mktxn("TXN-SD-1", "ACC-M110", now - timedelta(hours=2), 10.0, "T1001"),
        _mktxn("TXN-SD-2", "ACC-M110", now - timedelta(hours=2), 20.0, "T1002"),
        _mktxn("TXN-SD-3", "ACC-M110", now - timedelta(hours=2), 30.0, "T1003"),
        # M120 terminal must not contribute to M110.
        _mktxn("TXN-SD-4", "ACC-M120", now - timedelta(hours=2), 99.0, "T1004"),
    ])
    resp = _merchant_read(client, auth, "M110", _iso(now))
    vals = _values(resp)
    assert vals["merchant_terminal_count_7d"] == 3
    assert vals["merchant_txn_count_7d"] == 3
    assert vals["merchant_amount_sum_1d"] == 60.0
    term = _feature(resp, "merchant_terminal_count_7d")
    assert term["status"] == "ok"
    assert term["visible_batch_seq"] >= 1
    assert "B-SEED-MASTER" in term["source_batches"]

    # The other merchant sees only its own terminal.
    resp120 = _merchant_read(client, auth, "M120", _iso(now))
    assert _values(resp120)["merchant_terminal_count_7d"] == 1


# ---------------------------------------------------------------------------
# 2. Terminal migration: history at an earlier scoring instant is unchanged
# ---------------------------------------------------------------------------
def test_terminal_migration_preserves_history_and_revises_only_affected(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    migrate_at = now - timedelta(hours=1)
    txn_old = _mktxn("TXN-MIG-OLD", "ACC-MIG", now - timedelta(hours=3), 100.0, "T1003")
    txn_new = _mktxn("TXN-MIG-NEW", "ACC-MIG", now - timedelta(minutes=20), 50.0, "T1003")
    _ingest(client, auth, [txn_old, txn_new])

    # Score M110 now -> materializes an expanded snapshot (both txns included).
    before = _merchant_read(client, auth, "M110", _iso(now))
    before_vals = _values(before)
    assert before_vals["merchant_terminal_count_7d"] == 1  # same terminal, 2 txns
    assert before_vals["merchant_txn_count_7d"] == 2

    # Terminals T1001..T1005 seeded above + those of this test may exist; keep
    # the assertion scoped to the migrated terminal's contribution.

    # Master correction: T1003 moves from M110 to M130 from migrate_at onwards.
    batch = _batch(client, auth, {
        "batch_key": "B-MIG-1",
        "reason": "terminal_migration",
        "note": "T1003 迁店至 M130",
        "visible_at": _iso(migrate_at),
        "relations": [{
            "terminal_id": "T1003",
            "store_id": "S-M130-09",
            "merchant_id": "M130",
            "effective_from": _iso(migrate_at),
            "confidence": "high",
            "mode": "supersede",
        }],
    })
    impact = batch["impact"]
    assert impact["affected_transaction_count"] == 1  # only the post-migration txn
    assert [t["txn_id"] for t in impact["affected_transactions"]] == ["TXN-MIG-NEW"]
    assert impact["affected_snapshots"] >= 1
    assert impact["revisions"] >= 1

    # The pre-migration transaction keeps its old attribution at an earlier
    # scoring instant: a read pinned to before the batch's visibility still
    # sees the M110 relation.
    before_batch = _batch_read_as_of(client, auth, "M110", migrate_at - timedelta(minutes=10))
    # The old txn is attributed to M110 at a scoring instant before the batch.
    ctx = before_batch
    old_txn_ctx = next(t for t in ctx if t["txn_id"] == "TXN-MIG-OLD")
    assert old_txn_ctx["merchant_id"] == "M110"
    assert old_txn_ctx["attribution_id"] == old_rel_id(client, auth, "T1003")

    # Post-move txn now belongs to M130 and no longer to M110.
    after = _merchant_read(client, auth, "M110", _iso(now))
    after_vals = _values(after)
    assert after_vals["merchant_txn_count_7d"] == 1  # only the pre-move txn
    resp130 = _merchant_read(client, auth, "M130", _iso(now))
    assert _values(resp130)["merchant_txn_count_7d"] == 1

    # Feature-window revisions were produced, referencing the batch.
    revs = client.get("/api/merchant/window-revisions", headers=auth).json()
    mine = [r for r in revs if r["merchant_id"] == "M110" and r["batch_id"] == batch["id"]]
    assert mine, "expected a merchant window revision for the migrated terminal"
    assert mine[0]["old_value"] != mine[0]["new_value"]

    # Attribution module kept the old relation version visible to history.
    relations = client.get("/api/attribution/relations",
                           params={"terminal_id": "T1003"}, headers=auth).json()
    # The original open-ended M110 version is retired by the batch but still
    # listed (history is not rewritten); a truncated replacement version ends
    # exactly at the migration instant.
    retired = next(r for r in relations
                   if r["merchant_id"] == "M110" and r["effective_to"] is None
                   and r["retired_at_batch_seq"] is not None)
    prefix = next(r for r in relations
                  if r["merchant_id"] == "M110" and r["effective_to"] == _iso(migrate_at)
                  and r["retired_at_batch_seq"] is None)
    moved = next(r for r in relations if r["merchant_id"] == "M130")
    assert retired["batch_seq"] < moved["batch_seq"]
    assert prefix["attribution_id"] != retired["attribution_id"]
    assert moved["effective_from"] == _iso(migrate_at)


def old_rel_id(client, auth, terminal_id):
    rels = client.get("/api/attribution/relations",
                      params={"terminal_id": terminal_id}, headers=auth).json()
    return next(r["attribution_id"] for r in rels if r["merchant_id"] == "M110")


def _batch_read_as_of(client, auth, merchant_id, as_of):
    resp = client.get("/api/online/merchant-features",
                      params={"entity_id": merchant_id, "as_of": _iso(as_of)},
                      headers=auth).json()
    return _snapshot_context(client, auth, resp)


def _snapshot_context(client, auth, resp):
    term = _feature(resp, "merchant_terminal_count_7d")
    snap = client.get(f"/api/merchant/snapshots/{term['snapshot_id']}", headers=auth).json()
    return snap["entries"]


# ---------------------------------------------------------------------------
# 3. Batch master correction: precise impact scope, other data untouched
# ---------------------------------------------------------------------------
def test_batch_correction_scope_is_precise(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    _ingest(client, auth, [
        _mktxn("TXN-BC-1", "ACC-BC", now - timedelta(hours=2), 10.0, "T1001"),
        _mktxn("TXN-BC-2", "ACC-BC", now - timedelta(hours=2), 20.0, "T1002"),
        _mktxn("TXN-BC-3", "ACC-BC", now - timedelta(hours=2), 30.0, "T1003"),
        _mktxn("TXN-BC-4", "ACC-BC", now - timedelta(hours=2), 40.0, "T1004"),
    ])
    read_before = _merchant_read(client, auth, "M110", _iso(now))
    m120_before = _merchant_read(client, auth, "M120", _iso(now))
    snapshot_count_before = _snapshot_ids(client, auth, "M120")

    correction_at = now - timedelta(hours=1)
    effective_from = now - timedelta(hours=3)  # covers the two transactions
    batch = _batch(client, auth, {
        "batch_key": "B-BC-1",
        "reason": "batch_correction",
        "note": "批量纠错：T1001/T1002 自 effective_from 迁至 M140",
        "visible_at": _iso(correction_at),
        "relations": [
            {"terminal_id": "T1001", "store_id": "S-M140-01", "merchant_id": "M140",
             "effective_from": _iso(effective_from), "confidence": "high"},
            {"terminal_id": "T1002", "store_id": "S-M140-01", "merchant_id": "M140",
             "effective_from": _iso(effective_from), "confidence": "high"},
        ],
    })
    impact = batch["impact"]
    assert set(impact["changed_terminals"]) == {"T1001", "T1002"}
    # Only the two corrected terminals' transactions are in scope.
    assert {t["terminal_id"] for t in impact["affected_transactions"]} == {"T1001", "T1002"}
    # T1003/T1004 transactions are untouched.
    assert "TXN-BC-3" not in {t["txn_id"] for t in impact["affected_transactions"]}

    # M120's materialized windows are byte-identical (no revision, no rewrite).
    assert _snapshot_ids(client, auth, "M120") == snapshot_count_before
    revs = client.get("/api/merchant/window-revisions", headers=auth).json()
    assert not [r for r in revs if r["merchant_id"] == "M120" and r["batch_id"] == batch["id"]]

    # M110 no longer owns T1001/T1002 post-correction; M140 does.
    after = _merchant_read(client, auth, "M110", _iso(now))
    assert _values(after)["merchant_txn_count_7d"] == 1  # only T1003
    assert _values(_merchant_read(client, auth, "M140", _iso(now)))["merchant_txn_count_7d"] == 2


def _snapshot_ids(client, auth, merchant_id):
    now = datetime.now(UTC).replace(microsecond=0)
    resp = _merchant_read(client, auth, merchant_id, _iso(now))
    return sorted(f["snapshot_id"] for f in resp["features"] if f.get("snapshot_id"))


# ---------------------------------------------------------------------------
# 4. Overlapping intervals -> needs_review with candidates, no arbitrary pick
# ---------------------------------------------------------------------------
def test_overlapping_intervals_require_review_not_arbitrary_choice(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    # T1004 is seeded to M120. Add an overlapping relation to another store
    # with 'coexist' so both remain valid for a transaction in the overlap.
    overlap_from = now - timedelta(hours=6)
    overlap_to = now - timedelta(hours=3)
    batch = _batch(client, auth, {
        "batch_key": "B-OVL-1",
        "reason": "batch_correction",
        "note": "重叠区间演练（coexist，不覆盖）",
        "visible_at": _iso(now - timedelta(hours=7)),
        "relations": [{
            "terminal_id": "T1004",
            "store_id": "S-M150-01",
            "merchant_id": "M150",
            "effective_from": _iso(overlap_from),
            "effective_to": _iso(overlap_to),
            "confidence": "medium",
            "mode": "coexist",
        }],
    })
    assert batch["impact"]["changed_terminals"] == ["T1004"]

    # A transaction inside the overlap resolves to two valid stores.
    txn_time = now - timedelta(hours=4)
    _ingest(client, auth, [_mktxn("TXN-OVL-1", "ACC-OVL", txn_time, 77.0, "T1004")])
    resp = _merchant_read(client, auth, "M150", _iso(now))
    term = _feature(resp, "merchant_terminal_count_7d")
    assert term["status"] == "needs_review"
    assert term["needs_review_reasons"].get("overlapping_intervals") == 1
    item = next(i for i in term["review_items"] if i["txn_id"] == "TXN-OVL-1")
    assert item["reason"] == "overlapping_intervals"
    merchants = {c["merchant_id"] for c in item["candidates"]}
    assert merchants >= {"M120", "M150"}
    # The ambiguous txn is excluded, not counted for either merchant.
    assert term["value"] == 0
    resp120 = _merchant_read(client, auth, "M120", _iso(now))
    t120 = _feature(resp120, "merchant_terminal_count_7d")
    assert t120["value"] == 0  # excluded from M120 as well, not silently chosen


def test_low_confidence_only_mapping_requires_review(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    _batch(client, auth, {
        "batch_key": "B-LOW-1",
        "reason": "batch_correction",
        "visible_at": _iso(now - timedelta(days=2)),
        "relations": [{
            "terminal_id": "T-LOW-1",
            "store_id": "S-LOW-01",
            "merchant_id": "M170",
            "effective_from": _iso(now - timedelta(days=1)),
            "confidence": "low",
        }],
    })
    _ingest(client, auth, [_mktxn("TXN-LOW-1", "ACC-LOW", now - timedelta(hours=1), 5.0, "T-LOW-1")])
    resp = _merchant_read(client, auth, "M170", _iso(now))
    term = _feature(resp, "merchant_terminal_count_7d")
    assert term["status"] == "needs_review"
    assert term["needs_review_reasons"].get("low_confidence") == 1
    assert term["value"] == 0

    # Explicit explanation surfaces the single low-confidence candidate.
    explain = client.get("/api/attribution/resolve",
                         params={"terminal_id": "T-LOW-1",
                                 "event_time": _iso(now - timedelta(hours=1))},
                         headers=auth).json()
    assert explain["resolution"]["status"] == "needs_review"
    assert explain["resolution"]["reason"] == "low_confidence"
    assert len(explain["resolution"]["candidates"]) == 1


# ---------------------------------------------------------------------------
# 5. Archived samples & scoring evidence are immutable across corrections
# ---------------------------------------------------------------------------
def test_archive_and_evidence_not_rewritten_by_master_correction(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    _ingest(client, auth, [
        _mktxn("TXN-ARC-1", "ACC-ARC", now - timedelta(hours=2), 12.0, "T1002"),
    ])
    # Training sample at a decision time where T1002 -> M110.
    decision_time = now - timedelta(hours=1)
    r = client.post("/api/training/samples", json={
        "sample_key": "S-ARC-1",
        "entity_id": "ACC-ARC",
        "decision_time": _iso(decision_time),
        "transaction_id": "TXN-ARC-1",
    }, headers=auth)
    assert r.status_code == 201, r.text
    sample_id = r.json()["id"]

    # Online merchant read with evidence captured at the same instant.
    scored = client.get("/api/online/merchant-features", params={
        "entity_id": "M110", "as_of": _iso(decision_time), "record_evidence": "true",
    }, headers=auth).json()
    evidence_id = scored["evidence"]["id"]
    ev_before = client.get(f"/api/evidence/{evidence_id}", headers=auth).json()
    payload_digest = ev_before["digest"]

    trace_before = client.get(f"/api/training/samples/{sample_id}/trace", headers=auth).json()
    archive_before = trace_before["merchant_attribution"]["archive"]
    assert archive_before["merchant_id"] == "M110"
    assert archive_before["values"]["merchant_txn_count_7d"]["value"] == 1

    # Late master correction: T1002 actually belonged to M160 since long ago,
    # visible only now (backdated business time).
    _batch(client, auth, {
        "batch_key": "B-ARC-CORR",
        "reason": "batch_correction",
        "note": "T1002 主档纠错（业务回溯 + 系统时间早于样本决策点）",
        "visible_at": _iso(now - timedelta(days=2)),
        "relations": [{
            "terminal_id": "T1002",
            "store_id": "S-M160-01",
            "merchant_id": "M160",
            "effective_from": _iso(now - timedelta(days=30)),
            "confidence": "high",
        }],
    })

    # Scoring evidence payload + digest are unchanged.
    ev_after = client.get(f"/api/evidence/{evidence_id}", headers=auth).json()
    assert ev_after["digest"] == payload_digest
    assert ev_after["payload"] == ev_before["payload"]
    assert ev_after["integrity"]["immutable"] is True

    # Training archive is unchanged.
    trace_after = client.get(f"/api/training/samples/{sample_id}/trace", headers=auth).json()
    archive_after = trace_after["merchant_attribution"]["archive"]
    assert archive_after == archive_before
    # The stability check reports the (legitimate) divergence without editing.
    stability = trace_after["merchant_attribution"]["stability"]
    assert stability["stable"] is False
    assert any(d["feature"] == "merchant_txn_count_7d" for d in stability["differences"])

    # Only a new revision was added for the affected merchant window.
    revs = client.get("/api/merchant/window-revisions", headers=auth).json()
    assert any(r["merchant_id"] == "M110" for r in revs)


# ---------------------------------------------------------------------------
# 6. Anomaly trace: relation source, effective interval, excluded candidates
# ---------------------------------------------------------------------------
def test_anomaly_score_traces_relation_source_and_candidates(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    _ingest(client, auth, [
        _mktxn("TXN-TRACE-1", "ACC-TRACE", now - timedelta(hours=1), 20.0, "T1001"),
    ])
    scored = client.get("/api/online/merchant-features", params={
        "entity_id": "M110", "as_of": _iso(now), "record_evidence": "true",
    }, headers=auth).json()
    detail = client.get(f"/api/evidence/{scored['evidence']['id']}", headers=auth).json()
    ctx = detail["payload"]["attribution_context"]
    txn = next(t for t in ctx["transactions"] if t["txn_id"] == "TXN-TRACE-1")
    att = txn["attribution"]
    assert att["status"] == "resolved"
    assert att["merchant_id"] == "M110"
    assert att["batch_key"] == "B-SEED-MASTER"
    assert att["effective_from"].startswith("2026-01-01")
    assert isinstance(att["candidates"], list) and att["candidates"]

    # Per-transaction attribution endpoint also explains both the event-time
    # view and the view at a chosen scoring instant.
    tx = client.get("/api/merchant/transactions/TXN-TRACE-1/attribution", headers=auth).json()
    assert tx["event_time_resolution"]["resolution"]["merchant_id"] == "M110"


# ---------------------------------------------------------------------------
# 7. Peak query: snapshot cache + offline/online caliber consistency
# ---------------------------------------------------------------------------
def test_peak_query_uses_snapshots_and_offline_online_agree(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    _ingest(client, auth, [
        _mktxn(f"TXN-PEAK-{i}", "ACC-PEAK", now - timedelta(minutes=30 + i), 10.0 + i, "T1005")
        for i in range(20)
    ])
    as_of = _iso(now)

    # First read materializes; repeat read at the same scoring instant hits
    # the snapshot (no rescan + attribution join).
    first = _merchant_read(client, auth, "M120", as_of)
    second = _merchant_read(client, auth, "M120", as_of)
    term_first = _feature(first, "merchant_terminal_count_7d")
    term_second = _feature(second, "merchant_terminal_count_7d")
    assert term_first["cache"] == "miss"
    assert term_second["cache"] == "hit"
    assert term_first["value"] == term_second["value"] == 1  # one distinct terminal

    # Concurrency: throws/shards hitting the same (feature, merchant, as_of).
    results, errors = [], []

    def reader():
        try:
            with SessionLocal() as db:
                resp = serving.read_merchant_features(db, "merchant_risk", "M120", now)
            results.append(_values(resp))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results
    assert all(r == results[0] for r in results)

    # Offline replay uses the same attribution version as the online snapshot.
    with SessionLocal() as db:
        feature = next(f for f in serving._set_members(
            db, serving.resolve_set_version(db, "merchant_risk", None).id
        ) if f.name == "merchant_terminal_count_7d")
        online_snap = merchant_features.get_snapshot(db, feature, "M120", now)
        offline = merchant_features.compute_merchant_window(db, feature, "M120", now)
    assert online_snap is not None
    assert offline["value"] == (online_snap.value or {}).get("value")
    assert offline["visible_batch_seq"] == online_snap.visible_batch_seq


# ---------------------------------------------------------------------------
# 8. Overlap explained at the resolver level, deterministic half-open bounds
# ---------------------------------------------------------------------------
def test_relation_interval_bounds_are_half_open(client, auth):
    edge = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    _batch(client, auth, {
        "batch_key": "B-EDGE-1",
        "reason": "batch_correction",
        "relations": [{
            "terminal_id": "T-EDGE-1",
            "store_id": "S-EDGE-01",
            "merchant_id": "M180",
            "effective_from": _iso(edge),
            "effective_to": _iso(edge + timedelta(hours=2)),
            "confidence": "high",
        }],
    })
    inside = client.get("/api/attribution/resolve", params={
        "terminal_id": "T-EDGE-1", "event_time": _iso(edge + timedelta(hours=1)),
    }, headers=auth).json()["resolution"]
    at_start = client.get("/api/attribution/resolve", params={
        "terminal_id": "T-EDGE-1", "event_time": _iso(edge),
    }, headers=auth).json()["resolution"]
    at_end = client.get("/api/attribution/resolve", params={
        "terminal_id": "T-EDGE-1", "event_time": _iso(edge + timedelta(hours=2)),
    }, headers=auth).json()["resolution"]
    assert inside["status"] == "resolved" and inside["merchant_id"] == "M180"
    assert at_start["status"] == "resolved"  # [from, ...) inclusive of start
    assert at_end["status"] == "needs_review" and at_end["reason"] == "no_relation"


# ---------------------------------------------------------------------------
# 9. Visibility: an explicit earlier scoring instant ignores the later batch
# ---------------------------------------------------------------------------
def test_scoring_instant_visibility_is_respected(client, auth):
    now = datetime.now(UTC).replace(microsecond=0)
    event = now - timedelta(hours=2)
    _ingest(client, auth, [_mktxn("TXN-VIS-1", "ACC-VIS", event, 9.0, "T1005")])
    _batch(client, auth, {
        "batch_key": "B-VIS-1",
        "reason": "terminal_migration",
        "visible_at": _iso(now - timedelta(minutes=5)),
        "relations": [{
            "terminal_id": "T1005",
            "store_id": "S-M190-01",
            "merchant_id": "M190",
            "effective_from": _iso(now - timedelta(hours=3)),
            "confidence": "high",
        }],
    })
    # Scoring at an instant before the batch was visible: T1005 was still M120.
    early = client.get("/api/attribution/resolve", params={
        "terminal_id": "T1005", "event_time": _iso(event),
        "as_of": _iso(now - timedelta(minutes=10)),
    }, headers=auth).json()
    assert early["resolution"]["merchant_id"] == "M120"

    # Scoring after the batch: the migration applies.
    late = client.get("/api/attribution/resolve", params={
        "terminal_id": "T1005", "event_time": _iso(event), "as_of": _iso(now),
    }, headers=auth).json()
    assert late["resolution"]["merchant_id"] == "M190"

    # An online read pinned before visibility still attributes to M120.
    old_read = _merchant_read(client, auth, "M120", _iso(now - timedelta(minutes=10)))
    old_term = _feature(old_read, "merchant_terminal_count_7d")
    assert old_term["value"] == 1
    assert old_term["visible_batch_seq"] < late["visible_batch_seq"]
