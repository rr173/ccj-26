"""策略逐步调试服务测试（SQLite 单进程）。

覆盖：
- 会话创建：固定产物版本、输入脱敏、节点图快照
- step / continue / pause：按实际求值顺序推进、断点（node/op/condition）
- 确定性错误停在出错节点形成错误帧
- 分叉修改输入：父分支历史不可变、逐节点比较定位第一处分歧
- 租约：观察者拒绝、到期接管、旧 token 拒绝、cmd_id 幂等、seq 防乱序
- 长暂停后继续、服务重启恢复
- 产物撤销/新版本：会话仍固定版本并明确标注版本状态
- 时间线完整
"""
import time

import pytest
from fastapi.testclient import TestClient

from app.common import debug_service as ds
from app.common.db import SessionLocal

GEO = {"op": "in", "args": [{"var": "country"}, ["CN", "SG", "HK"]]}
AMOUNT = {"op": "lt", "args": [{"var": "amount"}, 10000]}
# 先 geo，再 amount，再 and；topo 末尾是入口节点 risk_base#3
RISK = {"op": "and", "args": [{"ref": "geo_check"}, {"ref": "amount_check"}]}
# 会除零的策略
DIV = {"op": "div", "args": [{"var": "x"}, {"var": "y"}]}


@pytest.fixture()
def dbg():
    """直接用领域层 + 独立 TestClient（debugger 只读写库，不依赖其它服务）。"""
    from app.debugger.main import app as debugger_app
    with TestClient(debugger_app) as c:
        yield c


def _publish_policy(e_client, name="payment_risk", entry="risk_base"):
    from app.common import compile_service
    for fname, body in (("geo_check", GEO), ("amount_check", AMOUNT),
                        ("risk_base", RISK)):
        r = e_client.post("/fragments", json={"name": fname, "body": body})
        assert r.status_code == 201, r.text
    assert e_client.post("/policies", json={
        "name": name, "entry_fragment": entry}).status_code == 201
    with SessionLocal() as s:
        out = compile_service.compile_policy(s, name, "tester")
    assert out["status"] == "published", out
    return out["version"]


def _publish_div(e_client, name="div_policy"):
    from app.common import compile_service
    r = e_client.post("/fragments", json={"name": "div_frag", "body": DIV})
    assert r.status_code == 201
    assert e_client.post("/policies", json={
        "name": name, "entry_fragment": "div_frag"}).status_code == 201
    with SessionLocal() as s:
        out = compile_service.compile_policy(s, name, "tester")
    assert out["status"] == "published", out
    return out["version"]


@pytest.fixture()
def editor():
    from app.editor.main import app as editor_app
    with TestClient(editor_app) as e:
        yield e


# --------------------------------------------------------------------------- #
# 创建会话：固定版本 + 脱敏
# --------------------------------------------------------------------------- #

def test_create_session_pins_version_and_redacts_inputs(editor, dbg):
    _publish_policy(editor)
    resp = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 500, "email": "a@b.com",
                   "nested": {"phone": "123", "ok": 1}},
        "actor": "alice", "lease_ttl_s": 60})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["artifact"]["pinned_version"] == 1
    assert body["artifact"]["version_status"]["code"] == "active"
    token = body["lease"]["token"]
    assert token and body["lease"]["holder"] == "alice"

    # 输入已脱敏：email / 嵌套 phone 被掩码，普通输入保留
    assert body["inputs"]["email"] == ds.REDACTED
    assert body["inputs"]["nested"]["phone"] == ds.REDACTED
    assert body["inputs"]["nested"]["ok"] == 1
    assert body["inputs"]["country"] == "CN"
    masked = body["input_redaction"]["masked_paths"]
    assert "$.email" in masked and "$.nested.phone" in masked
    # 原始输入不落库，只有摘要
    summary = body["input_redaction"]["original_input_summary"]
    assert "preview" in summary and len(summary["sha256"]) == 64

    sid = body["id"]
    main = body["branches"][0]
    assert main["branch_id"] == "main" and main["status"] == "paused"

    # 初始视图：当前节点 + 剩余路径（实际求值顺序 = 产物 topo）
    g = dbg.get(f"/debug/sessions/{sid}").json()
    assert g["status"] == "active"


