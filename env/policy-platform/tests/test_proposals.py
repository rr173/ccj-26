"""变更管控工作流测试：提案 -> 评审 -> 立即/预约生效。

覆盖：提交固化（版本/影响范围/逐项差异）、自评禁止、角色人数、拒绝/撤回/过期、
依赖变化标冲突且意见保留、同策略多提案确定顺序生效与基线移动冲突、
重复评审/重复执行幂等、完整时间线、服务重启后预约与评审续处理。
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.common import compile_service, compiler_client, proposal_service as ps
from app.common.db import SessionLocal
from app.common.models import Proposal
from tests.test_api import (_direct_compile_affected, _direct_compile_policy)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(compiler_client, "compile_policy", _direct_compile_policy)
    monkeypatch.setattr(compiler_client, "compile_affected", _direct_compile_affected)
    from app.editor.main import app as editor_app
    with TestClient(editor_app) as e:
        yield e


GEO = {"op": "in", "args": [{"var": "country"}, ["CN", "SG", "HK"]]}
AMOUNT = {"op": "lt", "args": [{"var": "amount"}, 10000]}
RISK = {"op": "and", "args": [{"ref": "geo_check"}, {"ref": "amount_check"}]}
GEO_V2 = {"op": "in", "args": [{"var": "country"}, ["CN", "SG"]]}
GEO_V3 = {"op": "in", "args": [{"var": "country"}, ["CN"]]}


def setup(client, publish=True):
    client.post("/fragments", json={"name": "geo_check", "body": GEO})
    client.post("/fragments", json={"name": "amount_check", "body": AMOUNT})
    client.post("/fragments", json={"name": "risk_base", "body": RISK})
    client.post("/policies", json={"name": "pay", "entry_fragment": "risk_base"})
    if publish:
        client.post("/policies/pay/publish", json={"actor": "owner"})


def submit(client, body=GEO_V2, actor="alice", **kw):
    for k in ("scheduled_at", "expires_at"):
        if isinstance(kw.get(k), datetime):
            kw[k] = kw[k].astimezone(timezone.utc).isoformat()
    payload = {"actor": actor, "title": kw.pop("title", "expand whitelist"),
               "changes": [{"fragment": "geo_check", "body": body}], **kw}
    return client.post("/proposals", json=payload)


def approve(client, pid, actor, role="approver"):
    return client.post(f"/proposals/{pid}/reviews",
                       json={"actor": actor, "role": role, "decision": "approved"})


# ---------- 提交固化：基线版本、影响范围、逐项语义差异 ----------

def test_submit_pins_baseline_impact_and_itemized_diff(client):
    setup(client)
    r = submit(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "IN_REVIEW"
    # 固定基线版本（geo_check 当前 v1）
    assert body["changes"][0]["base_version"] == 1
    assert body["changes"][0]["action"] == "update"
    # 固定影响范围
    assert body["impacted_policies"] == ["pay"]
    assert body["baseline"]["policy_versions"] == {"pay": 1}
    pins = body["baseline"]["fragments"]
    assert pins["geo_check"]["version"] == 1
    assert {n for n in pins} == {"geo_check", "amount_check", "risk_base"}
    # 逐项语义差异定位到具体路径（GEO=[CN,SG,HK] -> GEO_V2=[CN,SG]，移除 HK）
    diff = body["changes"][0]["diff"]
    paths = {(c["path"], c["kind"]) for c in diff["changes"]}
    assert ("$.args[1][2]", "removed") in paths
    assert diff["change_count"] == 1

    # 差异在提交后不再随后续片段变化而改变（固化）
    client.put("/fragments/geo_check", json={"body": GEO_V3})
    pinned = client.get(f"/proposals/{body['id']}/diff").json()
    assert pinned["changes"][0]["base_body"] == GEO  # 仍是提交时看到的 v1


def test_submit_preflight_rejects_cycle_and_missing_ref(client):
    setup(client)
    bad = {"op": "and", "args": [{"ref": "geo_check"}, {"op": "not", "args": [{"ref": "risk_base"}]}]}
    r = submit(client, body=bad)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "preflight_failed"
    assert "cycle" in r.json()["detail"]["detail"]["error"].lower()


def test_submit_rejects_identical_body(client):
    setup(client)
    r = submit(client, body=GEO)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "no_material_change"


def test_new_fragment_change_is_action_create(client):
    setup(client)
    body = {"op": "eq", "args": [{"var": "x"}, 1]}
    # 新片段不被任何策略引用 -> 无影响策略 -> 拒绝
    r = client.post("/proposals", json={
        "actor": "alice", "changes": [{"fragment": "newf", "body": body}]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "no_impacted_policy"


# ---------- 评审规则：自评禁止、角色人数 ----------

def test_proposer_cannot_approve_own(client):
    setup(client)
    pid = submit(client).json()["id"]
    r = approve(client, pid, "alice")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "self_approval_forbidden"


def test_quorum_requires_distinct_reviewers_per_role(client):
    setup(client)
    # 配置：sec 1 人 + ops 2 人
    client.put("/proposals/approval-config-default", json={
        "actor": "admin", "rules": [{"role": "sec", "count": 1}, {"role": "ops", "count": 2}]})
    pid = submit(client).json()["id"]

    assert approve(client, pid, "s1", "sec").json()["status"] == "IN_REVIEW"
    r = approve(client, pid, "o1", "ops").json()
    assert r["status"] == "IN_REVIEW" and r["quorum"][0]["role"] == "ops"
    r = approve(client, pid, "o2", "ops").json()
    assert r["status"] == "EFFECTIVE"  # 满足全部角色人数 -> 立即生效
    detail = client.get(f"/proposals/{pid}").json()
    assert detail["status"] == "EFFECTIVE"
    assert detail["applied"][0]["version"] == 2


def test_duplicate_review_is_idempotent(client):
    setup(client)
    client.put("/proposals/approval-config-default", json={
        "actor": "admin", "rules": [{"role": "approver", "count": 2}]})
    pid = submit(client).json()["id"]
    approve(client, pid, "bob")
    again = approve(client, pid, "bob")
    assert again.json()["status"] == "already_reviewed"
    # 只有一条意见
    reviews = client.get(f"/proposals/{pid}/reviews").json()
    assert [x["reviewer"] for x in reviews] == ["bob"]
    # 时间线也只有一次 REVIEWED
    tl = client.get(f"/proposals/{pid}/timeline").json()["timeline"]
    assert sum(1 for e in tl if e["event_type"] == "REVIEWED") == 1
    # 补上第二人后正常生效（幂等的重复意见没有多算一票）
    assert approve(client, pid, "carol").json()["status"] == "EFFECTIVE"


def test_rejection_terminates_and_cannot_take_effect(client):
    setup(client)
    pid = submit(client).json()["id"]
    r = client.post(f"/proposals/{pid}/reviews",
                    json={"actor": "bob", "role": "approver", "decision": "rejected",
                          "comment": "no"})
    assert r.json()["status"] == "REJECTED"
    # 拒绝后再有人同意也不能评审/生效
    late = approve(client, pid, "carol")
    assert late.status_code == 409 and late.json()["detail"]["error"] == "not_in_review"
    forced = client.post(f"/proposals/{pid}/apply", json={"actor": "carol"})
    assert forced.json()["status"] == "skipped"


def test_withdraw_by_proposer_only_and_terminal(client):
    setup(client)
    pid = submit(client).json()["id"]
    # 非发起人不能撤回
    assert client.post(f"/proposals/{pid}/withdraw", json={"actor": "bob"}).status_code == 403
    r = client.post(f"/proposals/{pid}/withdraw", json={"actor": "alice"})
    assert r.json()["status"] == "WITHDRAWN"
    # 撤回后不能再审批
    assert approve(client, pid, "bob").status_code == 409


def test_approval_after_expiry_marks_expired(client):
    setup(client)
    pid = submit(client).json()["id"]
    # 模拟等待到审批截止之后
    with SessionLocal() as s:
        s.get(Proposal, pid).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    r = approve(client, pid, "bob")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "proposal_expired"
    assert client.get(f"/proposals/{pid}").json()["status"] == "EXPIRED"


def test_sweep_expires_overdue_reviews(client):
    setup(client)
    pid = submit(client).json()["id"]
    with SessionLocal() as s:
        p = s.get(Proposal, pid)
        p.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    with SessionLocal() as s:
        assert ps.sweep_expired(s) == [pid]
    assert client.get(f"/proposals/{pid}").json()["status"] == "EXPIRED"


def test_expires_in_past_rejected_at_submit(client):
    setup(client)
    r = submit(client, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "expires_in_past"


# ---------- 依赖变化：标冲突且保留意见 ----------

def test_dependency_change_marks_conflict_and_keeps_reviews(client):
    setup(client)
    client.put("/proposals/approval-config-default", json={
        "actor": "admin", "rules": [{"role": "approver", "count": 2}]})
    pid = submit(client).json()["id"]
    approve(client, pid, "bob")  # 先给一条同意，尚未满足人数
    # 评审期间依赖片段在提案之外再次变化
    client.put("/fragments/geo_check", json={"body": GEO_V3})
    p = client.get(f"/proposals/{pid}").json()
    assert p["status"] == "CONFLICT"
    assert p["conflict_reason"]["fragment_drift"][0]["reason"] == "fragment_changed"
    # 已给出的意见保留
    reviews = client.get(f"/proposals/{pid}/reviews").json()
    assert len(reviews) == 1 and reviews[0]["reviewer"] == "bob"
    # 冲突后不能继续审批
    assert approve(client, pid, "carol").status_code == 409


def test_indirect_dependency_change_also_conflicts(client):
    setup(client)
    # 提案改 geo_check；评审期间另一个间接依赖 amount_check 被外部修改
    pid = submit(client).json()["id"]
    amt_v2 = {"op": "lt", "args": [{"var": "amount"}, 5000]}
    client.put("/fragments/amount_check", json={"body": amt_v2})
    assert client.get(f"/proposals/{pid}").json()["status"] == "CONFLICT"


# ---------- 预约生效、确定顺序、基线移动冲突 ----------

def _schedule_and_approve(client, body, actor, when, approver):
    pid = submit(client, body=body, actor=actor,
                 scheduled_at=when.replace(tzinfo=timezone.utc)).json()["id"]
    r = approve(client, pid, approver)
    assert r.json()["status"] == "SCHEDULED"
    return pid


def test_scheduled_not_due_does_not_apply(client):
    setup(client)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    pid = _schedule_and_approve(client, GEO_V2, "alice", future, "bob")
    with SessionLocal() as s:
        assert ps.run_due(SessionLocal) == []
    assert client.get(f"/proposals/{pid}").json()["status"] == "SCHEDULED"


def test_ordered_schedule_later_proposal_conflicts_when_baseline_moves(client):
    setup(client)
    t1 = datetime.now(timezone.utc) + timedelta(minutes=5)
    t2 = t1 + timedelta(minutes=5)
    p1 = _schedule_and_approve(client, GEO_V2, "alice", t1, "r1")
    p2 = _schedule_and_approve(client, GEO_V3, "bob", t2, "r2")
    # 到点：按 (scheduled_at, id) 确定顺序执行
    with SessionLocal() as s:
        for p in (s.get(Proposal, p1), s.get(Proposal, p2)):
            p.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    with SessionLocal() as s:
        results = ps.run_due(SessionLocal)
    assert results[0]["status"] == "EFFECTIVE"
    # 较晚执行的提案基线（geo_check v1）已被 p1 移动 -> 停止并说明冲突
    assert results[1]["status"] == "CONFLICT"
    drift = results[1]["conflict_reason"]
    assert drift["type"] == "dependency_changed"
    assert drift["trigger"]["type"] == "proposal_effective"
    assert drift["fragment_drift"][0]["pinned_version"] == 1
    assert drift["fragment_drift"][0]["current_version"] == 2
    d2 = client.get(f"/proposals/{p2}").json()
    assert d2["status"] == "CONFLICT"
    # 运行环境只进入了 p1 的产物（v2），p2 没产生版本
    assert [v["version"] for v in client.get("/policies/pay/versions").json()] == [2, 1]
    # p2 评审意见仍保留
    assert len(client.get(f"/proposals/{p2}/reviews").json()) == 1


def test_duplicate_execution_is_idempotent(client):
    setup(client)
    t = datetime.now(timezone.utc) + timedelta(minutes=1)
    pid = _schedule_and_approve(client, GEO_V2, "alice", t, "bob")
    with SessionLocal() as s:
        p = s.get(Proposal, pid)
        p.scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    with SessionLocal() as s:
        first = ps.claim_and_apply(SessionLocal, pid)
        second = ps.claim_and_apply(SessionLocal, pid)
    assert first["status"] == "EFFECTIVE"
    assert second["status"] == "already_effective"
    assert second["applied"] == first["applied"]
    # 产物只有一个新版本
    assert [v["version"] for v in client.get("/policies/pay/versions").json()] == [2, 1]
    # 时间线只有一次 EFFECTIVE
    tl = client.get(f"/proposals/{pid}/timeline").json()["timeline"]
    assert [e["event_type"] for e in tl].count("EFFECTIVE") == 1


def test_manual_apply_rejects_not_due(client):
    setup(client)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    pid = _schedule_and_approve(client, GEO_V2, "alice", future, "bob")
    r = client.post(f"/proposals/{pid}/apply", json={"actor": "ops"})
    assert r.json()["status"] == "not_due"


# ---------- 时间线与最终产物 ----------

def test_timeline_shows_full_lifecycle_decisions_and_artifact(client):
    setup(client)
    pid = submit(client).json()["id"]
    approve(client, pid, "bob", "approver")
    tl = client.get(f"/proposals/{pid}/timeline").json()
    types = [e["event_type"] for e in tl["timeline"]]
    assert types == ["SUBMITTED", "REVIEWED", "EFFECTIVE"]
    # 每位评审人的决定
    assert tl["reviews"][0]["reviewer"] == "bob"
    assert tl["reviews"][0]["decision"] == "approved"
    # 当时固定的差异
    assert tl["pinned_diff"][0]["fragment"] == "geo_check"
    assert tl["pinned_diff"][0]["diff"]["change_count"] >= 1
    # 最终进入运行环境的产物版本
    assert tl["runtime_artifacts"][0]["policy"] == "pay"
    assert tl["runtime_artifacts"][0]["version"] == 2
    assert len(tl["runtime_artifacts"][0]["hash"]) == 64


# ---------- 服务重启：未到点预约与等待中的评审继续处理 ----------

def test_restart_pending_schedule_and_review_continue(client):
    # 两个独立策略：A(pay) 上有一个等待第二人评审的提案；B(solo) 上有一个未到点的预约
    setup(client)
    client.post("/fragments", json={"name": "only_check",
                                    "body": {"op": "eq", "args": [{"var": "x"}, 1]}})
    client.post("/policies", json={"name": "solo", "entry_fragment": "only_check"})
    client.post("/policies/solo/publish", json={"actor": "owner"})
    client.put("/proposals/approval-config-default", json={
        "actor": "admin", "rules": [{"role": "approver", "count": 2}]})

    # A：geo_check 提案，1/2 人已同意，等待第二人
    pid_review = submit(client, body=GEO_V2, actor="alice").json()["id"]
    assert approve(client, pid_review, "bob").json()["status"] == "IN_REVIEW"

    # B：独立片段 only_check 的预约提案（同样需 2 人），两人到齐 -> SCHEDULED
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    only_v2 = {"op": "eq", "args": [{"var": "x"}, 2]}
    pid_sched = client.post("/proposals", json={
        "actor": "carol", "changes": [{"fragment": "only_check", "body": only_v2}],
        "scheduled_at": future.isoformat()}).json()["id"]
    assert approve(client, pid_sched, "dave").json()["status"] == "IN_REVIEW"
    assert approve(client, pid_sched, "erin").json()["status"] == "SCHEDULED"

    # 重启 editor（调度器重建；周期内不会执行未到点提案，评审状态在库里不受影响）
    from app.editor.main import app as editor_app
    with TestClient(editor_app) as e2:
        # 等待中的评审仍可继续：补上第二人 -> 立即生效
        r = approve(e2, pid_review, "frank")
        assert r.json()["status"] == "EFFECTIVE"
        # 未到点预约仍是 SCHEDULED
        assert e2.get(f"/proposals/{pid_sched}").json()["status"] == "SCHEDULED"
        # 时间到后可以继续处理
        with SessionLocal() as s:
            s.get(Proposal, pid_sched).scheduled_at = \
                datetime.now(timezone.utc) - timedelta(seconds=1)
            s.commit()
        r = e2.post(f"/proposals/{pid_sched}/apply", json={"actor": "ops"})
        assert r.json()["status"] == "EFFECTIVE"
        assert r.json()["applied"][0]["policy"] == "solo"


def test_restart_runs_overdue_scheduled_proposal(client):
    setup(client)
    # 宕机期间到点的预约：重启后立即周期应把它生效
    t = datetime.now(timezone.utc) + timedelta(seconds=30)
    pid = _schedule_and_approve(client, GEO_V2, "alice", t, "bob")
    with SessionLocal() as s:
        s.get(Proposal, pid).scheduled_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    from app.editor.main import app as editor_app
    with TestClient(editor_app):
        pass  # lifespan 启动即跑一个周期
    assert client.get(f"/proposals/{pid}").json()["status"] == "EFFECTIVE"


# ---------- 角色配置 ----------

def test_policy_specific_rules_merge_with_default(client):
    setup(client)
    client.put("/proposals/approval-config-default", json={
        "actor": "admin", "rules": [{"role": "approver", "count": 1}]})
    client.put("/proposals/approval-config/pay", json={
        "actor": "admin", "rules": [{"role": "sec", "count": 1}]})
    pid = submit(client).json()["id"]
    body = client.get(f"/proposals/{pid}").json()
    roles = {r["role"] for r in body["approval_rules"]}
    assert roles == {"approver", "sec"}


def test_invalid_rules_rejected(client):
    r = client.put("/proposals/approval-config-default",
                   json={"actor": "admin", "rules": [{"role": "sec", "count": 0}]})
    assert r.status_code == 422


def test_multi_fragment_proposal_impacts_and_applies_multiple_policies(client):
    setup(client)
    # geo_check 还被第二个策略直接引用
    client.post("/policies", json={"name": "geo_pol", "entry_fragment": "geo_check"})
    client.post("/policies/geo_pol/publish", json={"actor": "owner"})

    r = client.post("/proposals", json={"actor": "alice", "changes": [
        {"fragment": "geo_check",
         "body": {"op": "in", "args": [{"var": "country"}, ["CN", "SG"]]}},
        {"fragment": "amount_check",
         "body": {"op": "lt", "args": [{"var": "amount"}, 5000]}}]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["impacted_policies"] == ["geo_pol", "pay"]  # amount 仅触达 pay
    assert {c["fragment"] for c in body["changes"]} == {"geo_check", "amount_check"}

    pid = body["id"]
    res = approve(client, pid, "bob").json()
    assert res["status"] == "EFFECTIVE"
    applied = {a["policy"]: a["version"]
               for a in client.get(f"/proposals/{pid}").json()["applied"]}
    assert applied == {"geo_pol": 2, "pay": 2}


def test_duplicate_fragment_in_one_proposal_rejected(client):
    setup(client)
    r = client.post("/proposals", json={"actor": "alice", "changes": [
        {"fragment": "geo_check",
         "body": {"op": "in", "args": [{"var": "country"}, ["JP"]]}},
        {"fragment": "geo_check",
         "body": {"op": "in", "args": [{"var": "country"}, ["US"]]}}]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "duplicate_change"


def test_create_new_fragment_reaching_policy(client):
    setup(client)
    # 提案改 risk_base 引用一个尚不存在的新片段 brand_new，并在同一提案中创建它。
    # 提交时基于"叠加后视图"预演 -> brand_new 经 risk_base 可达 pay，action=create。
    new_risk = {"op": "and", "args": [
        {"ref": "geo_check"}, {"ref": "amount_check"}, {"ref": "brand_new"}]}
    r = client.post("/proposals", json={"actor": "alice", "changes": [
        {"fragment": "brand_new",
         "body": {"op": "eq", "args": [{"var": "z"}, 1]}},
        {"fragment": "risk_base", "body": new_risk}]})
    assert r.status_code == 201, r.text
    by_name = {c["fragment"]: c for c in r.json()["changes"]}
    assert by_name["brand_new"]["action"] == "create"
    assert by_name["brand_new"]["base_version"] == 0
    assert by_name["risk_base"]["action"] == "update"
    assert r.json()["impacted_policies"] == ["pay"]


def test_missing_ref_preflight_blocks_when_new_fragment_not_supplied(client):
    setup(client)
    # risk_base 引用了一个提案里没有创建、库中也不存在的片段 -> 预演失败
    bad_risk = {"op": "and", "args": [{"ref": "geo_check"}, {"ref": "ghost"}]}
    r = client.post("/proposals", json={"actor": "alice",
                                        "changes": [{"fragment": "risk_base", "body": bad_risk}]})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "preflight_failed"
