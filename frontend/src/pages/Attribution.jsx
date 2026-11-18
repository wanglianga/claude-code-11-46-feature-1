import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const REASONS = {
  no_relation: '无有效归属关系',
  overlapping_intervals: '区间重叠/跨门店待核对',
  low_confidence: '仅低置信映射待核对',
  no_terminal: '交易缺少终端号',
}

const fmt = (v) => (v === null || v === undefined ? '—' : typeof v === 'number' ? Number(v.toFixed(4)) : String(v))
const localToIso = (v) => (v ? new Date(v).toISOString() : '')

function Badge({ status }) {
  const cls = status === 'ok' ? 'current' : status === 'needs_review' ? 'pending' : 'down'
  return <span className={`badge ${cls}`}>{status}</span>
}

export default function Attribution() {
  const [batches, setBatches] = useState([])
  const [relations, setRelations] = useState([])
  const [revisions, setRevisions] = useState([])
  const [windowRevisions, setWindowRevisions] = useState([])
  const [evidence, setEvidence] = useState([])
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  // relation explanation
  const [explain, setExplain] = useState({
    terminal_id: 'T1001',
    event_time: '',
    as_of: '',
  })
  const [explainResult, setExplainResult] = useState(null)

  // merchant feature read
  const [read, setRead] = useState({ merchant_id: 'M110', as_of: '', record_evidence: true })
  const [readResult, setReadResult] = useState(null)

  // evidence detail
  const [evidenceDetail, setEvidenceDetail] = useState(null)

  // batch form
  const [form, setForm] = useState({
    batch_key: '',
    reason: 'terminal_migration',
    note: '',
    visible_at: '',
    terminal_id: 'T1001',
    store_id: '',
    merchant_id: '',
    effective_from: '',
    effective_to: '',
    confidence: 'high',
    mode: 'supersede',
  })

  async function reload() {
    try {
      const [b, r, rev, wr, ev] = await Promise.all([
        api.get('/api/attribution/batches?limit=50'),
        api.get('/api/attribution/relations?limit=200'),
        api.get('/api/attribution/revisions?limit=50'),
        api.get('/api/merchant/window-revisions?limit=50'),
        api.get('/api/evidence?limit=20'),
      ])
      setBatches(b)
      setRelations(r)
      setRevisions(rev)
      setWindowRevisions(wr)
      setEvidence(ev)
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => {
    reload()
  }, [])

  async function runExplain(e) {
    e && e.preventDefault()
    setError('')
    try {
      const params = new URLSearchParams({ terminal_id: explain.terminal_id, event_time: localToIso(explain.event_time) })
      if (explain.as_of) params.set('as_of', localToIso(explain.as_of))
      setExplainResult(await api.get(`/api/attribution/resolve?${params}`))
    } catch (err) {
      setError(err.message)
    }
  }

  async function runRead(e) {
    e && e.preventDefault()
    setError('')
    setMessage('')
    try {
      const params = new URLSearchParams({ entity_id: read.merchant_id })
      if (read.as_of) params.set('as_of', localToIso(read.as_of))
      if (read.record_evidence) params.set('record_evidence', 'true')
      const data = await api.get(`/api/online/merchant-features?${params}`)
      setReadResult(data)
      if (data.evidence) setMessage(`已写入评分证据 ${data.evidence.evidence_key}（不可改写）`)
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  async function submitBatch(e) {
    e.preventDefault()
    setError('')
    setMessage('')
    try {
      const body = {
        batch_key: form.batch_key,
        reason: form.reason,
        note: form.note,
        visible_at: localToIso(form.visible_at) || null,
        relations: [
          {
            terminal_id: form.terminal_id,
            store_id: form.store_id,
            merchant_id: form.merchant_id,
            effective_from: localToIso(form.effective_from),
            effective_to: localToIso(form.effective_to) || null,
            confidence: form.confidence,
            mode: form.mode,
          },
        ],
      }
      const res = await api.post('/api/attribution/batches', body)
      setMessage(
        `批次 ${res.batch_key} 已受理：影响交易 ${res.impact.affected_transaction_count} 笔，` +
          `精确重算快照 ${res.impact.affected_snapshots} 个，产生窗口修订 ${res.impact.revisions} 条`,
      )
      setForm({ ...form, batch_key: '', store_id: '', merchant_id: '', note: '' })
      await reload()
    } catch (err) {
      setError(err.message)
    }
  }

  async function showEvidence(id) {
    setError('')
    try {
      setEvidenceDetail(await api.get(`/api/evidence/${id}`))
    } catch (err) {
      setError(err.message)
    }
  }

  async function showSnapshot(id) {
    setError('')
    try {
      const snap = await api.get(`/api/merchant/snapshots/${id}`)
      setEvidenceDetail({
        evidence_key: `快照 ${id} · ${snap.feature?.name} v${snap.feature?.version}`,
        payload: {
          merchant_id: snap.merchant_id,
          as_of: snap.as_of,
          value: snap.value,
          summary: snap.summary,
          visible_batch_seq: snap.visible_batch_seq,
          revision_seq: snap.revision_seq,
          entries: Object.fromEntries(
            snap.entries.map((e) => [
              e.txn_id,
              {
                terminal_id: e.terminal_id,
                event_time: e.event_time,
                attribution_id: e.attribution_id,
                merchant_id: e.merchant_id,
                store_id: e.store_id,
                status: e.status,
                reason: e.reason,
                included: e.included,
                candidates: e.detail?.candidates,
              },
            ]),
          ),
        },
      })
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div>
      <section className="card">
        <h2>商户归属修订影响分析</h2>
        <p className="muted small">
          主档关系带“生效区间（业务时间）”与“来源批次（系统时间）”。评分使用评分时点可见的归属版本：
          历史交易不会被今天的归属改写；同时命中多条有效关系、区间重叠或仅低置信映射时返回待核对原因而不是任选一条。
          在线读取采用<strong>已展开归属快照</strong>加速，主档修订时精确找回受影响窗口并重算。
        </p>
        {message && <div className="ok">{message}</div>}
        {error && <div className="error">{error}</div>}
        <div className="stat-row">
          <div className="stat">
            <div className="stat-title">主档批次</div>
            <div>{batches.length} 个</div>
            <div className="muted small">系统时间序 seq 单调递增</div>
          </div>
          <div className="stat">
            <div className="stat-title">关系版本</div>
            <div>{relations.length} 条</div>
            <div className="muted small">历史区间只追加、不改写</div>
          </div>
          <div className="stat">
            <div className="stat-title">窗口修订</div>
            <div>{windowRevisions.length} 条</div>
            <div className="muted small">特征窗口模块</div>
          </div>
          <div className="stat">
            <div className="stat-title">评分证据</div>
            <div>{evidence.length} 条</div>
            <div className="muted small">写入后不可改写</div>
          </div>
        </div>
      </section>

      <div className="grid-2">
        <section className="card">
          <h2>提交主档修订批次</h2>
          <p className="muted small">
            supersede：按新区间截断重叠旧版本（保留历史）；coexist：不覆盖旧版本，真实重叠将呈现为待核对。
          </p>
          <form onSubmit={submitBatch} className="form-grid">
            <label>
              批次标识
              <input required value={form.batch_key} placeholder="如 B-20261118-01"
                onChange={(e) => setForm({ ...form, batch_key: e.target.value })} />
            </label>
            <label>
              原因
              <select value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })}>
                <option value="terminal_migration">终端迁店</option>
                <option value="batch_correction">批量主档纠错</option>
                <option value="store_merge">门店合并</option>
                <option value="acquirer_recoding">收单机构换码</option>
              </select>
            </label>
            <label>
              终端
              <input required value={form.terminal_id}
                onChange={(e) => setForm({ ...form, terminal_id: e.target.value })} />
            </label>
            <label>
              门店
              <input required value={form.store_id} placeholder="如 S-M110-02"
                onChange={(e) => setForm({ ...form, store_id: e.target.value })} />
            </label>
            <label>
              商户
              <input required value={form.merchant_id} placeholder="如 M110"
                onChange={(e) => setForm({ ...form, merchant_id: e.target.value })} />
            </label>
            <label>
              置信度
              <select value={form.confidence}
                onChange={(e) => setForm({ ...form, confidence: e.target.value })}>
                <option value="high">high</option>
                <option value="medium">medium</option>
                <option value="low">low（将进入待核对）</option>
              </select>
            </label>
            <label>
              生效起（业务时间）
              <input required type="datetime-local" step="1" value={form.effective_from}
                onChange={(e) => setForm({ ...form, effective_from: e.target.value })} />
            </label>
            <label>
              生效止（可空=无期限）
              <input type="datetime-local" step="1" value={form.effective_to}
                onChange={(e) => setForm({ ...form, effective_to: e.target.value })} />
            </label>
            <label>
              关系模式
              <select value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
                <option value="supersede">supersede（截断旧版本）</option>
                <option value="coexist">coexist（保留旧版本）</option>
              </select>
            </label>
            <label>
              可见时点（系统时间，可空=现在）
              <input type="datetime-local" step="1" value={form.visible_at}
                onChange={(e) => setForm({ ...form, visible_at: e.target.value })} />
            </label>
            <label className="span-2">
              备注
              <input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
            </label>
            <div className="span-2">
              <button className="btn primary">提交批次并计算影响范围</button>
            </div>
          </form>
        </section>

        <section className="card">
          <h2>归属解析（可解释）</h2>
          <form onSubmit={runExplain} className="inline-form">
            <label>
              终端
              <input value={explain.terminal_id}
                onChange={(e) => setExplain({ ...explain, terminal_id: e.target.value })} />
            </label>
            <label>
              交易事件时间
              <input required type="datetime-local" step="1" value={explain.event_time}
                onChange={(e) => setExplain({ ...explain, event_time: e.target.value })} />
            </label>
            <label>
              评分时点（可空=当前）
              <input type="datetime-local" step="1" value={explain.as_of}
                onChange={(e) => setExplain({ ...explain, as_of: e.target.value })} />
            </label>
            <button className="btn primary">解析</button>
          </form>
          {explainResult && (
            <div className="trace-box">
              <p>
                评分时点可见批次 seq <strong>{explainResult.visible_batch_seq}</strong> ·{' '}
                <Badge status={explainResult.resolution.status} />
                {explainResult.resolution.reason && (
                  <span className="muted small"> · {explainResult.resolution.reason_label || explainResult.resolution.reason}</span>
                )}
              </p>
              <p className="small">
                采用归属：
                {explainResult.resolution.merchant_id ? (
                  <>
                    <strong>{explainResult.resolution.merchant_id}</strong> /{' '}
                    {explainResult.resolution.store_id} · 区间{' '}
                    {explainResult.resolution.effective_from} ~ {explainResult.resolution.effective_to || '∞'} ·
                    来源 {explainResult.resolution.batch_key || '—'} · 置信{' '}
                    {explainResult.resolution.confidence || '—'}
                  </>
                ) : (
                  <span className="muted">未采用任何归属</span>
                )}
              </p>
              {explainResult.all_visible_relations && (
                <table>
                  <thead>
                    <tr>
                      <th>关系</th>
                      <th>门店</th>
                      <th>商户</th>
                      <th>有效区间</th>
                      <th>置信</th>
                      <th>来源批次</th>
                      <th>命中事件</th>
                    </tr>
                  </thead>
                  <tbody>
                    {explainResult.all_visible_relations.map((r) => (
                      <tr key={`${r.attribution_id}-${r.effective_from}`}>
                        <td>#{r.attribution_id}</td>
                        <td>{r.store_id}</td>
                        <td>{r.merchant_id}</td>
                        <td className="small">
                          {r.effective_from} ~ {r.effective_to || '∞'}
                        </td>
                        <td>{r.confidence}</td>
                        <td className="small">
                          {r.batch_key}（seq {r.batch_seq}）
                        </td>
                        <td>{r.interval_contains_event_time ? '是' : '否'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </section>
      </div>

      <section className="card">
        <h2>商户特征在线读取（快照加速 + 评分证据）</h2>
        <form onSubmit={runRead} className="inline-form">
          <label>
            商户
            <input value={read.merchant_id} onChange={(e) => setRead({ ...read, merchant_id: e.target.value })} />
          </label>
          <label>
            评分时点（可空=当前）
            <input type="datetime-local" step="1" value={read.as_of}
              onChange={(e) => setRead({ ...read, as_of: e.target.value })} />
          </label>
          <label style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <input type="checkbox" style={{ width: 'auto' }} checked={read.record_evidence}
              onChange={(e) => setRead({ ...read, record_evidence: e.target.checked })} />
            写入不可改写评分证据
          </label>
          <button className="btn primary">读取</button>
        </form>
        {readResult && (
          <div className="trace-box">
            <p className="small">
              特征集 {readResult.feature_set.name} v{readResult.feature_set.version} · as_of {readResult.as_of} ·
              快照缓存命中 {readResult.snapshot_cache?.hit} / 未命中 {readResult.snapshot_cache?.miss}
              {readResult.evidence && (
                <span className="muted"> · 证据 {readResult.evidence.evidence_key}</span>
              )}
            </p>
            <table>
              <thead>
                <tr>
                  <th>特征</th>
                  <th>值</th>
                  <th>状态</th>
                  <th>可见批次 seq</th>
                  <th>纳入归属</th>
                  <th>待核对</th>
                  <th>快照</th>
                </tr>
              </thead>
              <tbody>
                {readResult.features.map((f) => (
                  <tr key={f.name} className={f.status === 'needs_review' ? 'row-warn' : ''}>
                    <td>{f.name}</td>
                    <td>{fmt(f.value)}</td>
                    <td><Badge status={f.status} /></td>
                    <td>{f.visible_batch_seq ?? '—'}</td>
                    <td className="small">
                      {f.included_attributions?.length ? f.included_attributions.join(', ') : '—'}
                      {f.source_batches?.length ? <div className="muted">批次 {f.source_batches.join(', ')}</div> : null}
                    </td>
                    <td className="small">
                      {f.needs_review ? (
                        <>
                          {Object.entries(f.needs_review_reasons || {}).map(([k, n]) => (
                            <div key={k}>
                              {REASONS[k] || k} × {n}
                            </div>
                          ))}
                        </>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>
                      {f.snapshot_id ? (
                        <button className="btn ghost" onClick={() => showSnapshot(f.snapshot_id)}>
                          {f.cache === 'hit' ? '命中' : '新建'}
                        </button>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {readResult.features.some((f) => f.review_items?.length) && (
              <div>
                <h3>待核对明细（未纳入计算的候选关系）</h3>
                {readResult.features
                  .filter((f) => f.review_items?.length)
                  .slice(0, 1)
                  .map((f) => (
                    <div key={f.name}>
                      {f.review_items.map((item) => (
                        <div key={item.txn_id} className="trace-box">
                          <p className="small">
                            交易 {item.txn_id} · 终端 {item.terminal_id} · {item.event_time} ·{' '}
                            <strong>{item.reason_label || item.reason}</strong>
                          </p>
                          <table>
                            <thead>
                              <tr>
                                <th>候选关系</th>
                                <th>门店</th>
                                <th>商户</th>
                                <th>有效区间</th>
                                <th>置信</th>
                                <th>来源批次</th>
                              </tr>
                            </thead>
                            <tbody>
                              {item.candidates.map((c, i) => (
                                <tr key={i}>
                                  <td>#{c.attribution_id}</td>
                                  <td>{c.store_id}</td>
                                  <td>{c.merchant_id}</td>
                                  <td className="small">
                                    {c.effective_from} ~ {c.effective_to || '∞'}
                                  </td>
                                  <td>{c.confidence}</td>
                                  <td className="small">{c.batch_key}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ))}
                    </div>
                  ))}
              </div>
            )}
          </div>
        )}
      </section>

      <div className="grid-2">
        <section className="card">
          <h2>主档批次与影响范围</h2>
          {batches.length === 0 && <p className="muted">暂无批次</p>}
          {batches.map((b) => (
            <div className="set-version" key={b.id}>
              <div className="card-head">
                <strong>
                  {b.batch_key} <span className="muted small">seq {b.seq}</span>
                </strong>
                <span className="badge">{b.reason}</span>
              </div>
              <div className="muted small">
                可见时点 {b.visible_at} · 来源 {b.source} · {b.note || '无备注'}
              </div>
              {b.impact && (
                <div className="small">
                  影响交易 {b.impact.affected_transaction_count ?? 0} 笔 · 重算快照{' '}
                  {b.impact.affected_snapshots ?? 0} 个 · 窗口修订 {b.impact.revisions ?? 0} 条
                  {b.impact.affected_transactions?.length > 0 && (
                    <div className="muted">
                      {b.impact.affected_transactions.slice(0, 5).map((t) => (
                        <div key={t.txn_id}>
                          {t.txn_id}（{t.terminal_id}）：{t.old.merchant_id || t.old.reason || '—'} →{' '}
                          {t.new.merchant_id || t.new.reason || '—'}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </section>

        <section className="card">
          <h2>特征窗口修订审计</h2>
          <p className="muted small">
            由窗口修订模块生成，引用归属修订与批次；只新增，不改写已归档样本或评分证据。
          </p>
          {windowRevisions.length === 0 && <p className="muted">暂无窗口修订</p>}
          {windowRevisions.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>特征</th>
                  <th>商户</th>
                  <th>as_of</th>
                  <th>旧值 → 新值</th>
                  <th>批次</th>
                </tr>
              </thead>
              <tbody>
                {windowRevisions.map((r) => (
                  <tr key={r.id}>
                    <td>
                      {r.feature} <span className="muted small">v{r.feature_version}</span>
                    </td>
                    <td>{r.merchant_id}</td>
                    <td className="small">{r.as_of}</td>
                    <td>
                      {fmt(r.old_value?.value)} → <strong>{fmt(r.new_value?.value)}</strong>
                    </td>
                    <td className="small">
                      #{r.batch_id}
                      {r.attribution_revision_id ? ` / 归属修订 #${r.attribution_revision_id}` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>关系版本（双时间轴）</h2>
          <table>
            <thead>
              <tr>
                <th>终端</th>
                <th>门店</th>
                <th>商户</th>
                <th>有效区间</th>
                <th>置信</th>
                <th>系统状态</th>
                <th>来源批次</th>
              </tr>
            </thead>
            <tbody>
              {relations.slice(0, 100).map((r) => (
                <tr key={r.attribution_id}>
                  <td>{r.terminal_id}</td>
                  <td>{r.store_id}</td>
                  <td>{r.merchant_id}</td>
                  <td className="small">
                    {r.effective_from} ~ {r.effective_to || '∞'}
                  </td>
                  <td>{r.confidence}</td>
                  <td className="small">
                    {r.retired_at_batch_seq ? `被 seq ${r.retired_at_batch_seq} 截断` : r.status}
                  </td>
                  <td className="small">
                    {r.batch_key}（seq {r.batch_seq}，可见 {r.batch_visible_at}）
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <h2>归属修订日志</h2>
          {revisions.length === 0 && <p className="muted">暂无归属修订</p>}
          {revisions.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>终端</th>
                  <th>类型</th>
                  <th>旧版本</th>
                  <th>新版本</th>
                  <th>批次</th>
                </tr>
              </thead>
              <tbody>
                {revisions.map((r) => (
                  <tr key={r.id}>
                    <td>{r.terminal_id}</td>
                    <td>{r.kind}</td>
                    <td className="small">{r.old_attribution_id ?? '—'}</td>
                    <td className="small">{r.new_attribution_id ?? '—'}</td>
                    <td className="small">
                      {r.batch_key}（seq {r.batch_seq}）
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <section className="card">
        <h2>评分证据（不可改写）</h2>
        <p className="muted small">
          运营人员可从一次异常评分查到采用的关系来源、有效区间与未纳入计算的候选关系。
        </p>
        {evidence.length === 0 && <p className="muted">暂无评分证据（在商户特征读取时勾选写入）</p>}
        {evidence.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>证据</th>
                <th>实体</th>
                <th>特征集</th>
                <th>as_of</th>
                <th>可见批次 seq</th>
                <th>摘要</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((ev) => (
                <tr key={ev.id}>
                  <td className="small">{ev.evidence_key}</td>
                  <td>
                    {ev.entity_type}/{ev.entity_id}
                  </td>
                  <td>
                    {ev.feature_set.name} v{ev.feature_set.version}
                  </td>
                  <td className="small">{ev.as_of}</td>
                  <td>{ev.visible_batch_seq}</td>
                  <td className="small muted">{ev.digest.slice(0, 16)}…</td>
                  <td>
                    <button className="btn ghost" onClick={() => showEvidence(ev.id)}>
                      查看
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {evidenceDetail && (
          <div className="trace-box">
            <div className="card-head">
              <h3>{evidenceDetail.evidence_key}</h3>
              <button className="btn ghost" onClick={() => setEvidenceDetail(null)}>
                关闭
              </button>
            </div>
            {evidenceDetail.integrity && (
              <p className="small">
                不可改写校验：<strong>{evidenceDetail.integrity.immutable ? '通过' : '失败'}</strong>
                <span className="muted"> · 存储摘要 {evidenceDetail.integrity.stored_digest.slice(0, 16)}…</span>
              </p>
            )}
            <pre className="result-box">{JSON.stringify(evidenceDetail.payload, null, 2)}</pre>
          </div>
        )}
      </section>
    </div>
  )
}