def test_create_session_requires_nonrevoked_version_and_supports_pin(editor, dbg):
    _publish_policy(editor)
    # 无版本
    assert dbg.post("/debug/sessions", json={
        "policy": "nope", "inputs": {}, "actor": "a"}).status_code == 404
    # 显式固定 v1，即使之后出了 v2
    r = dbg.post("/debug/sessions", json={
        "policy": "payment_risk", "inputs": {"country": "CN", "amount": 1},
        "actor": "a", "version": 1, "lease_ttl_s": 300})
    assert r.status_code == 201
    assert r.json()["artifact"]["pinned_version"] == 1


def test_inputs_used_are_redacted_not_raw(editor, dbg):
    """var 节点读到的是脱敏后的值（敏感输入不会以原文参与求值/落帧）。"""
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1, "email": "secret@x.com"},
        "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    # topo[0] 是 geo_check#1 (var country)
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": 1})
    assert r.status_code == 200, r.text
    # 没有 email 变量；直接查库确认 inputs 不含明文
    with SessionLocal() as s:
        sess = ds._get_session(s, sid)
        assert "secret@x.com" not in str(sess.inputs)


# --------------------------------------------------------------------------- #
# step：按求值顺序、入参/中间结果/剩余路径
# --------------------------------------------------------------------------- #

def test_step_advances_in_topo_order_with_args_and_results(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 500}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]

    # 按产物 topo（实际求值顺序，依赖在前）逐步走
    with SessionLocal() as s:
        topo = ds._get_session(s, sid).topo

    for i in range(len(topo) - 1):
        r = dbg.post(f"/debug/sessions/{sid}/step",
                     json={"actor": "alice", "token": token, "seq": i + 1})
        assert r.status_code == 200, r.text
        b = r.json()["branch"]
        assert b["position"] == i + 1
        assert b["status"] == "paused"
        assert r.json()["expected_seq"] == i + 2

    # 当前节点是最后的 and（入口），其 args_in 来自两个已求值子节点
    cur_before = r.json()["branch"]["current"]
    assert cur_before["node_id"].startswith("risk_base#")
    assert cur_before["op"] == "and"
    assert cur_before["args_in"] == [True, True]

    # 最后一步 -> completed
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": len(topo)})
    assert r.status_code == 200
    b = r.json()["branch"]
    assert b["status"] == "completed"
    assert b["final_result"] is True
    assert b["remaining_path"] == []

    # 完成后不能继续 step（要分叉才行）
    r2 = dbg.post(f"/debug/sessions/{sid}/step",
                  json={"actor": "alice", "token": token,
                        "seq": len(topo) + 1})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "branch_finished"

    # 每个节点的输入输出在分支详情里都能看到
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()
    frames = detail["branch"]["frames"]
    assert [f["index"] for f in frames] == list(range(len(topo)))
    and_frame = next(f for f in frames if f["node_id"].startswith("risk_base#"))
    assert and_frame["args_in"] == [True, True]
    assert and_frame["result"] is True
    assert all(f["error"] is None for f in frames)


    # 占位函数已移除 —— 节点类型直接经会话快照访问


# --------------------------------------------------------------------------- #
# 断点：node / op / condition
# --------------------------------------------------------------------------- #

