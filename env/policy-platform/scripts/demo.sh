#!/usr/bin/env bash
# 端到端演示：编辑 -> 编译 -> 运行时查询 -> 回退 -> 撤销 -> 审计 -> 逐步调试
# 用法: ./scripts/demo.sh   (需要 curl 和 jq；服务地址可用环境变量覆盖)
set -euo pipefail

EDITOR=${EDITOR_URL:-http://localhost:8001}
COMPILER=${COMPILER_URL:-http://localhost:8002}
RUNTIME=${RUNTIME_URL:-http://localhost:8003}
DEBUGGER=${DEBUGGER_URL:-http://localhost:8004}
QUOTA=${QUOTA_URL:-http://localhost:8005}

command -v jq >/dev/null || { echo "需要 jq"; exit 1; }

step() { echo; echo "=== $* ==="; }

step "0. 等待五个服务就绪"
for url in "$EDITOR/health" "$COMPILER/health" "$RUNTIME/health" "$DEBUGGER/health" "$QUOTA/health"; do
  for i in $(seq 1 30); do
    curl -sf "$url" >/dev/null 2>&1 && break
    [ "$i" = 30 ] && { echo "服务未就绪: $url"; exit 1; }
    sleep 1
  done
done
echo ok

step "1. 创建可复用片段（geo_check / amount_check / risk_base）"
curl -s -X POST "$EDITOR/fragments" -H 'content-type: application/json' -d '{
  "name": "geo_check",
  "body": {"op": "in", "args": [{"var": "country"}, ["CN", "SG", "HK"]]},
  "actor": "alice"}' | jq -c '{name: .fragment.name, version: .fragment.version}'
curl -s -X POST "$EDITOR/fragments" -H 'content-type: application/json' -d '{
  "name": "amount_check",
  "body": {"op": "lt", "args": [{"var": "amount"}, 10000]},
  "actor": "alice"}' | jq -c '{name: .fragment.name, version: .fragment.version}'
curl -s -X POST "$EDITOR/fragments" -H 'content-type: application/json' -d '{
  "name": "risk_base",
  "body": {"op": "and", "args": [{"ref": "geo_check"}, {"ref": "amount_check"}]},
  "actor": "alice"}' | jq -c '{name: .fragment.name, refs: .fragment.refs}'

step "2. 创建策略并发布 v1"
curl -s -X POST "$EDITOR/policies" -H 'content-type: application/json' -d '{
  "name": "payment_risk", "entry_fragment": "risk_base", "actor": "alice"}' | jq -c .
curl -s -X POST "$EDITOR/policies/payment_risk/publish" -H 'content-type: application/json' \
  -d '{"actor": "alice"}' | jq -c .

step "3. 运行时查询（v1）"
curl -s -X POST "$RUNTIME/query" -H 'content-type: application/json' -d '{
  "policy": "payment_risk",
  "inputs": {"country": "CN", "amount": 500}}' | jq -c '{result, used_version, fallback}'

step "4. 更新片段 geo_check -> 只增量重编译受影响策略（payment_risk -> v2）"
curl -s -X PUT "$EDITOR/fragments/geo_check" -H 'content-type: application/json' -d '{
  "body": {"op": "in", "args": [{"var": "country"}, ["CN"]]},
  "actor": "bob"}' | jq -c '{fragment: .fragment.name, to_version: .fragment.version, recompiled: .recompile.recompiled}'

step "5. 查询指定 min_version=2（SG 已被移出白名单）"
curl -s -X POST "$RUNTIME/query" -H 'content-type: application/json' -d '{
  "policy": "payment_risk", "min_version": 2,
  "inputs": {"country": "SG", "amount": 500}}' | jq -c '{result, used_version}'

step "6. 重复发布同内容 -> duplicate，不产生新版本"
curl -s -X POST "$EDITOR/policies/payment_risk/publish" -H 'content-type: application/json' \
  -d '{"actor": "alice"}' | jq -c .

step "7. 引入循环依赖 -> 编译失败，旧产物不受影响"
curl -s -X PUT "$EDITOR/fragments/risk_base" -H 'content-type: application/json' -d '{
  "body": {"op": "not", "args": [{"ref": "risk_base"}]}, "actor": "mallory"}' \
  | jq -c '{recompiled: .recompile.recompiled}'
