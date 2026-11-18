import React, { useState } from 'react'
import { api } from '../api.js'

function ResultBox({ result }) {
  if (!result) return null
  return (
    <pre className="result-box">{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>
  )
}

export default function Simulate() {
  const [results, setResults] = useState({})
  const [busy, setBusy] = useState('')

  async function run(key, fn) {
    setBusy(key)
    try {
      const data = await fn()
      setResults((r) => ({ ...r, [key]: data }))
    } catch (e) {
      setResults((r) => ({ ...r, [key]: `错误：${e.message}` }))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="grid-2">
      <section className="card">
        <h2>本地数据模拟</h2>
        <p className="muted small">
          生成交易流（含重复回调、迟到事件、跨日分布）、账户日快照与延迟确认的拒付/核查标签。
        </p>
        <div className="action-list">
          <button
            className="btn"
            disabled={!!busy}
            onClick={() => run('accounts', () => api.post('/api/simulate/accounts', { accounts: 8, days: 7, seed: 42 }))}
          >
            1. 生成账户日快照（8 户 × 7 天）
          </button>
          <ResultBox result={results.accounts} />
          <button
            className="btn"
            disabled={!!busy}
            onClick={() =>
              run('transactions', () =>
                api.post('/api/simulate/transactions', { count: 60, span_hours: 48, duplicate_rate: 0.15, late_rate: 0.15, seed: 42 }),
              )
            }
          >
            2. 生成交易流（60 笔，含重复回调与迟到事件）
          </button>
          <ResultBox result={results.transactions} />
          <button
            className="btn"
            disabled={!!busy}
            onClick={() => run('labels', () => api.post('/api/simulate/labels', { fraud_rate: 0.12, seed: 42 }))}
          >
            3. 生成延迟拒付/核查标签
          </button>
          <ResultBox result={results.labels} />
          <button
            className="btn"
            disabled={!!busy}
            onClick={() => run('master', () => api.post('/api/simulate/merchant-master', { seed: 42 }))}
          >
            4. 生成商户主档（终端 M110/M120 扩展）
          </button>
          <ResultBox result={results.master} />
          <button
            className="btn"
            disabled={!!busy}
            onClick={() =>
              run('merchantTxns', () =>
                api.post('/api/simulate/merchant-transactions', { count: 60, span_hours: 168, seed: 42 }),
              )
            }
          >
            5. 生成商户终端交易流（60 笔 × 7 天）
          </button>
          <ResultBox result={results.merchantTxns} />
        </div>
      </section>

      <section className="card">
        <h2>上游故障演练</h2>
        <p className="muted small">
          模拟账户资料源不可用：在线接口将返回来源状态与最近成功时间，缺失值不会伪装成 0。
        </p>
        <div className="action-list">
          <button
            className="btn warn"
            disabled={!!busy}
            onClick={() =>
              run('outage', () =>
                api.post('/api/sources/outage', { source: 'account_profiles', status: 'unavailable', message: '上游账户日更延迟（演练）' }),
              )
            }
          >
            模拟账户源中断
          </button>
          <button
            className="btn"
            disabled={!!busy}
            onClick={() =>
              run('recover', () =>
                api.post('/api/simulate/accounts', { accounts: 8, days: 7, seed: 42 }),
              )
            }
          >
            恢复（重新推送账户快照）
          </button>
          <ResultBox result={results.outage || results.recover} />
        </div>
        <div className="hint">
          <h3>建议验证路径</h3>
          <ol>
            <li>按 1→2→3 生成数据，到「计算状态」查看水位、迟到与修订。</li>
            <li>到「在线查询」读取 ACC001 当前特征值。</li>
            <li>到「训练样本」以某笔交易时间创建样本并「追溯」，对比同一 as_of 的在线值。</li>
            <li>点击「模拟账户源中断」后再次在线查询：账户类特征显示 unavailable 而非 0。</li>
            <li>到「特征目录」勾选特征发布新版本，发布期间在线查询始终读取完整单一版本。</li>
            <li>按 4→5 生成商户主档与终端交易，到「归属修订」读取 M110 的「近七日同商户终端数」。</li>
            <li>在「归属修订」提交一个终端迁店批次（supersede），观察影响交易数、精确重算快照数与窗口修订。</li>
            <li>提交 coexist 重叠区间或 low 置信关系，确认在线读取返回 needs_review 与候选关系而非任选一条。</li>
          </ol>
        </div>
      </section>
    </div>
  )
}