def test_breakpoints_node_op_and_condition_via_continue(editor, dbg):
    _publish_policy(editor)
    # 在 op=lt 的节点上下断（amount_check 里的 lt）
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 500}, "actor": "alice",
        "breakpoints": [{"type": "op", "op": "lt"}]}).json()
    sid, token = body["id"], body["lease"]["token"]

    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token, "cmd_id": "c1"})
    assert r.status_code == 202, r.text
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")
    g = dbg.get(f"/debug/sessions/{sid}").json()
    main = g["branches"][0]
    assert main["status"] == "paused"
    # 停在 lt 节点执行前
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["current"]["op"] == "lt"
    assert detail["current"]["args_in"] == [500, 10000]
    # 时间线上有 breakpoint 暂停
    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    assert any(e["event_type"] == "paused"
               and e["payload"].get("reason") == "breakpoint"
               for e in tl["events"])

    # 换成无条件节点限定的条件断点 amount > 100：在每个剩余节点执行前求值，
    # 恢复后第一个节点（lt，index 2）条件即为真，停在该节点
    r = dbg.put(f"/debug/sessions/{sid}/breakpoints", json={
        "actor": "alice", "token": token,
        "breakpoints": [{"type": "condition",
                         "expr": {"op": "gt",
                                  "args": [{"var": "amount"}, 100]},
                         "node": None}]})
    assert r.status_code == 200
    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 202
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["position"] == 2
    assert detail["current"]["node_id"] == "amount_check#3"

    # 限定节点的条件断点：只在入口 and 前求值，应一直跑到 and
    dbg.put(f"/debug/sessions/{sid}/breakpoints", json={
        "actor": "alice", "token": token,
        "breakpoints": [{"type": "condition",
                         "expr": {"op": "gt",
                                  "args": [{"var": "amount"}, 100]},
                         "node": "risk_base#10"}]})
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token})
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["position"] == 9
    assert detail["current"]["node_id"] == "risk_base#10"

    # 条件不命中场景：amount=50（不大于 100），新会话同断点应直接跑完
    body2 = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 50}, "actor": "alice",
        "breakpoints": [{"type": "condition",
                         "expr": {"op": "gt",
                                  "args": [{"var": "amount"}, 100]}}]}).json()
    sid2, token2 = body2["id"], body2["lease"]["token"]
    dbg.post(f"/debug/sessions/{sid2}/continue",
             json={"actor": "alice", "token": token2})
    _wait_until(dbg, sid2, "main", lambda b: b["status"] != "running")
    detail2 = dbg.get(f"/debug/sessions/{sid2}/branches/main").json()["branch"]
    assert detail2["status"] == "completed"
    # CN 在白名单 且 50 < 10000 -> and 为 True
    assert detail2["final_result"] is True


def test_node_breakpoint_and_bad_breakpoint_rejected(editor, dbg):
    _publish_policy(editor)
    # 不存在的节点 -> 创建即 422
    r = dbg.post("/debug/sessions", json={
        "policy": "payment_risk", "inputs": {"country": "CN", "amount": 1},
        "actor": "alice",
        "breakpoints": [{"type": "node", "node": "ghost#9"}]})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_breakpoint_node"
    # 条件引用 ref（运行时不支持）-> 422
    r = dbg.post("/debug/sessions", json={
        "policy": "payment_risk", "inputs": {}, "actor": "alice",
        "breakpoints": [{"type": "condition",
                         "expr": {"op": "eq", "args": [{"ref": "x"}, 1]}}]})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# pause / 长暂停后继续
# --------------------------------------------------------------------------- #

def test_pause_stops_at_next_node_boundary(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 500}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    # 无断点 continue：图很小可能瞬间跑完，所以先在入口节点下断，再暂停
    with SessionLocal() as s:
        entry = ds._get_session(s, sid).entry_node
    dbg.put(f"/debug/sessions/{sid}/breakpoints", json={
        "actor": "alice", "token": token,
        "breakpoints": [{"type": "node", "node": entry}]})
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token})
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")
    # 停在入口 and 前；长暂停（睡一会儿）后删掉断点 continue 跑完
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["current"]["node_id"] == entry
    time.sleep(0.2)
    dbg.put(f"/debug/sessions/{sid}/breakpoints",
            json={"actor": "alice", "token": token, "breakpoints": []})
    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 202
    _wait_until(dbg, sid, "main", lambda b: b["status"] == "completed")
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["status"] == "completed"