curl -s -X POST "$RUNTIME/query" -H 'content-type: application/json' -d '{
  "policy": "payment_risk", "inputs": {"country": "CN", "amount": 1}}' \
  | jq -c '{result, used_version, note: "循环导致编译失败后，旧版本仍在服务"}'
# 恢复
curl -s -X PUT "$EDITOR/fragments/risk_base" -H 'content-type: application/json' -d '{
  "body": {"op": "and", "args": [{"ref": "geo_check"}, {"ref": "amount_check"}]},
  "actor": "alice"}' | jq -c '{recompiled: [.recompile.recompiled[] | {policy, status, version}]}'

step "8. 新算子 approx_match（编译器认识、运行时未加载）-> 节点未加载回退"
curl -s -X POST "$EDITOR/fragments" -H 'content-type: application/json' -d '{
  "name": "fuzzy_check",
  "body": {"op": "eq", "args": [{"var": "email"}, "a@b.com"]}, "actor": "alice"}' >/dev/null
curl -s -X POST "$EDITOR/policies" -H 'content-type: application/json' -d '{
  "name": "fuzzy_policy", "entry_fragment": "fuzzy_check", "actor": "alice"}' >/dev/null
curl -s -X POST "$EDITOR/policies/fuzzy_policy/publish" -H 'content-type: application/json' \
  -d '{"actor": "alice"}' | jq -c .
curl -s -X PUT "$EDITOR/fragments/fuzzy_check" -H 'content-type: application/json' -d '{
  "body": {"op": "approx_match", "args": [{"var": "email"}, "a@b.com"]},
  "actor": "alice"}' | jq -c '{recompiled: [.recompile.recompiled[] | {policy, status, version}]}'
curl -s -X POST "$RUNTIME/query" -H 'content-type: application/json' -d '{
  "policy": "fuzzy_policy", "inputs": {"email": "a@b.com"}}' \
  | jq -c '{result, used_version, fallback}'

step "9. 慢节点 + 超时 -> 回退到上一版本"
curl -s -X PUT "$EDITOR/fragments/fuzzy_check" -H 'content-type: application/json' -d '{
  "body": {"op": "and", "args": [
    {"op": "eq", "args": [{"op": "sleep", "args": [500]}, 500]},
    {"op": "eq", "args": [{"var": "email"}, "a@b.com"]}]},
  "actor": "alice"}' | jq -c '{recompiled: [.recompile.recompiled[] | {policy, status, version}]}'
curl -s -X POST "$RUNTIME/query" -H 'content-type: application/json' -d '{
  "policy": "fuzzy_policy", "timeout_ms": 100, "inputs": {"email": "a@b.com"}}' \
  | jq -c '{result, used_version, fallback: .fallback.attempts}'

step "10. 撤销 payment_risk v2 -> 运行时自动跳过（v3 仍可用）"
curl -s -X POST "$COMPILER/artifacts/payment_risk/2/revoke" -H 'content-type: application/json' \
  -d '{"reason": "规则过严，误伤新加坡用户", "actor": "ops"}' | jq -c .
curl -s -X POST "$RUNTIME/query" -H 'content-type: application/json' -d '{
  "policy": "payment_risk", "inputs": {"country": "SG", "amount": 500}}' \
  | jq -c '{result, used_version, note: "v2 已撤销，自动落到 v3"}'
echo "strict_min_version=true 且 min_version=99（无满足条件的版本）-> 409:"
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$RUNTIME/query" \
  -H 'content-type: application/json' -d '{
    "policy": "payment_risk", "min_version": 99, "strict_min_version": true,
    "inputs": {"country": "CN", "amount": 1}}'

step "11. 依赖链 / 决策日志（输入摘要）/ 审计轨迹"
curl -s "$EDITOR/policies/payment_risk/chain" \
  | jq -c '{entry: .entry_fragment, order: .live_graph.eval_order, artifact: .latest_artifact.dep_chain}'
curl -s "$RUNTIME/decisions?policy=payment_risk&limit=3" \
  | jq -c '.[] | {id, used_version, fallback_from, input_keys: .input_summary.keys, input_hash: .input_summary.sha256[0:12]}'
