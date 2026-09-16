# 策略编译与执行平台（policy-platform）

策略编辑、编译校验、运行时查询、**逐步调试**、**资源台账与周期限额**五个
**可独立部署**的服务。策略由可复用片段（fragment）
组成，发布前解析依赖图、检测循环，生成**带版本的不可变产物**；运行时按请求上下文
选版执行，失败按安全回退规则降级，全程可审计。维护者可对一次输入创建**可暂停、
可恢复、可分叉**的逐步调试会话。每次规则判定先占用余额、按真实消耗销账，
批次封账后形成**只读账页**，晚到凭证经财务确认另记补账/冲账。

## 架构

```
            ┌────────────┐   HTTP    ┌─────────────┐
  编辑/发布  │  editor    │──────────▶│  compiler   │
  ─────────▶│  :8001     │           │  :8002      │
            └─────┬──────┘           └──────┬──────┘
                  │                         │ 写产物/审计
                  ┌────────────┴─────────────────────────▼────────────┐
                  │            PostgreSQL                 │
                  │  fragments / policies / artifacts     │
                  │  policy_deps / audit_events / decisions│
                  │  debug_sessions / debug_branches /    │
                  │  debug_frames / debug_events / ...     │
                  └────────────▲─────────────────────────┘
                               │ 读产物（启动预热 + 缓存）   │ 固定版本快照
            ┌──────────────────┴───┐              ┌────────┴────────┐
  查询 ────▶│  runtime   :8003     │   调试 ─────▶│  debugger :8004 │
            └──────────────────────┘              └─────────────────┘
```

- **editor**：片段/策略 CRUD、发布入口、依赖链查询、**变更提案与评审工作流**。片段更新后调用 compiler 做**增量重编译**。
- **compiler**：依赖图解析、循环检测、拓扑排序、生成不可变版本产物；撤销版本。
- **runtime**：按 `min_version` 选版执行；节点未加载/超时按回退规则降级；记录决策日志。
- **debugger**：针对一次输入的**逐步调试会话**：固定产物版本 + 脱敏输入、断点、租约、分叉与逐节点比较。
- **quota**：资源消耗台账与周期限额；凭证采集、前置余额门禁、批次核算三个组件可分开启动。
- 五个服务无共享内存状态，各自独立扩缩容；共享存储只有数据库。

## 快速开始

```bash
docker compose up --build        # db + compiler + editor + runtime + debugger
./scripts/demo.sh                # 端到端演示（需要 jq）
```