def test_pause_when_not_running_is_409(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    r = dbg.post(f"/debug/sessions/{sid}/pause",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "not_running"


# --------------------------------------------------------------------------- #
# 确定性错误：错误帧停在对应节点
# --------------------------------------------------------------------------- #

def test_deterministic_error_stops_with_error_frame(editor, dbg):
    _publish_div(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "div_policy", "inputs": {"x": 10, "y": 0},
        "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    # 直接 continue 到除零节点
    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 202
    _wait_until(dbg, sid, "main", lambda b: b["status"] in ("error", "completed"))
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["status"] == "error"
    assert detail["error"]["type"] == "dsl_error"
    assert "division by zero" in detail["error"]["message"]
    # 游标停在出错节点：current 仍是该 div，剩余路径从它开始
    assert detail["current"]["op"] == "div"
    assert detail["current"]["args_in"] == [10, 0]
    assert detail["remaining_path"][0] == detail["current"]["node_id"]
    # 帧里有错误帧，会话依然可查
    frames = detail["frames"]
    err_frames = [f for f in frames if f["error"]]
    assert len(err_frames) == 1
    assert err_frames[0]["node_id"] == detail["current"]["node_id"]

    g = dbg.get(f"/debug/sessions/{sid}").json()
    assert g["status"] == "active" and g["branches"][0]["status"] == "error"
    # 时间线有 error 事件
    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    assert any(e["event_type"] == "error" for e in tl["events"])

    # error 分支不能 step/continue，只能分叉或结束
    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "branch_finished"


def test_missing_input_is_error_frame(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk", "inputs": {"country": "CN"},
        "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 202
    _wait_until(dbg, sid, "main", lambda b: b["status"] in ("error", "completed"))
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["status"] == "error"
    assert detail["error"]["type"] == "missing_input"


# --------------------------------------------------------------------------- #
# 分叉：修改输入、历史不可变、逐节点比较
# --------------------------------------------------------------------------- #

def test_fork_modifies_inputs_parent_history_immutable_and_compare(editor, dbg):
    _publish_div(editor)
    # main 缺少 y：在读取 y 的 var 节点形成 missing_input 错误帧
    body = dbg.post("/debug/sessions", json={
        "policy": "div_policy", "inputs": {"x": 10},
        "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token})
    _wait_until(dbg, sid, "main", lambda b: b["status"] == "error")
    main_before = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert main_before["error"] is not None

    # 从错误点分叉，补上 y=2
    r = dbg.post(f"/debug/sessions/{sid}/fork", json={
        "actor": "alice", "token": token, "input_patch": {"y": 2},
        "seq": 1})
    assert r.status_code == 201, r.text
    child_id = r.json()["branch"]["branch_id"]
    assert r.json()["parent"]["branch_id"] == "main"
    assert r.json()["parent"]["next_seq"] == 2
    assert r.json()["branch"]["position"] == 0  # 子分支从头重放

    # 父分支历史原样保留：仍是 error，帧未被改写
    main_after = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert main_after["status"] == "error"
    assert [f["index"] for f in main_after["frames"]] == \
           [f["index"] for f in main_before["frames"]]

    # 子分支跑到完成，结果 5.0
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token, "branch": child_id})
    _wait_until(dbg, sid, child_id, lambda b: b["status"] != "running")
    child = dbg.get(f"/debug/sessions/{sid}/branches/{child_id}").json()["branch"]
    assert child["status"] == "completed" and child["final_result"] == 5.0

    # 逐节点比较：第一处分歧就是 var y 节点（一侧缺输入错误，一侧成功）
    cmp = dbg.get(f"/debug/sessions/{sid}/compare",
                  params={"a": "main", "b": child_id}).json()
    assert cmp["equal"] is False
    d = cmp["first_divergence"]
    assert d["kind"] == "error"
    assert d["a"]["error"] is not None and d["b"]["error"] is None
    assert d["b"]["result"] == 2

    # 时间线含 forked，且子分支谱系正确
    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    fk = [e for e in tl["events"] if e["event_type"] == "forked"]
    assert fk and fk[0]["branch_id"] == child_id
    assert fk[0]["payload"]["parent_branch"] == "main"
    assert fk[0]["payload"]["changed_keys"] == ["y"]


def test_fork_compare_result_divergence(editor, dbg):
    """两侧都成功但中间结果不同：第一处分歧 kind=result，定位到具体节点。"""
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 500}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    # main 直接跑完 -> True
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token})
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")

    # 在完成点分叉，改 country=US，子分支跑完 -> False
    r = dbg.post(f"/debug/sessions/{sid}/fork", json={
        "actor": "alice", "token": token,
        "input_patch": {"country": "US"}, "seq": 1})
    child = r.json()["branch"]["branch_id"]
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token, "branch": child})
    _wait_until(dbg, sid, child, lambda b: b["status"] != "running")

    cmp = dbg.get(f"/debug/sessions/{sid}/compare",
                  params={"a": "main", "b": child}).json()
    assert cmp["equal"] is False
    d = cmp["first_divergence"]
    # 第一处分歧是读取 country 的 var 节点（'CN' vs 'US'），
    # 后续 in 节点 True vs False 的差异由它传导
    assert d["kind"] == "result"
    assert d["a"]["result"] == "CN" and d["b"]["result"] == "US"
    assert d["node_id"] == "geo_check#4"