curl -s "$EDITOR/audit?limit=200" \
  | jq -c '[.[].event_type] | group_by(.) | map({event: .[0], count: length})'

step "12. 逐步调试：创建会话（固定版本 + 脱敏输入）"
# 造一个会除零的调试专用策略
curl -s -X POST "$EDITOR/fragments" -H 'content-type: application/json' -d '{
  "name": "ratio_check",
  "body": {"op": "div", "args": [{"var": "x"}, {"var": "y"}]},
  "actor": "alice"}' | jq -c '{fragment: .fragment.name}'
curl -s -X POST "$EDITOR/policies" -H 'content-type: application/json' -d '{
  "name": "ratio_policy", "entry_fragment": "ratio_check", "actor": "alice"}' >/dev/null
curl -s -X POST "$EDITOR/policies/ratio_policy/publish" -H 'content-type: application/json' \
  -d '{"actor": "alice"}' | jq -c '{published: .status, version: .version}'

# token 仅本次返回；email 等敏感输入会被掩码
TOKEN=$(curl -s -X POST "$DEBUGGER/debug/sessions" -H 'content-type: application/json' -d '{
  "policy": "ratio_policy",
  "inputs": {"x": 10, "y": 0, "email": "ops@example.com"},
  "actor": "alice", "lease_ttl_s": 300}' \
  | tee /tmp/dbg_session.json | jq -r '.lease.token')
SID=$(jq -r '.id' /tmp/dbg_session.json)
jq -c '{id, pinned: .artifact.pinned_version, version_status: .artifact.version_status.code,
        redacted_email: .inputs.email, masked_paths: .input_redaction.masked_paths}' \
  /tmp/dbg_session.json

step "13. continue 到确定性错误 -> 错误帧停在除零节点（会话不消失）"
curl -s -X POST "$DEBUGGER/debug/sessions/$SID/continue" -H 'content-type: application/json' \
  -d "{\"actor\": \"alice\", \"token\": \"$TOKEN\"}" >/dev/null
sleep 1
curl -s "$DEBUGGER/debug/sessions/$SID/branches/main" \
  | jq -c '{status: .branch.status, position: .branch.position,
            error: .branch.error, current: .branch.current.node_id,
            frames: [.branch.frames[] | {node: .node_id, result, error}]}'

step "14. 从错误点分叉，把 y 改为 2，子分支跑通；父分支历史不变"
CHILD=$(curl -s -X POST "$DEBUGGER/debug/sessions/$SID/fork" -H 'content-type: application/json' \
  -d "{\"actor\": \"alice\", \"token\": \"$TOKEN\", \"input_patch\": {\"y\": 2}, \"seq\": 1}" \
  | tee /tmp/dbg_fork.json | jq -r '.branch.branch_id')
jq -c '{parent: .parent.branch_id, child: .branch.branch_id, child_position: .branch.position}' \
  /tmp/dbg_fork.json
curl -s -X POST "$DEBUGGER/debug/sessions/$SID/continue" -H 'content-type: application/json' \
  -d "{\"actor\": \"alice\", \"token\": \"$TOKEN\", \"branch\": \"$CHILD\"}" >/dev/null
sleep 1
echo "-- 两个分支逐节点比较，第一处分歧（一侧缺/错、一侧成功）："
curl -s "$DEBUGGER/debug/sessions/$SID/compare?a=main&b=$CHILD" \
  | jq -c '{equal, ancestor: .common_ancestor, divergence: {kind: .first_divergence.kind,
            node: .first_divergence.node_id, a_error: .first_divergence.a.error.type,
            b_result: .first_divergence.b.result}}'

step "15. 时间线：推进 / 暂停 / 错误 / 分叉 / 接管 全程留痕"
curl -s "$DEBUGGER/debug/sessions/$SID/timeline" \
  | jq -c '{events: [.events[].event_type], branches: [.branches[] | {id: .branch_id, status, position}]}'
echo "租约到期后他人可 POST /debug/sessions/$SID/lease/takeover 接管"

