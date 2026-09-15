"""端到端集成测试：编辑 -> 编译 -> 运行时查询（SQLite 单进程，editor->compiler 直连）。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.common import compile_service, compiler_client
from app.common.db import SessionLocal

GEO = {"op": "in", "args": [{"var": "country"}, ["CN", "SG", "HK"]]}
AMOUNT = {"op": "lt", "args": [{"var": "amount"}, 10000]}
RISK = {"op": "and", "args": [{"ref": "geo_check"}, {"ref": "amount_check"}]}


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload

    def json(self):
        return self._p

    @property
    def text(self):
        return json.dumps(self._p)


def _wrap(result):
    st = result["status"]
    code = 200 if st in ("published", "duplicate") else (404 if st == "not_found" else 422)
    return _Resp(code, result)


def _direct_compile_policy(policy, actor):
    with SessionLocal() as s:
        return _wrap(compile_service.compile_policy(s, policy, actor))


def _direct_compile_affected(fragment, actor):
    with SessionLocal() as s:
        return _Resp(200, compile_service.compile_affected(s, fragment, actor))


@pytest.fixture()
def clients(monkeypatch):
    monkeypatch.setattr(compiler_client, "compile_policy", _direct_compile_policy)
    monkeypatch.setattr(compiler_client, "compile_affected", _direct_compile_affected)
    from app.compiler.main import app as compiler_app
    from app.editor.main import app as editor_app
    from app.runtime.main import app as runtime_app
    with TestClient(editor_app) as e, TestClient(compiler_app) as c, TestClient(runtime_app) as r:
        yield e, c, r


def setup_base(e):
    assert e.post("/fragments", json={"name": "geo_check", "body": GEO}).status_code == 201
    assert e.post("/fragments", json={"name": "amount_check", "body": AMOUNT}).status_code == 201
    assert e.post("/fragments", json={"name": "risk_base", "body": RISK}).status_code == 201
    assert e.post("/policies", json={
        "name": "payment_risk", "entry_fragment": "risk_base"}).status_code == 201


def publish(e, policy="payment_risk"):
    return e.post(f"/policies/{policy}/publish", json={"actor": "tester"})


def test_publish_and_query(clients):
    e, c, r = clients
    setup_base(e)
    resp = publish(e)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {**resp.json(), "status": "published", "version": 1}

    q = r.post("/query", json={"policy": "payment_risk",
                               "inputs": {"country": "CN", "amount": 500}})
    assert q.status_code == 200
    body = q.json()
    assert body["result"] is True
    assert body["used_version"] == 1
    assert body["fallback"] is None

    q2 = r.post("/query", json={"policy": "payment_risk",
                                "inputs": {"country": "US", "amount": 500}})
    assert q2.json()["result"] is False


def test_incremental_recompile_only_affected(clients):
    e, c, r = clients
    setup_base(e)
    # 第二个策略只依赖 amount_check，不应被 geo_check 更新波及
    assert e.post("/policies", json={
        "name": "amount_only", "entry_fragment": "amount_check"}).status_code == 201
    assert publish(e).json()["version"] == 1
    assert publish(e, "amount_only").json()["version"] == 1

    geo_v2 = {"op": "in", "args": [{"var": "country"}, ["CN"]]}
    resp = e.put("/fragments/geo_check", json={"body": geo_v2, "actor": "tester"})
    assert resp.status_code == 200
    recompiled = {x["policy"]: x for x in resp.json()["recompile"]["recompiled"]}
    assert set(recompiled) == {"payment_risk"}  # amount_only 未受影响
    assert recompiled["payment_risk"]["status"] == "published"
    assert recompiled["payment_risk"]["version"] == 2

    assert len(e.get("/policies/amount_only/versions").json()) == 1
    q = r.post("/query", json={"policy": "payment_risk", "min_version": 2,
                               "inputs": {"country": "SG", "amount": 500}})
    assert q.json()["used_version"] == 2
    assert q.json()["result"] is False  # SG 已不在白名单


def test_duplicate_publish_is_idempotent(clients):
    e, c, r = clients
    setup_base(e)
    assert publish(e).json()["status"] == "published"
    resp = publish(e)
    assert resp.json()["status"] == "duplicate"
    assert resp.json()["version"] == 1
    assert len(e.get("/policies/payment_risk/versions").json()) == 1


def test_compile_failure_keeps_last_good_artifact(clients):
    e, c, r = clients
    setup_base(e)
    assert publish(e).json()["version"] == 1

    # 把 risk_base 改成自引用循环 -> 增量重编译失败
    bad = {"op": "not", "args": [{"ref": "risk_base"}]}
    resp = e.put("/fragments/risk_base", json={"body": bad})
    recompiled = resp.json()["recompile"]["recompiled"]
    assert recompiled[0]["status"] == "failed"
    assert "cycle" in recompiled[0]["error"].lower()

    # 旧产物仍在，查询不受影响
    assert e.get("/policies/payment_risk").json()["latest_version"] == 1
    q = r.post("/query", json={"policy": "payment_risk",
                               "inputs": {"country": "CN", "amount": 1}})
    assert q.status_code == 200 and q.json()["used_version"] == 1

    # 审计里有失败记录
    events = e.get("/audit", params={"event_type": "PUBLISH_FAILED"}).json()
    assert any("cycle" in ev["payload"].get("error", "").lower() for ev in events)

    # 修复后可以继续发布新版本
    e.put("/fragments/risk_base", json={"body": RISK})
    assert e.get("/policies/payment_risk").json()["latest_version"] == 2


def test_nodes_not_loaded_fallback(clients):
    e, c, r = clients
    # v1: 普通算子；v2: 仅编译器认识的 approx_match（模拟滚动升级版本偏斜）
    assert e.post("/fragments", json={
        "name": "fuzzy_check",
        "body": {"op": "eq", "args": [{"var": "email"}, "a@b.com"]}}).status_code == 201
    assert e.post("/policies", json={
        "name": "fuzzy_policy", "entry_fragment": "fuzzy_check"}).status_code == 201
    assert publish(e, "fuzzy_policy").json()["version"] == 1

    e.put("/fragments/fuzzy_check", json={
        "body": {"op": "approx_match", "args": [{"var": "email"}, "a@b.com"]}})
    assert e.get("/policies/fuzzy_policy").json()["latest_version"] == 2

    q = r.post("/query", json={"policy": "fuzzy_policy",
                               "inputs": {"email": "a@b.com"}})
    assert q.status_code == 200
    body = q.json()
    assert body["used_version"] == 1           # 回退到 v1
    assert body["result"] is True
    assert body["fallback"]["from_version"] == 2
    assert body["fallback"]["attempts"][0]["reason"] == "nodes_not_loaded"
    assert body["fallback"]["attempts"][0]["missing_ops"] == ["approx_match"]


def test_timeout_fallback(clients):
    e, c, r = clients
    assert e.post("/fragments", json={
        "name": "slow_check", "body": {"op": "eq", "args": [1, 1]}}).status_code == 201
    assert e.post("/policies", json={
        "name": "slow_policy", "entry_fragment": "slow_check"}).status_code == 201
    assert publish(e, "slow_policy").json()["version"] == 1

    slow = {"op": "eq", "args": [{"op": "sleep", "args": [300]}, 300]}
    e.put("/fragments/slow_check", json={"body": slow})
    assert e.get("/policies/slow_policy").json()["latest_version"] == 2

    q = r.post("/query", json={"policy": "slow_policy", "timeout_ms": 50})
    assert q.status_code == 200
    body = q.json()
    assert body["used_version"] == 1
    assert body["fallback"]["from_version"] == 2
    assert body["fallback"]["attempts"][0]["reason"] == "timeout"


def test_revoke_version(clients):
    e, c, r = clients
    setup_base(e)
    publish(e)
    e.put("/fragments/geo_check", json={
        "body": {"op": "in", "args": [{"var": "country"}, ["CN"]]}})
    assert e.get("/policies/payment_risk").json()["latest_version"] == 2

    resp = c.post("/artifacts/payment_risk/2/revoke",
                  json={"reason": "bad rollout", "actor": "ops"})
    assert resp.json()["status"] == "revoked"

    # v2 被撤销后自动使用 v1
    q = r.post("/query", json={"policy": "payment_risk",
                               "inputs": {"country": "SG", "amount": 1}})
    assert q.json()["used_version"] == 1

    # strict 模式：没有 >=2 的可用版本 -> 409
    q2 = r.post("/query", json={"policy": "payment_risk", "min_version": 2,
                                "strict_min_version": True,
                                "inputs": {"country": "CN", "amount": 1}})
    assert q2.status_code == 409

    # 非 strict：允许低于 min_version 的安全网回退，并明确标记
    q3 = r.post("/query", json={"policy": "payment_risk", "min_version": 2,
                                "inputs": {"country": "CN", "amount": 1}})
    assert q3.json()["used_version"] == 1
    assert q3.json()["below_min_version"] is True

    events = e.get("/audit", params={"event_type": "VERSION_REVOKED"}).json()
    assert events and events[0]["payload"]["reason"] == "bad rollout"


def test_decision_log_and_dependency_chain(clients):
    e, c, r = clients
    setup_base(e)
    publish(e)
    q = r.post("/query", json={"policy": "payment_risk", "request_id": "req-1",
                               "inputs": {"country": "CN", "amount": 500}})
    decision_id = q.json()["decision_id"]

    decisions = r.get("/decisions", params={"policy": "payment_risk"}).json()
    assert len(decisions) == 1
    summary = decisions[0]["input_summary"]
    assert summary["keys"] == ["amount", "country"]
    assert len(summary["sha256"]) == 64

    d = r.get(f"/decisions/{decision_id}").json()
    assert d["request_id"] == "req-1"
    assert d["used_version"] == 1
    assert {x["fragment"] for x in d["dep_chain"]} == {
        "risk_base", "geo_check", "amount_check"}

    chain = e.get("/policies/payment_risk/chain").json()
    names = {n["fragment"] for n in chain["live_graph"]["nodes"]}
    assert names == {"risk_base", "geo_check", "amount_check"}
    assert {"from": "risk_base", "to": "geo_check"} in chain["live_graph"]["edges"]
    assert chain["live_graph"]["cycle"] is None
    assert chain["latest_artifact"]["version"] == 1


def test_audit_trail_covers_lifecycle(clients):
    e, c, r = clients
    setup_base(e)
    publish(e)
    types = {ev["event_type"] for ev in e.get("/audit").json()}
    assert {"SERVICE_STARTED", "FRAGMENT_CREATED", "POLICY_CREATED",
            "PUBLISH_REQUESTED", "PUBLISH_SUCCEEDED"} <= types


def test_restart_recovers_and_is_audited(clients):
    e, c, r = clients
    setup_base(e)
    publish(e)
    q = r.post("/query", json={"policy": "payment_risk",
                               "inputs": {"country": "CN", "amount": 1}})
    assert q.status_code == 200

    # 模拟 runtime 重启：重新进入 lifespan，缓存清空后从存储恢复
    from app.runtime.main import app as runtime_app
    import app.runtime.main as rt
    rt.cache.clear()
    with TestClient(runtime_app) as r2:
        q2 = r2.post("/query", json={"policy": "payment_risk",
                                     "inputs": {"country": "CN", "amount": 1}})
        assert q2.status_code == 200 and q2.json()["used_version"] == 1
        assert "payment_risk@1" in r2.get("/cache").json()

    started = e.get("/audit", params={"event_type": "SERVICE_STARTED"}).json()
    assert len(started) >= 2


def test_all_candidates_failed_returns_503(clients):
    e, c, r = clients
    # 唯一版本就用了 runtime 不认识的算子 -> 无版可退
    assert e.post("/fragments", json={
        "name": "only_fuzzy",
        "body": {"op": "approx_match", "args": [{"var": "x"}, "y"]}}).status_code == 201
    assert e.post("/policies", json={
        "name": "doomed", "entry_fragment": "only_fuzzy"}).status_code == 201
    assert publish(e, "doomed").json()["version"] == 1

    q = r.post("/query", json={"policy": "doomed", "inputs": {"x": "y"}})
    assert q.status_code == 503
    assert q.json()["detail"]["attempts"][0]["reason"] == "nodes_not_loaded"
    # 失败也记录了决策
    d = r.get(f"/decisions/{q.json()['detail']['decision_id']}").json()
    assert d["used_version"] is None and d["error"] == "all_candidate_versions_failed"


def test_deterministic_error_does_not_fallback(clients):
    e, c, r = clients
    setup_base(e)
    publish(e)
    q = r.post("/query", json={"policy": "payment_risk",
                               "inputs": {"country": "CN"}})  # 缺 amount
    assert q.status_code == 422
    assert "missing input" in q.json()["detail"]["error"]


def test_concurrent_publish_versions_stay_consistent(clients):
    """并发发布：唯一约束 + 重试保证版本连续、无哈希重复。"""
    import threading

    e, c, r = clients
    setup_base(e)
    publish(e)  # v1

    results = []

    def worker(i):
        with SessionLocal() as s:
            res = compile_service.compile_policy(s, "payment_risk", f"w{i}")
            results.append(res["status"])

    # 同一内容并发发布 8 次：全部应收敛为 duplicate（v1 已存在）
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) == {"duplicate"}
    versions = e.get("/policies/payment_risk/versions").json()
    assert [v["version"] for v in versions] == [1]