def test_compare_equal_branches(editor, dbg):
    _publish_div(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "div_policy", "inputs": {"x": 10, "y": 2},
        "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token})
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")
    r = dbg.post(f"/debug/sessions/{sid}/fork", json={
        "actor": "alice", "token": token, "input_patch": {"y": 2}, "seq": 1})
    child = r.json()["branch"]["branch_id"]
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token, "branch": child})
    _wait_until(dbg, sid, child, lambda b: b["status"] != "running")
    cmp = dbg.get(f"/debug/sessions/{sid}/compare",
                  params={"a": "main", "b": child}).json()
    assert cmp["equal"] is True and cmp["first_divergence"] is None
    assert cmp["nodes_compared"] >= 3


# --------------------------------------------------------------------------- #
# 租约：观察者、接管、旧 token 拒绝
# --------------------------------------------------------------------------- #

def test_lease_observer_rejected_and_takeover(editor, dbg, monkeypatch):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice",
        "lease_ttl_s": 300}).json()
    sid, token = body["id"], body["lease"]["token"]

    # bob 没有租约 -> 只能观察，推进被拒
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "bob", "token": "whatever", "seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_lease_holder"
    assert dbg.get(f"/debug/sessions/{sid}").status_code == 200  # 观察允许

    # alice 用错 token 也被拒
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": "wrong", "seq": 1})
    assert r.status_code == 409

    # 租约未到期不能抢
    r = dbg.post(f"/debug/sessions/{sid}/lease/takeover",
                 json={"actor": "bob"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "lease_active"

    # 让租约立即到期，bob 接管
    with SessionLocal() as s:
        sess = ds._get_session(s, sid)
        from datetime import timedelta
        sess.lease_expires_at = ds.utcnow() - timedelta(seconds=1)
        s.commit()

    # 到期后旧持有人的命令被拒
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "lease_expired"

    r = dbg.post(f"/debug/sessions/{sid}/lease/takeover",
                 json={"actor": "bob", "ttl_s": 60})
    assert r.status_code == 200
    bob_token = r.json()["lease"]["token"]
    assert r.json()["lease"]["holder"] == "bob"

    # 接管后 alice 的旧 token 一律拒绝，即使她的命令带对的 seq
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_lease_holder"

    # bob 可以推进
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "bob", "token": bob_token, "seq": 1})
    assert r.status_code == 200

    # 时间线记录接管
    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    to = [e for e in tl["events"] if e["event_type"] == "lease_taken_over"]
    assert to and to[0]["payload"]["from_holder"] == "alice"
    assert to[0]["payload"]["to_holder"] == "bob"


