# 反欺诈特征计算与在线服务平台

面向网络支付机构的特征平台（Feature Store）：风控分析师在特征目录中登记交易行为特征，
**离线训练集与实时评分读取同一计算口径**。平台持续维护流式窗口结果，迟到事件只触发受影响
账户与窗口的修订，用同一特征定义重放历史输入生成训练值。

在此基础上提供**商户归属修订影响分析**：终端—门店—商户关系带生效区间、来源批次与置信状态，
“近七日同商户终端数”等商户层级特征在每笔交易上使用**评分时点可见的归属版本**；主档晚到修订
只形成受影响范围与新的特征窗口修订，不改写已归档训练样本或既有评分证据。

技术栈：Python 3.12 · FastAPI · PostgreSQL · React (Vite)

## 核心设计

### 口径一致性如何保证

- **同一个计算函数**：`backend/app/services/compute.py` 中的 `compute_feature_value` 同时服务
  于在线查询、检查点物化与训练重放，离线/在线不存在两套实现。
- **流式窗口状态**：计算作业为每个（特征版本 × 账户）维护窗口贡献状态 `window_entries`
  （已去重、按事件时间索引、只含该特征过滤后的输入），在线读取是对该状态的索引化区间聚合，
  不在请求时扫描原始交易表；事件边界处同时物化检查点 `checkpoints`。
- **窗口语义**：`(as_of − W, as_of]` 左开右闭，全程事件时间（timestamptz），无日历日截断，
  跨日窗口天然正确。窗口归属是事件时间的纯函数，与到达顺序无关；相同事件时间的事件按
  稳定键 `(event_time, event_id)` 处理，检查点物化顺序确定。
- **去重**：`(source, event_id)` 唯一约束。重复交易回调被确认但不重复应用，窗口计数不变，
  重复次数计入 `duplicate_attempts` 并在计算状态页可见。
- **迟到修订**：事件时间落后于该（特征 × 账户）水位即为迟到。迟到事件只重算
  `[event_time, event_time + W)` 范围内的检查点，且每次值变更写入 `revisions` 审计
  （旧值 → 新值、触发事件、检查点 as_of），其他账户与其他窗口不受影响。
- **版本钉住**：发布 = 新建不可变的特征集版本 + 单行原子切换在线指针。每个在线请求只在
  开始时解析一次版本，因此发布中途的请求要么读到完整旧版本、要么读到完整新版本，
  绝不会把两个版本的字段拼成一条评分输入。也可显式 `set_version=` 钉住历史版本。
- **缺失不伪装**：上游账户源不可用（或某账户无快照）时，账户类特征返回
  `status=unavailable, value=null` 并附来源状态与最近成功时间，绝不返回 0。
  （空窗口的 COUNT/SUM 返回 0 是真实度量，与缺失不同；空窗口 AVG/MIN/MAX 返回 null。）
- **训练追溯**：训练样本记录决策时点；追溯接口按同口径重放特征值，并列出每个值由哪些
  输入记录（交易/标签/账户快照）算出。拒付与人工核查标签延迟到达后，样本标签自动
  由 pending 变为 confirmed，标签类窗口特征同步修订。

### 商户归属修订（双时间轴）

- **业务时间 × 系统时间**：归属关系 `merchant_attributions` 的生效区间为
  `[effective_from, effective_to)`（业务时间）；`attribution_batches` 以单调 `seq` 与
  `visible_at` 记录“哪个主档批次从何时起对评分可见”（系统时间）。解析一笔交易需要同时满足
  “生效区间包含事件时间”和“评分时点可见包含该版本”。
- **历史不被改写**：主档修订只追加版本。`supersede` 通过**新增**截断版本（前缀/尾段）并给旧行
  记 `retired_at_seq` 来结束区间，旧行对更低可见 seq 的评分时点仍然可见；`coexist` 则保留旧版本，
  真实重叠会显式暴露。任何评分证据与样本归档都不在修订路径中被写入。
- **不任选归属**：一笔交易同时命中两条有效关系（跨门店/跨商户或区间重叠）→
  `needs_review / overlapping_intervals`；只有低置信映射 → `needs_review / low_confidence`；
  终端在可见主档中无关系 → `needs_review / no_relation`。返回值始终列出**全部候选关系**
  及每条的来源批次、有效区间、置信度，命中冲突的交易不计入任何商户的取值。