本地开发（SQLite，无需 Docker）：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/                       # 115 个测试
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.compiler.main:app --port 8002 &
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.editor.main:app --port 8001 &
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.runtime.main:app --port 8003 &
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.debugger.main:app --port 8004 &
# 资源台账（:8005）：三个组件可分开启动 —— QUOTA_ROLE=collector / gate / accountant
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.quota.main:app --port 8005 &
```

## 核心机制

### 片段与策略

- **片段**：可复用的表达式（JSON DSL），可 `{"ref": "其他片段"}` 互相引用；每次更新 `version+1` 并记录内容哈希。
- **策略**：`name + entry_fragment`，编译时从入口片段出发解析整个依赖闭包。

### 编译（发布）

1. 从入口片段 DFS 解析依赖图：**循环检测**（报出完整环路径）、缺失引用检测、DSL 静态校验；
2. 拓扑排序后把表达式编译为**节点图**（引用内联为节点 id），连同依赖链（片段名+版本+哈希）
   计算产物内容哈希；
3. **不可变产物**：`(policy, version)` 与 `(policy, hash)` 双唯一约束；内容哈希相同 →
   `duplicate`，不产生新版本（重复发布幂等）；
4. **并发安全**：Postgres 下对策略行 `SELECT ... FOR UPDATE` 串行化版本分配；唯一约束兜底，
   冲突重试；SQLite 下靠唯一约束 + 重试；
5. **失败不覆盖**：编译任何一步失败只写 `PUBLISH_FAILED` 审计，已有产物原样保留；
6. **增量编译**：`policy_deps` 反向索引记录"策略 → 依赖闭包"，片段更新只重编译受影响策略。

### 运行时版本选择与回退

候选版本 = 未撤销且 `version >= min_version`，从高到低尝试：

| 情况 | 行为 |
|---|---|
| 节点未完全加载（如滚动升级时 runtime 不认识新算子） | 回退到下一候选版本，响应 `fallback.reason=nodes_not_loaded` |
| 执行超时（请求级 deadline，默认 200ms，上限 5s） | 回退到下一候选版本，响应 `fallback.reason=timeout` |
| 确定性错误（缺输入、除零等 DSL 错误） | **不回退**，直接 422（换版本结果相同） |
| 所有 ≥ min_version 候选失败，`strict_min_version=false` | 安全网：回退到低于 min_version 的最近可用版本，响应标记 `below_min_version=true` |
| `strict_min_version=true` 且无满足版本 | 409 `no_available_version` |
| 全部失败 | 503 + 每个版本的失败原因；决策日志照常记录 |

响应始终包含 `used_version` / `requested_min_version` / `fallback` / `decision_id`，
**用了哪一版、为什么回退**一目了然。已撤销版本不参与选择。

> 演示"节点未加载"：算子 `approx_match` 只在编译器注册表中（模拟滚动升级时
> compiler 领先 runtime 的版本偏斜），含该算子的产物在 runtime 加载校验失败，触发回退。

### 审计与可观测

- **审计事件**（`audit_events`，追加式）：`SERVICE_STARTED`（重启留痕）、`FRAGMENT_CREATED/UPDATED`、
  `POLICY_CREATED`、`PUBLISH_REQUESTED/SUCCEEDED/FAILED/DUPLICATE`、`VERSION_REVOKED`。
  三个服务都挂 `GET /audit`。
- **决策日志**（`GET /decisions`）：每次查询记录输入摘要（键列表 + 规范 JSON 的 SHA-256 +
  截断预览）、使用版本、回退原因、依赖链快照、耗时。
- **依赖链**（`GET /policies/{name}/chain`）：实时片段图（节点/边/环/缺失引用/求值顺序）
  + 最新产物固化的依赖链。

### 变更管控：提案 → 评审 →（立即/预约）生效

策略变更不再直接进入运行环境，必须走提案。核心在 `proposal_service.py` + `semdiff.py`。

**提交即固化（`POST /proposals`）**
- 每个变更项（片段名 + 新正文）在提交时固定：基线版本/哈希/正文、新正文哈希、**逐项语义差异**；
- 固定**影响范围**：受影响策略、整个依赖闭包内片段的版本/哈希、各策略当前产物版本、
  本次生效所需的审批角色规则（全局默认 ∪ 策略专属，同角色取最大人数）；
- 提交时用"叠加后的内存片段视图"**预演编译**：环/缺失引用/DSL 错误、无实质变化、
  变更不触达任何策略都会在提交阶段拒绝（不会等到生效才失败）。
- `GET /proposals/{id}/diff` 查看逐项差异：按 JSON 路径（`$.args[1][2]`）给出
  added/removed/literal_changed/operator_changed/variable_changed/reference_changed/
  structure_changed，并汇总引用片段、输入变量、算子集合的增减。

**评审规则**
- **发起人不能审批自己的提案**（403 `self_approval_forbidden`）；
- 必须满足提案固定的"角色 × 不同人数"（`PUT /proposals/approval-config-default`
  或 `/approval-config/{policy}`），同一人多角色/重复同意只算一票；
- **重复评审幂等**：同一评审人再次提交返回 `already_reviewed`，不产生第二条意见、事件、审计；
- 任一评审人拒绝 → `REJECTED`（终态）；发起人可 `withdraw`；
- 超过 `expires_at` 仍未满足人数 → 调度周期或再次评审时标记 `EXPIRED`。

**生效与冲突**
- 满足人数时：无预约（或预约已到点）→ 同事务内立即 `EFFECTIVE`；
  预约时间未到 → `SCHEDULED`，由 editor 后台调度器到点生效；
- 同策略多个提案按 **`(scheduled_at, id)` 确定顺序**生效；排在前面的先生成新产物、
  移动基线，**较晚提案执行前逐项核对固定基线，发现片段/产物版本已变即停止**，
  标记 `CONFLICT` 并在 `conflict_reason` 给出漂移项（片段/策略、固定版本 vs 当前版本）；
- 评审期间依赖片段在提案之外再次变化（直接 `PUT /fragments`、直接 publish）同样立即标冲突；
  **冲突只改提案状态与时间线，已给出的评审意见原样保留**；
- 拒绝/撤回/过期/冲突的提案都不会生效；
- **重复执行幂等**：调度器用 `SCHEDULED→APPLYING` 原子认领 + 状态守卫，重复触发对已生效提案
  返回 `already_effective`，不产生新版本/新事件/新审计。

**时间线查询（`GET /proposals/{id}/timeline`）**：提交、每位评审人的决定（`/reviews`）、
当时固定的逐项差异、拒绝/撤回/过期/冲突、以及最终进入运行环境的产物
（`runtime_artifacts: [{policy, version, hash}]`）。

**重启续处理**：评审意见、预约时间、提案状态全部在数据库；调度器只是"到点触发"。
editor 重启后：复位崩溃残留的 `APPLYING`，立即跑一个周期（宕机期间到点的立即生效），
未到点的预约继续等到点，等待中的评审继续可处理。调度周期由 `SCHEDULER_INTERVAL_MS` 控制。

### 逐步调试（debugger :8004）

让策略维护者针对**一次输入**创建可暂停、可恢复、可分叉的逐步调试会话。调试器只读
产物、不参与运行时选版与回退；所有状态都在数据库，可独立扩缩容、随时重启。

**会话创建即固化（`POST /debug/sessions`）**
- **固定产物版本**：默认取最新未撤销版本，也可 `version` 指定；创建时把节点图
  `nodes/topo/entry/dep_chain/hash` **整体快照**进会话。之后即使产物被撤销或发布了
  新版本，调试始终走快照；会话状态里的 `artifact.version_status` 实时标注
  `active / outdated / revoked / missing` 与 `newer_versions`、撤销原因。
- **输入脱敏后落库**：递归按敏感键名（`password/token/secret/email/phone/...`，
  可用 `secret_keys` 追加）把值替换为 `***REDACTED***`；原始输入**不落库**，
  只保留键名 + SHA-256 + 大小（`input_redaction.original_input_summary`），
  并报告每个被掩码的 JSON 路径。
- 创建者即第一任租约持有人，返回一次性 `lease.token`（仅创建/接管响应里出现）。

**按实际求值顺序推进**
- `POST .../step`：执行**恰好一个**拓扑节点后停下（同步）。
- `POST .../continue`：进入 `running`，后台线程推进到**下一断点 / 暂停请求 /
  错误 / 完成**（返回 202，轮询分支状态）。每个节点提交一次，崩溃只丢当前节点。
- 任意停止点的分支视图含：`current`（节点定义、执行前入参 `args_in`）、
  `remaining_path`（剩余 topo 节点）、每个已执行节点的 `frames`（入参/结果/耗时）。

**断点**（创建时给定或 `PUT .../breakpoints` 整体替换，可按分支不同）
- `{"type":"node","node":"risk_base#10"}`：按节点 id；
- `{"type":"op","op":"div"}`：按算子类型；
- `{"type":"condition","expr":{...DSL...},"node":null}`：DSL 布尔表达式，对**输入**
  求值（不允许 `ref`）；可选 `node` 限定只在该节点前判断，否则每个节点执行前都判断。
- 断点在节点**执行前**命中；非法节点/算子/表达式在设置时即 422。

**确定性错误停成错误帧**：缺输入（`missing_input`）、除零等 DSL 错误、
`op not loaded`（运行时缺该算子）都在出错节点形成 `error frame`：分支转 `error`、
游标停在该节点（`remaining_path` 含它）、记录错误类型与消息，**会话不消失**；
此后只能从该点 `fork`（改输入重试）或结束会话。

**分叉（`POST .../fork`）**
- 只能从停止点（暂停/错误/完成）分叉；`input_patch` 是顶层输入覆盖（键值覆盖、
  `null` 删除键），新增值同样脱敏；子分支继承断点、从节点 0 **用新输入重放**。
- **父分支历史永不改写**：`debug_frames` 对 `(session, branch, index)` 唯一、只追加。
- `GET .../compare?a=main&b=<child>`：按 topo 下标逐节点对齐比较中间结果与错误，
  给出最近共同祖先和**第一处分歧**（`result` / `error` / `execution_boundary`）。

**租约：同一时刻只有持有人能推进**
- 所有写操作要求 `actor + token` 且租约未到期；其他人（token/持有人不符）只能 GET，
  写操作返回 409 `not_lease_holder`。
- `POST .../lease` 续租（token 不变）；租约到期后任何人可
  `POST .../lease/takeover` 接管（换发新 token）。
- **接管后旧持有人的命令一律拒绝**（其旧 token 不再匹配）；正在 `continue` 的后台
  线程在下一**节点边界**检测到 token 变化/到期即停（时间线 `reason=lease_lost`），
  不会多执行节点；新持有人随后可从该停止点继续。
- 默认 TTL 60s（`DEBUG_LEASE_TTL_S`，上限 `DEBUG_LEASE_MAX_S`）。

**幂等与防乱序**
- 变更命令可带 `cmd_id`：同一会话内相同 `cmd_id+actor+command` **回放首次响应**，
  重复命令**不会多推进一步**（返回体带 `replayed:true`）；cmd_id 被不同命令复用 →
  409 `cmd_id_conflict`。
- `step/fork` 带分支级单调 `seq`；乱序/重放旧序号 → 409 `unexpected_seq`，
  响应始终带 `expected_seq`。

**长暂停与重启**：分支位置、帧、租约、断点、输入全部在库。长时间暂停后继续仍从原
节点走；debugger 重启时把「所属进程心跳已死」的残留 `running` 分支复位为 `paused`
（基于 `debug_epochs` 心跳，**不会**误复位同库其它存活实例正在推进的分支），事件
`reason=service_restart`。

**完整时间线（`GET .../timeline`）**：追加式 `debug_events` 记录每次
创建 / 推进（advanced，含节点结果与耗时）/ 暂停（breakpoint/manual/lease_lost/
service_restart）/ 接管（lease_taken_over）/ 续租 / 分叉（forked，含 changed_keys）/
错误 / 完成 / 结束，并附各分支全部节点的当时输入、结果、错误帧；配合 `compare`
定位分支间第一处分歧。

## 资源消耗台账与周期限额（quota :8005）

为每次规则判定建立资源消耗台账与周期限额。判定生命周期（同一业务流水 `serial`
贯穿）：**占用（判定开始，预估量先占余额）→ 凭证（判定结束真实消耗）→
销账（差额自动补退）**；异常退出或超时则**归还占用**。

```
业务端                collector 采集            gate 前置门禁            accountant 核算
POST /quota/holds  ──▶ 入站事件箱(去重/乱序) ──▶ 锁限额→判余额→写占用 ──▶ 配对凭证→销账
POST /quota/vouchers──▶（同一事件箱，可先到）                          ──▶ 封账→只读账页
POST /quota/aborts ──▶                      ──▶ 归还 / 超时回收        ──▶ 晚到挂起→财务补/冲账
```

**作用域（共用/分别设限）**：规则注册为 `mode=shared` 时并入账户共享池
（`scope_rule=""`，多条规则共用一份限额）；`mode=dedicated` 时按规则名独立设限。

- **重投消除**：采集箱 `(serial, event_type)` 唯一，重投返回 `duplicate:true`，
  绝不重复入账；同流水同类型但内容不一致报 `serial_conflict`。
- **乱序接纳**：凭证先于占用到达时凭证置 `PENDING`，后续核算周期自动配对；
  放弃先到则退回队列等待占用。事件认领用 NEW→PROCESSING 的 CAS 互斥，
  崩溃残留 PROCESSING 在启动时退回 NEW（业务写入幂等，重放安全）。
- **时区批次**：批次 = 发生时刻在**账户时区**下的日期；占用记开始日，销账记
  凭证发生日（跨周期销账打 `cross_period`），**归还永远回到占用产生日**
  （跨周期未结束的占用即使两个周期后才超时回收，也回到产生它的批次）。
- **并发不超卖**：占用在一个写事务里「锁限额版本行（PG `SELECT … FOR UPDATE`，
  SQLite `BEGIN IMMEDIATE` 写锁）→ 读占用/已销账 → 判定 → 写占用」；
  40 个并发门禁实例抢 100 额度的压测中恰好 10 笔通过、30 笔拒绝，总量不破线。
- **销账唯一**：`quota_settlements.hold_serial` 唯一 + 占用行 HELD→终态 CAS，
  同一流水多次销账只生效一次；超时回收与销账竞争时只有一方赢（落败方按
  held=0 全额补记一次，不重复）。
- **只读账页**：封账把当时账目整体快照成 `quota_pages + quota_page_lines`；
  账页、账页行、销账、归还、调整分录五张表在**数据库层**由触发器拒绝
  UPDATE/DELETE（SQLite trigger / Postgres trigger function）。封账幂等。
- **晚到凭证**：封账后到达的凭证（含封账时仍 PENDING 的）一律置 `SUSPENDED`
  并生成 PENDING 调整单；财务 `confirm` 后追加**补账（+）/冲账（−）分录**，
  冲账后调整净额不得为负，**原账页永不改写**（调整在账页 `post_seal_appendix`
  可追溯）；财务也可 `reject` 驳回。
- **限额变更**：`POST /quota/limits` 带 `effective_from`（选定批次，含当日）；
  版本号按生效日单调，已封账批次不允许选为生效日（不重算历史账页）。
- **分开启动 / 中断续处理**：`QUOTA_ROLE=collector|gate|accountant`（逗号组合，
  默认 all）。collector 另扫 `QUOTA_SPOOL_DIR` 文件台（incoming→processing→done，
  坏文件落 `.bad:<code>` 不毒化周期）；gate 周期处理占用/放弃并 `reap_expired`
  回收超时占用；accountant 周期配对销账、重试乱序凭证、把每个账户「本地日已过」
  的 OPEN 批次自动封账。所有进度在数据库。
- **可解释**：`GET /quota/usage/{account}` 逐项给出限额版本、占用量（含流水）、
  已销账量（含流水）、已入账/待处理调整与可花余额；
  `GET /quota/trace/{serial}` 沿唯一流水串起 采集→占用→凭证/销账→归还→
  账页→调整 的完整时间线。

## DSL 参考

```jsonc
// 表达式 = 字面量 | {"var": "x", "default": ...} | {"ref": "fragment"} | {"op": "...", "args": [...]}
{"op": "and", "args": [
  {"op": "in", "args": [{"var": "country"}, ["CN", "SG"]]},
  {"ref": "amount_check"}
]}
```

算子：`and or not eq ne lt lte gt gte add sub mul div mod in contains startswith
endswith lower upper concat len abs min max round coalesce if`，以及测试辅助
`sleep(ms)`（模拟慢节点，上限 2s）。`if` 为 eager 求值（两个分支都会执行）。

## API 一览

**editor :8001**
`POST/GET /fragments`、`GET/PUT /fragments/{name}`（更新触发增量重编译，并把固定了旧基线的等待中提案标冲突）、
`POST/GET /policies`、`GET /policies/{name}`、`GET /policies/{name}/versions`、
`GET /policies/{name}/chain`、`POST /policies/{name}/publish`、
提案：`POST/GET /proposals`、`GET /proposals/{id}`、`GET /proposals/{id}/diff`、
`POST /proposals/{id}/reviews`、`GET /proposals/{id}/reviews`、
`POST /proposals/{id}/withdraw`、`POST /proposals/{id}/apply`、
`GET /proposals/{id}/timeline`、
`GET /proposals/approval-config`、`PUT /proposals/approval-config-default`、
`PUT /proposals/approval-config/{policy}`、`GET /audit`

**compiler :8002**
`POST /compile`、`POST /compile/affected`、`POST /compile/batch`、
`GET /artifacts/{policy}`、`POST /artifacts/{policy}/{version}/revoke`、`GET /audit`

**runtime :8003**
`POST /query`（`{policy, min_version, inputs, timeout_ms, strict_min_version, request_id}`）、
`GET /decisions`、`GET /decisions/{id}`、`GET /cache`（节点加载状态）、`GET /audit`

**debugger :8004**
`POST /debug/sessions`（`{policy, inputs, actor, version?, title?, breakpoints?, secret_keys?, lease_ttl_s?}`）、
`GET /debug/sessions`、`GET /debug/sessions/{id}`、`POST /debug/sessions/{id}/end`、
`POST /debug/sessions/{id}/step`、`POST /debug/sessions/{id}/continue`、
`POST /debug/sessions/{id}/pause`、`PUT /debug/sessions/{id}/breakpoints`、
`POST /debug/sessions/{id}/fork`、
`GET /debug/sessions/{id}/branches/{branch}`、
`GET /debug/sessions/{id}/compare?a=&b=`、`GET /debug/sessions/{id}/timeline`、
`POST /debug/sessions/{id}/lease`（续租）、
`POST /debug/sessions/{id}/lease/takeover`（到期后接管）、`GET /audit`

**quota :8005**（`QUOTA_ROLE=collector,gate,accountant` 可分开启动）
管理：`POST /quota/accounts`、`POST /quota/rules`（mode=shared/dedicated）、
`POST /quota/limits`（`{scope_rule?, amount, effective_from?}`）
采集：`POST /quota/holds`、`POST /quota/vouchers`、`POST /quota/aborts`、
`GET /quota/events`、`POST /quota/events/{id}/requeue`
门禁/凭证查询：`GET /quota/holds`、`GET /quota/vouchers`
核算：`POST /quota/batches/seal`、`POST /quota/batches/auto-seal`、
`GET /quota/batches/{account}/{date}/page`
财务：`GET /quota/adjustments`、`POST /quota/adjustments/{id}/decision`
（confirm/reject，可修正带符号金额）、`POST /quota/adjustments/manual`
解释：`GET /quota/usage/{account}?scope_rule=&batch_date=`、
`GET /quota/trace/{serial}`
运维：`POST /quota/tick/{collector|gate|accountant}`（手动触发一个工作周期）

## 设计取舍

- **运行时与编译器算子表分离**：`approx_match` 仅编译器支持，用于真实复现滚动升级期间
  "新产物在旧 runtime 上部分节点未加载"的回退场景。
- **输入摘要不落完整输入**：决策日志存键名 + 哈希 + 512 字符预览，兼顾审计与数据最小化。
- **撤销不删除**：版本撤销只打标记，历史产物与审计永久保留。
- **测试用 SQLite、部署用 Postgres**：并发控制在两种方言下都正确（行锁 vs 唯一约束重试），
  测试里用 monkeypatch 把 editor→compiler 的 HTTP 调用替换为进程内直连。