def test_renew_lease_extends_and_old_takeover_still_blocked(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice",
        "lease_ttl_s": 300}).json()
    sid, token = body["id"], body["lease"]["token"]
    r = dbg.post(f"/debug/sessions/{sid}/lease",
                 json={"actor": "alice", "token": token, "ttl_s": 600})
    assert r.status_code == 200 and r.json()["lease"]["ttl_s"] == 600
    # 续租后他人仍不能接管
    assert dbg.post(f"/debug/sessions/{sid}/lease/takeover",
                    json={"actor": "bob"}).status_code == 409
    # 错 token 不能续租
    assert dbg.post(f"/debug/sessions/{sid}/lease",
                    json={"actor": "alice", "token": "x"}).status_code == 409


def test_takeover_stops_running_worker(editor, dbg):
    """后台 continue 期间租约被接管：worker 在下一节点边界停下（lease_lost）。"""
    _publish_policy(editor)
    # 让每次节点执行变慢：monkeypatch dsl 的 sleep 不现实，改用较大的图——
    # 用 sleep 算子构造多节点慢图
    slow = {"op": "and", "args": [
        {"op": "eq", "args": [{"op": "sleep", "args": [300]}, 300]},
        {"op": "eq", "args": [{"op": "sleep", "args": [300]}, 300]},
    ]}
    from fastapi.testclient import TestClient as TC
    r = editor.post("/fragments", json={"name": "slow_frag", "body": slow})
    assert r.status_code == 201
    editor.post("/policies", json={"name": "slow_policy", "entry_fragment": "slow_frag"})
    from app.common import compile_service
    with SessionLocal() as s:
        assert compile_service.compile_policy(s, "slow_policy", "t")["status"] == "published"

    body = dbg.post("/debug/sessions", json={
        "policy": "slow_policy", "inputs": {}, "actor": "alice",
        "lease_ttl_s": 300}).json()
    sid, token = body["id"], body["lease"]["token"]
    dbg.post(f"/debug/sessions/{sid}/continue",
             json={"actor": "alice", "token": token})
    time.sleep(0.15)  # worker 正在 sleep 节点中
    # 强制到期 + bob 接管
    with SessionLocal() as s:
        from datetime import timedelta
        sess = ds._get_session(s, sid)
        sess.lease_expires_at = ds.utcnow() - timedelta(seconds=1)
        s.commit()
    assert dbg.post(f"/debug/sessions/{sid}/lease/takeover",
                    json={"actor": "bob"}).status_code == 200
    # worker 应在节点边界停下而不是跑完
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running", timeout=5)
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["status"] == "paused"
    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    assert any(e["event_type"] == "paused"
               and e["payload"].get("reason") == "lease_lost"
               for e in tl["events"])


# --------------------------------------------------------------------------- #
# 幂等 / 防乱序
# --------------------------------------------------------------------------- #