- **明确采用哪种取舍**：商户特征选择**已展开归属快照**（`merchant_window_snapshots` +
  `merchant_snapshot_entries`）——每个已评分的（特征 × 商户 × 评分时点）保存展开后的逐笔归属；
  高峰重复读取是指索引命中而不是重扫交易再关联关系。代价落在主档修订侧：批次提交时必须
  **精确找回**含有“受影响终端 + 处于改动区间内”交易的已物化窗口并重算，产生
  `merchant_window_revisions`；未物化的窗口按各自 `as_of` 的可见性按需计算，历史读取依然正确。
- **职责分离、版本引用**：归属模块（关系/批次/影响范围）、特征窗口模块（窗口值与展开快照、
  窗口修订）、评分证据模块（不可改写证据与摘要校验）、训练样本解释模块（归档决策时点归属视图
  与稳定性检查）各自独立，通过 `attribution_id` / `batch_id` / `revision_id` 引用关联。
- **离线/在线同口径**：`merchant_features.compute_merchant_window` 同时服务于在线快照物化与
  离线重放，且整个评分响应只解析一次可见批次 seq，不会把两个系统时间视图拼进一条输入。

### 数据流

```
交易回调 / 拒付标签 / 账户日快照
        │  POST /api/ingest/*（幂等去重）
        ▼
processed_events（去重日志）──► window_entries（流式窗口状态）
                                  │        ▲ 迟到事件只修订受影响范围
                                  ▼        │
                            checkpoints（事件时点物化值）──► revisions（修订审计）
                                  │
        在线评分 ◄────────────────┘  训练重放 / 数据集构建（同一计算函数）
```

## 目录结构

```
backend/            FastAPI 后端
  app/models.py       数据模型（目录/事件/窗口状态/检查点/修订/样本/归属/快照/证据）
  app/services/       compute(窗口引擎) ingestion serving training features simulation
                      attribution(归属) merchant_features(商户窗口/快照) evidence(证据/归档)
  app/routers/        REST API（含 attribution / merchant / evidence）
  tests/              验收测试（pytest，21 项：11 项基础 + 10 项归属修订）
frontend/           React 前端（特征目录/计算状态/在线查询/训练样本/归属修订/数据模拟）
scripts/verify_e2e.py  端到端验收脚本（针对运行中的服务，32 项检查）
scripts/dev_start.sh   本地一键启动（后端 + 前端 dev server）
docker-compose.yml  一键启动（db + backend + frontend）
```

## 本地启动

前置：PostgreSQL（127.0.0.1:5432，库/用户/密码均为 `ccdev`）、Python 3.12、Node 22。

```bash
# 后端（首次自动建表并播种：测试账号 + 默认特征集 fraud_scoring v1）
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端（新终端，dev server 代理 /api 到 8000）
cd frontend
npm install
npm run dev        # http://localhost:5173
```

也可以 `bash scripts/dev_start.sh` 一次拉起两者。API 文档：http://127.0.0.1:8000/docs

## Docker 一键启动

```bash
docker compose up --build
# 前端 http://localhost:8080 ，后端 http://localhost:8000/docs
```

compose 使用独立的 `featuredb/featureapp` 数据库账号（非开发容器账号），首次启动自动建表播种。

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| `analyst` | `analyst123` | 风控分析师 |
| `admin` | `admin123` | 管理员 |

## 验证方式

### 1. 单元/验收测试（pytest，21 项）

```bash
cd backend && .venv/bin/python -m pytest tests -v
```

`tests/test_acceptance.py`（11 项）：重复回调去重、相同事件时间稳定键与半开窗口边界、跨日窗口、
迟到修订范围（仅受影响账户与窗口 + 审计值）、延迟拒付标签、发布中途不混版本（多线程并发）、
离线/在线同口径（含迟到后再验证）、上游缺失不伪装零值、样本输入记录追溯、鉴权。

`tests/test_attribution.py`（10 项）：种子主档与“近七日同商户终端数”、终端迁店后历史不变且
只产生受影响范围与窗口修订、批量主档纠错影响范围精确且其他商户无修订、相邻/跨门店区间重叠
返回待核对与全部候选、仅低置信映射返回待核对、归档样本与评分证据在主档纠错后字节不变、
异常评分可追溯关系来源/有效区间/未纳入候选、高峰并发读快照缓存与离线在线同口径、
半开生效区间边界、评分时点系统时间可见性。

### 2. 端到端验收脚本（32 项检查，需后端已启动）

```bash
/opt/python-3.12/bin/python3 scripts/verify_e2e.py
```

前 15 项覆盖既有流程（重复回调 → 跨日窗口 → 迟到修订范围 → 延迟拒付 → 发布中途版本完整 →
离线样本与在线读取一致 → 账户源中断返回状态而非零值）；后 17 项覆盖商户归属修订：
终端迁店影响范围仅含迁移后交易、精确重算受影响已展开窗口、主档修订产生窗口修订审计、
评分证据摘要与载荷不被改写、归档训练样本归属视图不变、稳定性检查报告差异但不改写、
异常评分可追溯关系来源/有效区间/候选、区间重叠与低置信返回待核对、高峰重复查询命中快照、
离线/在线使用同一归属版本。

