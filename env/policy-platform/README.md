# 策略编译与执行平台（policy-platform）

策略编辑、编译校验、运行时查询三个**可独立部署**的服务。策略由可复用片段（fragment）
组成，发布前解析依赖图、检测循环，生成**带版本的不可变产物**；运行时按请求上下文
选版执行，失败按安全回退规则降级，全程可审计。

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
                  └────────────▲─────────────────────────┘
                               │ 读产物（启动预热 + 缓存）
            ┌──────────────────┴───┐
  查询 ────▶│  runtime   :8003     │
            └──────────────────────┘
```

- **editor**：片段/策略 CRUD、发布入口、依赖链查询、**变更提案与评审工作流**。片段更新后调用 compiler 做**增量重编译**。
- **compiler**：依赖图解析、循环检测、拓扑排序、生成不可变版本产物；撤销版本。
- **runtime**：按 `min_version` 选版执行；节点未加载/超时按回退规则降级；记录决策日志。
- 三个服务无共享内存状态，各自独立扩缩容；共享存储只有数据库。

## 快速开始

```bash
docker compose up --build        # db + compiler + editor + runtime
./scripts/demo.sh                # 端到端演示（需要 jq）
```

本地开发（SQLite，无需 Docker）：

```bash
pip install -r requirements-dev.txt
python -m pytest tests/                       # 60 个测试
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.compiler.main:app --port 8002 &
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.editor.main:app --port 8001 &
DATABASE_URL=sqlite:///./dev.db python -m uvicorn app.runtime.main:app --port 8003 &
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

## 设计取舍

- **运行时与编译器算子表分离**：`approx_match` 仅编译器支持，用于真实复现滚动升级期间
  "新产物在旧 runtime 上部分节点未加载"的回退场景。
- **输入摘要不落完整输入**：决策日志存键名 + 哈希 + 512 字符预览，兼顾审计与数据最小化。
- **撤销不删除**：版本撤销只打标记，历史产物与审计永久保留。
- **测试用 SQLite、部署用 Postgres**：并发控制在两种方言下都正确（行锁 vs 唯一约束重试），
  测试里用 monkeypatch 把 editor→compiler 的 HTTP 调用替换为进程内直连。