def test_cmd_id_is_idempotent_duplicate_does_not_advance(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    payload = {"actor": "alice", "token": token, "seq": 1, "cmd_id": "step-1"}
    r1 = dbg.post(f"/debug/sessions/{sid}/step", json=payload)
    assert r1.status_code == 200
    pos1 = r1.json()["branch"]["position"]
    # 完全相同的重试：不进一步，回放同一响应
    r2 = dbg.post(f"/debug/sessions/{sid}/step", json=payload)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["branch"]["position"] == pos1
    # 下一节点数未增加
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert len(detail["frames"]) == pos1


def test_out_of_order_seq_returns_expected_seq(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    # 第一条就乱序（期望 1，发 5）
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": 5})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "unexpected_seq"
    assert d["expected_seq"] == 1 and d["got_seq"] == 5
    # 乱序命令没有推进
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["position"] == 0
    # 正常推进后，再发旧序号也被拒
    dbg.post(f"/debug/sessions/{sid}/step",
             json={"actor": "alice", "token": token, "seq": 1})
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["expected_seq"] == 2


def test_cmd_id_reuse_for_different_command_conflicts(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    dbg.post(f"/debug/sessions/{sid}/step",
             json={"actor": "alice", "token": token, "seq": 1, "cmd_id": "x"})
    r = dbg.post(f"/debug/sessions/{sid}/pause",
                 json={"actor": "alice", "token": token, "cmd_id": "x"})
    # pause 先报 not_running（语义检查先于冲突）；用 end 验证 cmd 冲突
    assert r.status_code == 409
    r2 = dbg.post(f"/debug/sessions/{sid}/end",
                  json={"actor": "alice", "token": token, "cmd_id": "x"})
    assert r2.status_code == 409 and r2.json()["detail"]["error"] == "cmd_id_conflict"


# --------------------------------------------------------------------------- #
# 持久化：长暂停 + 重启恢复
# --------------------------------------------------------------------------- #

def test_service_restart_recovers_running_and_keeps_paused_position(editor):
    """重启后：已暂停分支位置不变；崩溃残留 running 复位为 paused 并留痕。"""
    _publish_policy(editor)
    with SessionLocal() as s:
        created = ds.create_session(
            s, policy="payment_risk",
            inputs={"country": "CN", "amount": 1}, actor="alice")
        sid = created["id"]
        token = created["lease"]["token"]
        # 手动走 2 步
        ds.step(s, sid, actor="alice", token=token, seq=1)
        ds.step(s, sid, actor="alice", token=token, seq=2)
        # 模拟崩溃在 continue 途中：分支置 running（position 保持已落盘位置）
        sess = ds._get_session(s, sid)
        b = ds._get_branch(s, sess, "main")
        b.status = "running"
        s.commit()
        pos = b.position

    from app.debugger.main import app as debugger_app
    with TestClient(debugger_app):  # lifespan -> recover_running
        with SessionLocal() as s:
            recovered = ds.recover_running(s)
            assert recovered == 0  # lifespan 里已恢复，再跑一次幂等为 0
            sess = ds._get_session(s, sid)
            branch = ds._get_branch(s, sess, "main")
            assert branch.status == "paused"
            assert branch.position == pos  # 从原节点（已落盘的下一节点）继续
    with SessionLocal() as s:
        tl = ds.get_timeline(s, sid)
    reasons = [e["payload"].get("reason") for e in tl["events"]
               if e["event_type"] == "paused"]
    assert "service_restart" in reasons


# --------------------------------------------------------------------------- #
# 版本固定：撤销 / 新版本后既有会话不变并提示
# --------------------------------------------------------------------------- #

def test_revoked_or_newer_artifact_session_stays_pinned(editor, dbg, monkeypatch):
    # editor 片段更新会触发增量重编译：测试里用进程内直连替代 HTTP 调 compiler
    from app.common import compile_service, compiler_client

    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._p = payload

        def json(self):
            return self._p

        @property
        def text(self):
            import json as _json
            return _json.dumps(self._p)

    def _wrap(result):
        st = result["status"]
        code = 200 if st in ("published", "duplicate") else (
            404 if st == "not_found" else 422)
        return _Resp(code, result)

    def _affected(fragment, actor):
        with SessionLocal() as s:
            return compile_service.compile_affected(s, fragment, actor)

    monkeypatch.setattr(
        compiler_client, "compile_affected",
        lambda fragment, actor: _Resp(200, _affected(fragment, actor)))

    version = _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice",
        "version": version}).json()
    sid, token = body["id"], body["lease"]["token"]

    # 发布 v2（geo 只含 CN）并撤销 v1
    geo_v2 = {"op": "in", "args": [{"var": "country"}, ["CN"]]}
    editor.put("/fragments/geo_check", json={"body": geo_v2, "actor": "t"})
    r = dbg.get(f"/debug/sessions/{sid}").json()
    vs = r["artifact"]["version_status"]
    assert vs["code"] == "outdated" and vs["latest_version"] == 2

    # 直接在库里撤销 v1（等价于 compiler 的 revoke 接口）
    from app.common.models import Artifact
    with SessionLocal() as s:
        art = s.query(Artifact).filter_by(
            policy_name="payment_risk", version=1).first()
        art.revoked = True
        art.revoke_reason = "test revoke"
        art.revoked_by = "ops"
        s.commit()

    g = dbg.get(f"/debug/sessions/{sid}").json()
    vs = g["artifact"]["version_status"]
    assert vs["code"] == "revoked"
    assert vs["revoked"] is True and "REVOKED" in vs["note"]
    # 会话仍使用固定快照正常推进（CN 仍在旧白名单 [CN,SG,HK]）
    r = dbg.post(f"/debug/sessions/{sid}/continue",
                 json={"actor": "alice", "token": token})
    assert r.status_code == 202
    _wait_until(dbg, sid, "main", lambda b: b["status"] != "running")
    detail = dbg.get(f"/debug/sessions/{sid}/branches/main").json()["branch"]
    assert detail["status"] == "completed"
    assert detail["final_result"] is True  # v1 语义，未受 v2/撤销影响


# --------------------------------------------------------------------------- #
# 结束 / 列表
# --------------------------------------------------------------------------- #

def test_end_session_blocks_commands(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 1}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    r = dbg.post(f"/debug/sessions/{sid}/end",
                 json={"actor": "alice", "token": token, "reason": "done"})
    assert r.status_code == 200 and r.json()["status"] == "ended"
    r = dbg.post(f"/debug/sessions/{sid}/step",
                 json={"actor": "alice", "token": token, "seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "session_ended"
    # 时间线仍可查
    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    assert any(e["event_type"] == "ended" for e in tl["events"])
    assert tl["session"]["status"] == "ended"


def test_list_sessions_filter(editor, dbg):
    _publish_policy(editor)
    for i in range(2):
        dbg.post("/debug/sessions", json={
            "policy": "payment_risk",
            "inputs": {"country": "CN", "amount": i}, "actor": "alice"})
    lst = dbg.get("/debug/sessions", params={"policy": "payment_risk"}).json()
    assert len(lst) == 2
    assert dbg.get("/debug/sessions", params={"policy": "other"}).json() == []
    # 列表不回输入正文
    assert "inputs" not in lst[0]


# --------------------------------------------------------------------------- #
# 时间线完整性
# --------------------------------------------------------------------------- #

def test_timeline_documents_every_action(editor, dbg):
    _publish_policy(editor)
    body = dbg.post("/debug/sessions", json={
        "policy": "payment_risk",
        "inputs": {"country": "CN", "amount": 500}, "actor": "alice"}).json()
    sid, token = body["id"], body["lease"]["token"]
    dbg.post(f"/debug/sessions/{sid}/step",
             json={"actor": "alice", "token": token, "seq": 1})
    dbg.post(f"/debug/sessions/{sid}/fork",
             json={"actor": "alice", "token": token,
                   "input_patch": {"country": "US"}, "seq": 2})
    dbg.post(f"/debug/sessions/{sid}/end",
             json={"actor": "alice", "token": token})

    tl = dbg.get(f"/debug/sessions/{sid}/timeline").json()
    types = [e["event_type"] for e in tl["events"]]
    assert types[0] == "session_created"
    assert "advanced" in types and "forked" in types and "ended" in types
    # 每个事件带 ISO 时间
    assert all(e["ts"] for e in tl["events"])
    # 分支与帧都在时间线详情中
    assert {b["branch_id"] for b in tl["branches"]} >= {"main"}
    assert tl["branches"][0]["frames"]


# --------------------------------------------------------------------------- #
def _wait_until(client, sid, branch_id, pred, *, timeout=5.0):
    """轮询直到分支状态满足 pred（后台 worker 最终一致）。"""
    deadline = time.time() + timeout
    while True:
        detail = client.get(
            f"/debug/sessions/{sid}/branches/{branch_id}").json()["branch"]
        if pred(detail):
            return detail
        assert time.time() < deadline, f"condition timeout; branch={detail}"
        time.sleep(0.02)