step "16. 资源台账：开户（上海时区）+ 两条规则共用池 200 与独立限额 100"
curl -s -X POST "$QUOTA/quota/accounts" -H 'content-type: application/json' \
  -d '{"name":"demo","timezone":"Asia/Shanghai","actor":"op"}' | jq -c .
curl -s -X POST "$QUOTA/quota/rules" -H 'content-type: application/json' \
  -d '{"account":"demo","rule_name":"fraud","mode":"dedicated","initial_limit":100}' | jq -c '{rule: .rule_name, scope}'

step "17. 判定开始先占用：60 通过并签发通行令、再来 50 当场驳回（总量不破 100），重投回放同一裁决"
curl -s -X POST "$QUOTA/quota/holds" -H 'content-type: application/json' \
  -d '{"serial":"d1","account":"demo","rule_name":"fraud","occurred_at":"2026-09-16T10:00:00+08:00","amount":60}' \
  | jq -c '{duplicate, verdict: .verdict.verdict, status: .verdict.status, token: .verdict.token}'
curl -s -X POST "$QUOTA/quota/holds" -H 'content-type: application/json' \
  -d '{"serial":"d1","account":"demo","rule_name":"fraud","occurred_at":"2026-09-16T10:00:00+08:00","amount":60}' \
  | jq -c '{duplicate, verdict: .verdict.verdict, token: .verdict.token}'
echo "-- 网络抖动后再次询问，拿到同一份裁决："
curl -s "$QUOTA/quota/holds/d1/verdict" | jq -c '{verdict, status, token}'
curl -s -X POST "$QUOTA/quota/holds" -H 'content-type: application/json' \
  -d '{"serial":"d2","account":"demo","rule_name":"fraud","occurred_at":"2026-09-16T10:00:00+08:00","amount":50}' \
  | jq -c '{verdict: .verdict.verdict, reject_reason: .verdict.reject_reason}'
curl -s "$QUOTA/quota/holds?account=demo" | jq -c '[.[] | {serial, status, reject_reason}]'

step "18. 真实消耗 40 销账（预估 60，差额 20 自动退回），可花余额回到 60"
curl -s -X POST "$QUOTA/quota/vouchers" -H 'content-type: application/json' \
  -d '{"serial":"d1","account":"demo","rule_name":"fraud","occurred_at":"2026-09-16T10:05:00+08:00","amount":40}' >/dev/null
curl -s -X POST "$QUOTA/tick/accountant" >/dev/null
curl -s "$QUOTA/quota/usage/demo?scope_rule=fraud&batch_date=2026-09-16" \
  | jq -c '.scopes[0] | {limit: .limit.amount, held: .held.amount, settled: .settled.amount, available}'

step "19. 封账形成只读账页；晚到凭证挂起，财务确认后另记补账，原账页不覆盖"
curl -s -X POST "$QUOTA/quota/batches/seal" -H 'content-type: application/json' \
  -d '{"account":"demo","batch_date":"2026-09-16"}' | jq -c '.page | {page_id, lines: [.lines[] | {serial, type, amount}]}'
curl -s -X POST "$QUOTA/quota/vouchers" -H 'content-type: application/json' \
  -d '{"serial":"d3","account":"demo","rule_name":"fraud","occurred_at":"2026-09-16T12:00:00+08:00","amount":7}' >/dev/null
curl -s -X POST "$QUOTA/tick/accountant" >/dev/null
AID=$(curl -s "$QUOTA/quota/adjustments?status=PENDING" | jq '.[0].id')
curl -s -X POST "$QUOTA/quota/adjustments/$AID/decision" -H 'content-type: application/json' \
  -d '{"decision":"confirm","actor":"cfo","amount":7}' | jq -c '{status, entry: {kind: .entry.kind, amount: .entry.amount}}'
echo "-- 账页原文 + 封账后附录："
curl -s "$QUOTA/quota/batches/demo/2026-09-16/page" \
  | jq -c '{lines: [.lines[].serial], appendix: .post_seal_appendix.adjustment_entries}'

step "20. 沿流水 d1 串起占用、销账、账页全过程"
curl -s "$QUOTA/quota/trace/d1" | jq -c '[.timeline[] | {stage, label}]'

echo; echo "演示完成。"