### 3. 页面手工验证

1. 「数据模拟」按 1→2→3 生成账户快照、交易流（含重复回调与迟到事件）、延迟标签。
2. 「计算状态」查看水位、迟到计数、重复回调数与修订日志（旧值→新值）。
3. 「在线查询」读取某账户特征；切换 as_of 观察窗口变化。
4. 「训练样本」以某笔交易时间创建样本并点「追溯」，与同一 as_of 的在线值逐项对比。
5. 「数据模拟 → 模拟账户源中断」后再次在线查询：账户类特征显示 unavailable 而非 0。
6. 「特征目录」勾选特征发布新版本，发布期间在线查询始终读取完整单一版本。
7. 「数据模拟」按 4→5 生成商户主档与终端交易流；到「归属修订」读取 M110
   （`merchant_terminal_count_7d` 即“近七日同商户终端数”），观察快照命中与纳入的归属版本。
8. 「归属修订」提交一个终端迁店批次（supersede）：查看影响交易数、精确重算快照数与窗口修订；
   再提交 coexist 重叠区间或 low 置信关系，确认读取返回 `needs_review` 与候选关系而非任选一条。
9. 「归属修订」勾选“写入不可改写评分证据”后读取商户特征，查看证据摘要校验与归属上下文；
   到「训练样本」追溯样本可看到决策时点归档的商户归属视图与稳定性结论。

## 实际执行的验证结论

- `pytest tests`：21 项全部通过（测试库 `ccdev_test`，每用例重建 schema）。
- `scripts/verify_e2e.py`：32 项检查全部 PASS（针对本地运行的后端 + `ccdev` 库）。
- 前端 `npm run build` 构建通过（`dist/` 产物生成）；vite dev server 页面与 `/api` 代理实测可用。
- 后端 uvicorn 启动后，模拟数据（账户/交易/标签/商户主档/商户终端交易）、在线查询、
  修订日志、计算状态、样本追溯、数据集构建、归属解析、商户特征读取、评分证据等接口均经 curl 实测。
- 快照加速实测：同一评分时点首次读取 4 个特征 74ms（miss），随后读取 15ms（hit）；
  8 并发读同一 `(商户, as_of)` 得到一致取值且不发生快照主键冲突。
- 当前容器无 docker 命令，`docker-compose.yml` 仅做了 YAML 静态校验，未实际构建镜像。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/auth/login | 登录获取 token |
| GET/POST | /api/features | 特征目录查询 / 新建特征（版本） |
| GET | /api/feature-sets | 特征集版本与当前指针 |
| POST | /api/feature-sets/{name}/publish | 原子发布新版本 |
| POST | /api/ingest/transactions | 交易回调（幂等去重、迟到检测） |
| POST | /api/ingest/labels | 拒付/核查结果（延迟到达） |
| POST | /api/ingest/account-profiles | 账户日快照 |
| GET | /api/online/features | 在线特征查询（钉住完整版本） |
| GET | /api/compute/status、/watermarks、/revisions | 计算状态与修订审计 |
| POST/GET | /api/training/samples | 训练样本 |
| GET | /api/training/samples/{id}/trace | 样本追溯（决策时点/特征值/输入记录） |
| POST/GET | /api/training/datasets | 训练数据集构建与读取 |
| POST | /api/simulate/* | 本地模拟数据与故障演练（含 merchant-master / merchant-transactions） |
| GET | /api/sources | 上游来源状态 |
| POST/GET | /api/attribution/batches | 提交主档批次（含精确影响范围）/ 列表 |
| GET | /api/attribution/relations | 关系版本（含生效区间、来源批次、截断状态） |
| GET | /api/attribution/revisions | 归属修订日志 |
| GET | /api/attribution/resolve | 终端×事件时间×评分时点 的归属解析与全部候选 |
| GET | /api/online/merchant-features | 商户特征读取（展开快照、待核对原因、可选写证据） |
| GET | /api/merchant/snapshots/{id} | 已展开快照明细（逐笔归属与候选） |
| GET | /api/merchant/window-revisions | 特征窗口修订审计 |
| GET | /api/merchant/transactions/{txn_id}/attribution | 单笔交易归属解释（事件时点/评分时点） |
| GET | /api/evidence、/api/evidence/{id} | 评分证据列表与不可改写校验 |
