"""资源消耗台账与周期限额（quota :8005）测试。

覆盖需求逐项：
- 采集去重（重投消除）与乱序接纳（凭证/放弃先于占用）
- 账户时区批次、跨周期销账/归还回到产生周期
- 并发抢占不超限额；共享池 vs 独立设限
- 占用->销账（差额补退）唯一生效；abort/超时归还
- 封账只读账页（DB 触发器拦截改删）、晚到挂起、财务补/冲账
- 限额变更从选定批次起算、已封账不重算
- 三组件分开启动、崩溃恢复、文件台采集
- explain / trace 可解释、可沿流水串联全过程
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.common import quota_service as qs, quota_worker as worker
from app.common.db import SessionLocal, engine
from app.common.models import (QuotaHold, QuotaInboxEvent, QuotaSettlement)
from app.quota.main import app

SH = timezone(timedelta(hours=8))


def at(y, m, d, hh=12, mm=0, ss=0, tz=timezone.utc):
    return datetime(y, m, d, hh, mm, ss, tzinfo=tz)


@pytest.fixture
def svc():
    """标准账户：上海时区；r1 独立限额 100，共享池规则 rs1/rs2 共享 200。"""
    with SessionLocal() as s:
        qs.create_account(s, "acct", "Asia/Shanghai", "op")
        qs.register_rule(s, "acct", "r1", "dedicated", initial_limit=100)
        qs.register_rule(s, "acct", "rs1", "shared", initial_limit=200)
        qs.register_rule(s, "acct", "rs2", "shared")
        s.commit()
    return "acct"


def submit(s, serial, etype, rule, when, payload, account="acct"):
    return qs.submit_event(s, serial=serial, event_type=etype, account=account,
                           rule_name=rule, occurred_at=when, payload=payload)


def drain(s, now=None):
    """同步把所有 NEW 事件按 gate/accountant 处理完（乱序重试多轮）。"""
    for _ in range(10):
        progressed = False
        for ev in s.query(QuotaInboxEvent).filter_by(status="NEW").order_by(
                QuotaInboxEvent.id).all():
            if ev.event_type == "hold":
                qs.process_hold_event(s, ev, now)
                progressed = True
            elif ev.event_type == "abort":
                r = qs.process_abort_event(s, ev, now)
                progressed |= r != "AWAIT_HOLD"
            else:
                r = qs.process_voucher_event(s, ev, now)
                progressed |= r["result"] != "AWAIT_HOLD"
            s.commit()
        progressed |= qs.sweep_pending_vouchers(s, now)["scanned"] > 0
        s.commit()
        if not progressed:
            break


# ---------------------------------------------------------------------------
# 基础：开户 / 注册 / 共享与独立限额
# ---------------------------------------------------------------------------

def test_account_validation():
    with SessionLocal() as s:
        with pytest.raises(qs.QuotaError) as e:
            qs.create_account(s, "bad", "Mars/Olympus")
        assert e.value.code == "unknown_timezone"


def test_rule_register_idempotent_and_mode_conflict(svc):
    with SessionLocal() as s:
        r1 = qs.register_rule(s, "acct", "r1", "dedicated")
        r2 = qs.register_rule(s, "acct", "r1", "dedicated")
        assert r1["id"] == r2["id"]
        with pytest.raises(qs.QuotaError) as e:
            qs.register_rule(s, "acct", "r1", "shared")
        assert e.value.code == "rule_mode_conflict"


def test_shared_pool_rules_share_one_limit(svc):
    with SessionLocal() as s:
        submit(s, "a", "hold", "rs1", at(2026, 9, 16, 4), {"amount": 150})
        submit(s, "b", "hold", "rs2", at(2026, 9, 16, 4), {"amount": 60})
        s.commit()
        drain(s, at(2026, 9, 16, 4))
        ha = s.query(QuotaHold).filter_by(serial="a").one()
        hb = s.query(QuotaHold).filter_by(serial="b").one()
        # 共享池 200：150 通过，再要 60 => 210 > 200 拒绝
        assert (ha.status, hb.status) == ("HELD", "REJECTED")
        assert hb.reject_reason == "quota_exceeded"


def test_dedicated_rules_have_independent_limits(svc):
    with SessionLocal() as s:
        qs.register_rule(s, "acct", "r9", "dedicated", initial_limit=5)
        s.commit()
        submit(s, "a", "hold", "r1", at(2026, 9, 16, 4), {"amount": 100})
        submit(s, "b", "hold", "r9", at(2026, 9, 16, 4), {"amount": 6})
        s.commit()
        drain(s, at(2026, 9, 16, 4))
        assert s.query(QuotaHold).filter_by(serial="a").one().status == "HELD"
        hb = s.query(QuotaHold).filter_by(serial="b").one()
        assert (hb.status, hb.reject_reason) == ("REJECTED", "quota_exceeded")


def test_hold_without_limit_is_rejected(svc):
    with SessionLocal() as s:
        qs.register_rule(s, "acct", "nolim", "dedicated")
        s.commit()
        submit(s, "x", "hold", "nolim", at(2026, 9, 16, 4), {"amount": 1})
        s.commit()
        drain(s, at(2026, 9, 16, 4))
        h = s.query(QuotaHold).filter_by(serial="x").one()
        assert (h.status, h.reject_reason) == ("REJECTED", "no_quota_limit")


# ---------------------------------------------------------------------------
# 采集：重投消除 + 内容冲突
# ---------------------------------------------------------------------------

def test_duplicate_submission_is_idempotent(svc):
    with SessionLocal() as s:
        r1 = submit(s, "dup1", "voucher", "r1", at(2026, 9, 16, 4),
                    {"amount": 3})
        r2 = submit(s, "dup1", "voucher", "r1", at(2026, 9, 16, 4),
                    {"amount": 3})
        s.commit()
        assert r1["duplicate"] is False and r2["duplicate"] is True
        assert s.query(QuotaInboxEvent).filter_by(serial="dup1").count() == 1
        # 相同流水号不同事件类型是合法的不同事件
        r3 = submit(s, "dup1", "hold", "r1", at(2026, 9, 16, 4),
                    {"amount": 3})
        s.commit()
        assert r3["duplicate"] is False


def test_same_serial_different_payload_conflicts(svc):
    with SessionLocal() as s:
        submit(s, "dup2", "voucher", "r1", at(2026, 9, 16, 4), {"amount": 3})
        s.commit()
        with pytest.raises(qs.QuotaError) as e:
            submit(s, "dup2", "voucher", "r1", at(2026, 9, 16, 4),
                   {"amount": 4})
        assert e.value.code == "serial_conflict"


def test_naive_datetime_rejected(svc):
    with SessionLocal() as s:
        with pytest.raises(qs.QuotaError) as e:
            submit(s, "z", "voucher", "r1", datetime(2026, 9, 16, 4),
                   {"amount": 1})
        assert e.value.code == "naive_datetime"


# ---------------------------------------------------------------------------
# 乱序：凭证先到 / 放弃先到，后续周期补配
# ---------------------------------------------------------------------------

def test_voucher_before_hold_settles_on_later_cycle(svc):
    with SessionLocal() as s:
        # 凭证先到（占用事件还没来）
        submit(s, "oo1", "voucher", "r1", at(2026, 9, 16, 4), {"amount": 30})
        s.commit()
        ev = s.query(QuotaInboxEvent).filter_by(serial="oo1").one()
        r = qs.process_voucher_event(s, ev, at(2026, 9, 16, 4))
        s.commit()
        assert r["result"] == "AWAIT_HOLD"

        # 占用后到，下一个核算周期完成配对销账
        submit(s, "oo1", "hold", "r1", at(2026, 9, 16, 3, 59),
               {"amount": 40})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        st = s.query(QuotaSettlement).filter_by(serial="oo1").one()
        assert (st.held_amount, st.actual_amount, st.delta_amount) == (40, 30, -10)
        assert s.query(QuotaHold).filter_by(serial="oo1").one().status == "SETTLED"


def test_abort_before_hold_is_retried_then_releases(svc):
    with SessionLocal() as s:
        submit(s, "oo2", "abort", "r1", at(2026, 9, 16, 5), {})
        s.commit()
        ev = s.query(QuotaInboxEvent).filter_by(serial="oo2").one()
        assert qs.process_abort_event(s, ev, at(2026, 9, 16, 5)) == "AWAIT_HOLD"
        s.commit()
        assert ev.status == "NEW"  # 退回队列

        submit(s, "oo2", "hold", "r1", at(2026, 9, 16, 4), {"amount": 20})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        h = s.query(QuotaHold).filter_by(serial="oo2").one()
        assert h.status == "RELEASED"


def test_abort_after_settle_is_noop(svc):
    with SessionLocal() as s:
        submit(s, "oo3", "hold", "r1", at(2026, 9, 16, 4), {"amount": 20})
        submit(s, "oo3", "voucher", "r1", at(2026, 9, 16, 4, 1),
               {"amount": 20})
        submit(s, "oo3", "abort", "r1", at(2026, 9, 16, 4, 2), {})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        assert s.query(QuotaSettlement).filter_by(serial="oo3").count() == 1
        assert s.query(QuotaHold).filter_by(serial="oo3").one().status == "SETTLED"


# ---------------------------------------------------------------------------
# 时区批次 + 跨周期
# ---------------------------------------------------------------------------

def test_batch_uses_account_timezone(svc):
    with SessionLocal() as s:
        # UTC 9/16 16:30 == 上海 9/17 00:30
        submit(s, "tz1", "hold", "r1", at(2026, 9, 16, 16, 30),
               {"amount": 10, "ttl_s": 60})
        s.commit()
        drain(s, at(2026, 9, 16, 16, 31))
        h = s.query(QuotaHold).filter_by(serial="tz1").one()
        assert h.batch_date == "2026-09-17"


def test_cross_period_settlement_lands_on_voucher_day(svc):
    with SessionLocal() as s:
        # 占用产生于上海 9/16 晚，真实消耗发生在 9/17 凌晨
        submit(s, "cp1", "hold", "r1", at(2026, 9, 16, 15), {"amount": 50})
        submit(s, "cp1", "voucher", "r1", at(2026, 9, 16, 16, 30),
               {"amount": 70})
        s.commit()
        drain(s, at(2026, 9, 16, 17))
        h = s.query(QuotaHold).filter_by(serial="cp1").one()
        st = s.query(QuotaSettlement).filter_by(serial="cp1").one()
        assert h.batch_date == "2026-09-16"
        assert st.batch_date == "2026-09-17"
        assert st.origin_batch_date == "2026-09-16"
        assert st.delta_amount == 20  # 真实 70 > 预估 50，追加占用 20


def test_timeout_release_returns_to_origin_period(svc):
    with SessionLocal() as s:
        submit(s, "cp2", "hold", "r1", at(2026, 9, 16, 15),
               {"amount": 40, "ttl_s": 60})
        s.commit()
        drain(s, at(2026, 9, 16, 15))
        h = s.query(QuotaHold).filter_by(serial="cp2").one()
        assert h.batch_date == "2026-09-16"
        # 封账（未结占用进账页 open_hold）
        page = qs.seal_batch(s, "acct", "2026-09-16", "fin",
                             now=at(2026, 9, 17, 0))
        s.commit()
        types = [ln["type"] for ln in page["page"]["lines"] if ln["serial"] == "cp2"]
        assert types == ["open_hold"]
        # 两个周期后才超时回收：归还必须回到 9/16
        rels = qs.reap_expired(s, now=at(2026, 9, 18, 12))
        s.commit()
        assert len(rels) == 1 and rels[0]["batch_date"] == "2026-09-16"
        assert s.query(QuotaHold).filter_by(serial="cp2").one().status == "RELEASED"
        page2 = qs.get_page(s, "acct", "2026-09-16")
        assert page2["lines"] == page["page"]["lines"]  # 账页原文未动
        after = page2["post_seal_appendix"]["releases_after_seal"]
        assert [r["serial"] for r in after] == ["cp2"]


# ---------------------------------------------------------------------------
# 并发抢占：总量绝不超上限
# ---------------------------------------------------------------------------

def test_concurrent_holds_never_exceed_limit(svc):
    results = []

    def worker_fn(i):
        # 先经采集箱提交（与业务侧一致）
        with SessionLocal() as s:
            qs.submit_event(s, serial=f"c{i:03d}", event_type="hold",
                            account="acct", rule_name="r1",
                            occurred_at=at(2026, 9, 16, 4),
                            payload={"amount": 10})
            s.commit()
        # gate 工作器：原子认领 -> IMMEDIATE 事务门禁判定
        with SessionLocal() as s:
            ev = qs.claim_next_event(s, ("hold",), f"w{i}")
            assert ev is not None
            h = qs.process_hold_event(s, ev, at(2026, 9, 16, 4))
            s.commit()
            results.append((h["status"], h["amount"]))

    threads = [threading.Thread(target=worker_fn, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    admitted = [a for st, a in results if st == "HELD"]
    rejected_status = {st for st, _ in results if st == "REJECTED"}
    assert sum(admitted) == 100          # 限额 100 恰好占满
    assert len(admitted) == 10
    assert rejected_status == {"REJECTED"}
    assert len(rejected_status) == 1 and len(results) == 20
    with SessionLocal() as s:
        assert all(h.reject_reason == "quota_exceeded"
                   for h in s.query(QuotaHold)
                   .filter_by(status="REJECTED").all())


# ---------------------------------------------------------------------------
# 销账：差额补退 + 多次销账只生效一次
# ---------------------------------------------------------------------------

def test_settle_refund_and_single_effect(svc):
    with SessionLocal() as s:
        submit(s, "u1", "hold", "r1", at(2026, 9, 16, 4), {"amount": 80})
        s.commit()
        drain(s, at(2026, 9, 16, 4))
        submit(s, "u1", "voucher", "r1", at(2026, 9, 16, 4, 5),
               {"amount": 30})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        st = s.query(QuotaSettlement).filter_by(serial="u1").one()
        assert st.delta_amount == -50  # 退回 50
        # 余额重新可用：80 占用中 50 已退回
        usage = qs.explain_usage(s, "acct", "r1", "2026-09-16")
        sc = usage["scopes"][0]
        assert sc["settled"]["amount"] == 30
        assert sc["available"] == 70

        # 重复处理同一凭证（重投/重放）：不产生第二条销账
        ev = s.query(QuotaInboxEvent).filter_by(serial="u1",
                                                event_type="voucher").one()
        qs.process_voucher_event(s, ev, at(2026, 9, 16, 6))
        s.commit()
        assert s.query(QuotaSettlement).filter_by(serial="u1").count() == 1


def test_settle_after_release_records_full_amount_once(svc):
    """凭证与超时回收竞争落败：占用已归还，凭证按全额补记一次。"""
    with SessionLocal() as s:
        submit(s, "u2", "hold", "r1", at(2026, 9, 16, 4),
               {"amount": 30, "ttl_s": 10})
        s.commit()
        drain(s, at(2026, 9, 16, 4))
        qs.reap_expired(s, now=at(2026, 9, 16, 4, 0, 11))
        s.commit()
        submit(s, "u2", "voucher", "r1", at(2026, 9, 16, 4, 0, 12),
               {"amount": 30})
        s.commit()
        drain(s, at(2026, 9, 16, 4, 0, 12))
        st = s.query(QuotaSettlement).filter_by(serial="u2").one()
        assert (st.held_amount, st.actual_amount) == (0, 30)
        # 再处理一次同一凭证（重投/重放）：不产生第二条销账
        ev = s.query(QuotaInboxEvent).filter_by(
            serial="u2", event_type="voucher").one()
        qs.process_voucher_event(s, ev)
        s.commit()
        assert s.query(QuotaSettlement).filter_by(serial="u2").count() == 1


# ---------------------------------------------------------------------------
# 封账 + 晚到凭证 + 财务补/冲账
# ---------------------------------------------------------------------------

def test_seal_is_idempotent_and_append_only(svc):
    from sqlalchemy import text
    with SessionLocal() as s:
        submit(s, "p1", "hold", "r1", at(2026, 9, 16, 4), {"amount": 10})
        submit(s, "p1", "voucher", "r1", at(2026, 9, 16, 4, 1),
               {"amount": 10})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        p1 = qs.seal_batch(s, "acct", "2026-09-16", "fin",
                           now=at(2026, 9, 17, 0))
        s.commit()
        assert p1["sealed"] is True
        p2 = qs.seal_batch(s, "acct", "2026-09-16", "fin",
                           now=at(2026, 9, 17, 0))
        assert p2["duplicate"] is True and p2["page"]["page_id"] == \
            p1["page"]["page_id"]

    # 数据库级只读：对有数据的账页/账页行/结算 UPDATE/DELETE 均被拒
    with engine.connect() as c:
        for sql in ("UPDATE quota_pages SET created_by='hacker'",
                    "UPDATE quota_page_lines SET amount=1 WHERE line_no>=1",
                    "UPDATE quota_settlements SET actual_amount=0 WHERE 1=1"):
            with pytest.raises(Exception):
                c.execute(text(sql))
                c.commit()
            c.rollback()


def test_sealed_batch_rejects_new_hold(svc):
    with SessionLocal() as s:
        qs.seal_batch(s, "acct", "2026-09-16", "fin", now=at(2026, 9, 17, 0))
        s.commit()
        submit(s, "latehold", "hold", "r1", at(2026, 9, 16, 4),
               {"amount": 1})
        s.commit()
        drain(s, at(2026, 9, 17, 1))
        h = s.query(QuotaHold).filter_by(serial="latehold").one()
        assert (h.status, h.reject_reason) == ("REJECTED", "batch_sealed")


def test_late_voucher_suspended_then_supplement(svc):
    with SessionLocal() as s:
        submit(s, "lv1", "hold", "r1", at(2026, 9, 16, 4), {"amount": 20})
        submit(s, "lv1", "voucher", "r1", at(2026, 9, 16, 4, 1),
               {"amount": 20})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        before = qs.seal_batch(s, "acct", "2026-09-16", "fin",
                               now=at(2026, 9, 17, 0))
        s.commit()

        # 封账后到达、发生时刻却属于 9/16 的凭证：挂起
        submit(s, "lv2", "voucher", "r1", at(2026, 9, 16, 10),
               {"amount": 15})
        s.commit()
        drain(s, at(2026, 9, 17, 2))
        adj = s.query(qs.QuotaAdjustment).filter_by(serial="lv2").one()
        assert adj.status == "PENDING"
        pending = qs.explain_usage(s, "acct", "r1", "2026-09-16")
        assert pending["scopes"][0]["adjustments_pending"]["amount"] == 15

        # 财务确认补账：原账页不变，追加补充分录
        out = qs.decide_adjustment(s, adj.id, "confirm", "fin", amount=15)
        s.commit()
        assert out["entry"]["kind"] == "supplement"
        after = qs.get_page(s, "acct", "2026-09-16")
        assert after["lines"] == before["page"]["lines"]
        assert after["snapshot"] == before["page"]["snapshot"]
        ents = after["post_seal_appendix"]["adjustment_entries"]
        assert [(e["serial"], e["amount"], e["kind"]) for e in ents] == \
            [("lv2", 15, "supplement")]

        # 已确认的调整不可重复确认
        with pytest.raises(qs.QuotaError) as e:
            qs.decide_adjustment(s, adj.id, "confirm", "fin", amount=15)
        assert e.value.code == "adjustment_not_pending"


def test_finance_reversal_cannot_exceed_supplement(svc):
    with SessionLocal() as s:
        submit(s, "rv1", "voucher", "r1", at(2026, 9, 16, 4),
               {"amount": 10})
        s.commit()
        qs.seal_batch(s, "acct", "2026-09-16", "fin", now=at(2026, 9, 17, 0))
        s.commit()
        drain(s, at(2026, 9, 17, 1))
        adj = s.query(qs.QuotaAdjustment).filter_by(serial="rv1").one()
        with pytest.raises(qs.QuotaError) as e:
            qs.decide_adjustment(s, adj.id, "confirm", "fin", amount=-30)
        assert e.value.code == "reversal_exceeds_supplement"
        s.rollback()
        # 财务驳回：不产生分录
        out = qs.decide_adjustment(s, adj.id, "reject", "fin", note="duplicate")
        s.commit()
        assert out["entry"] is None
        assert s.query(qs.QuotaAdjustmentEntry).count() == 0


def test_pending_voucher_at_seal_gets_suspended(svc):
    with SessionLocal() as s:
        # 凭证先到且始终没有配对占用，封账时一并挂起
        submit(s, "pv1", "voucher", "r1", at(2026, 9, 16, 4), {"amount": 9})
        s.commit()
        ev = s.query(QuotaInboxEvent).filter_by(serial="pv1").one()
        qs.process_voucher_event(s, ev, at(2026, 9, 16, 4))
        s.commit()
        qs.seal_batch(s, "acct", "2026-09-16", "fin", now=at(2026, 9, 17, 0))
        s.commit()
        adj = s.query(qs.QuotaAdjustment).filter_by(serial="pv1").one()
        assert adj.reason == "late_voucher"
        assert adj.detail["sealed_while_pending"] is True


# ---------------------------------------------------------------------------
# 限额变更：选定批次起算，封账批次不重算
# ---------------------------------------------------------------------------

def test_limit_change_applies_from_chosen_batch(svc):
    with SessionLocal() as s:
        # 9/15 批次先封账（限额 100）
        submit(s, "d1", "hold", "r1", at(2026, 9, 15, 4), {"amount": 100})
        submit(s, "d1", "voucher", "r1", at(2026, 9, 15, 4, 1),
               {"amount": 100})
        s.commit()
        drain(s, at(2026, 9, 15, 5))
        old_page = qs.seal_batch(s, "acct", "2026-09-15", "fin",
                                 now=at(2026, 9, 16, 0))
        s.commit()
        assert old_page["page"]["snapshot"]["scopes"][0]["limit_amount"] == 100

        # 限额降到 60，从 9/17 起；9/16 仍按 100
        qs.set_limit(s, "acct", "r1", 60, effective_from="2026-09-17",
                     actor="boss")
        s.commit()
        submit(s, "d2", "hold", "r1", at(2026, 9, 16, 4), {"amount": 80})
        s.commit()
        drain(s, at(2026, 9, 16, 4))
        assert s.query(QuotaHold).filter_by(serial="d2").one().status == "HELD"

        submit(s, "d3", "hold", "r1", at(2026, 9, 17, 4), {"amount": 70})
        s.commit()
        drain(s, at(2026, 9, 17, 4))
        h = s.query(QuotaHold).filter_by(serial="d3").one()
        assert (h.status, h.reject_reason) == ("REJECTED", "quota_exceeded")

        # 另一作用域（独立规则）：其 9/16 批次封账后，限额变更不得选已封账批次
        qs.register_rule(s, "acct", "rX", "dedicated", initial_limit=1000)
        s.commit()
        submit(s, "dx", "hold", "rX", at(2026, 9, 16, 5), {"amount": 5})
        submit(s, "dx", "voucher", "rX", at(2026, 9, 16, 5, 1),
               {"amount": 5})
        s.commit()
        drain(s, at(2026, 9, 16, 6))
        qs.seal_batch(s, "acct", "2026-09-16", "fin", now=at(2026, 9, 17, 0))
        s.commit()
        with pytest.raises(qs.QuotaError) as e:
            qs.set_limit(s, "acct", "rX", 10, effective_from="2026-09-16")
        assert e.value.code == "effective_batch_sealed"
        s.rollback()
        # r1 的生效日必须晚于上一个版本
        with pytest.raises(qs.QuotaError) as e:
            qs.set_limit(s, "acct", "r1", 50, effective_from="2026-09-10")
        assert e.value.code == "effective_from_order"
        s.rollback()

        # 9/15 旧账页没有被重算
        again = qs.get_page(s, "acct", "2026-09-15")
        assert again["snapshot"]["scopes"][0]["limit_amount"] == 100


# ---------------------------------------------------------------------------
# 自动封账（本地日已过的 OPEN 批次）
# ---------------------------------------------------------------------------

def test_auto_seal_uses_each_account_local_day(svc):
    with SessionLocal() as s:
        qs.create_account(s, "acct-utc", "UTC", "op")
        qs.register_rule(s, "acct-utc", "r1", "dedicated", initial_limit=100)
        s.commit()
        # 上海账户：9/15、9/16 两个批次都有数据
        submit(s, "x1", "hold", "r1", at(2026, 9, 15, 4), {"amount": 5})
        submit(s, "x1", "voucher", "r1", at(2026, 9, 15, 4, 1), {"amount": 5})
        submit(s, "x2", "hold", "r1", at(2026, 9, 16, 4), {"amount": 5})
        # UTC 账户只有 9/15 批次
        submit(s, "u1", "hold", "r1", at(2026, 9, 15, 4), {"amount": 5},
               account="acct-utc")
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        # 此刻=上海 9/17 00:30 / UTC 9/16 16:30：
        #   上海账户的 9/15、9/16 都应封；UTC 账户的 9/16 尚无批次、9/15 封
        sealed = qs.auto_seal_due(s, now=at(2026, 9, 16, 16, 30))
        s.commit()
        acct_dates = sorted(p["page"]["batch_date"] for p in sealed
                            if p["page"]["account"] == "acct")
        utc_dates = sorted(p["page"]["batch_date"] for p in sealed
                           if p["page"]["account"] == "acct-utc")
        assert acct_dates == ["2026-09-15", "2026-09-16"]
        assert utc_dates == ["2026-09-15"]


# ---------------------------------------------------------------------------
# 工作器：三组件分开启动、认领互斥、崩溃恢复、文件台
# ---------------------------------------------------------------------------

def test_worker_split_roles_and_recovery(svc, monkeypatch):
    monkeypatch.setenv("QUOTA_WORKER_INTERVAL_MS", "600000")
    with SessionLocal() as s:
        submit(s, "w1", "hold", "r1", at(2026, 9, 16, 4), {"amount": 10})
        submit(s, "w1", "voucher", "r1", at(2026, 9, 16, 4, 1),
               {"amount": 10})
        s.commit()
        # 模拟 gate 进程崩溃：hold 事件停在 PROCESSING
        ev = s.query(QuotaInboxEvent).filter_by(event_type="hold").one()
        ev.status = "PROCESSING"
        ev.claimed_by = "dead-gate"
        s.commit()

    # 只有 accountant 的进程：凭证事件可被接收（配对未完成，不产生销账/占用）
    r = worker.accountant_tick("acc-test")
    assert r["processed"] == 1 and r["settled"] == 0
    with SessionLocal() as s:
        assert s.query(QuotaHold).filter_by(serial="w1").count() == 0
        assert s.query(QuotaSettlement).filter_by(serial="w1").count() == 0

    # gate 新进程启动恢复 PROCESSING -> NEW，随后 gate 周期完成占用
    with SessionLocal() as s:
        assert qs.recover_processing(s, "gate-restart") == 1
        s.commit()
    g = worker.gate_tick("gate-test")
    assert g["admitted"] == 1
    # accountant 再跑：扫描重试完成配对销账
    r = worker.accountant_tick("acc-test")
    assert r["sweep"]["settled"] == 1
    with SessionLocal() as s:
        assert s.query(QuotaSettlement).filter_by(serial="w1").count() == 1


def test_claim_is_mutually_exclusive(svc):
    with SessionLocal() as s:
        for i in range(5):
            submit(s, f"m{i}", "hold", "r1", at(2026, 9, 16, 4),
                   {"amount": 1})
        s.commit()
        claimed = []

        def grab(wid):
            with SessionLocal() as s2:
                ev = qs.claim_next_event(s2, ("hold", "voucher", "abort"), wid)
                claimed.append((wid, ev.serial if ev else None))

        threads = [threading.Thread(target=grab, args=(f"w{i}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        serials = [ser for _, ser in claimed if ser]
        assert len(serials) == len(set(serials)) == 5


def test_spool_collector(svc, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTA_SPOOL_DIR", str(tmp_path / "spool"))
    incoming = tmp_path / "spool" / "incoming"
    incoming.mkdir(parents=True)
    doc = {"serial": "sp1", "event_type": "voucher", "account": "acct",
           "rule_name": "r1",
           "occurred_at": at(2026, 9, 16, 4).isoformat(),
           "payload": {"amount": 7}}
    (incoming / "sp1.json").write_text(json.dumps(doc), encoding="utf-8")
    r = worker.collector_tick("col-test")
    assert r["ingested"] == 1
    with SessionLocal() as s:
        assert s.query(QuotaInboxEvent).filter_by(serial="sp1").count() == 1

    # 重投同名文件（重发）：采集箱判重，不重复入账
    (incoming / "sp1-resend.json").write_text(json.dumps(doc), encoding="utf-8")
    r = worker.collector_tick("col-test")
    assert r["duplicates"] == 1
    with SessionLocal() as s:
        assert s.query(QuotaInboxEvent).filter_by(serial="sp1").count() == 1

    # 坏文件进 .bad，不毒化后续文件
    (incoming / "bad.json").write_text("{not json", encoding="utf-8")
    doc2 = dict(doc, serial="sp2")
    (incoming / "sp2.json").write_text(json.dumps(doc2), encoding="utf-8")
    r = worker.collector_tick("col-test")
    assert r["ingested"] == 1
    processing = tmp_path / "spool" / "processing"
    assert any(n.startswith("bad.json.bad:") for n in os.listdir(processing))


# ---------------------------------------------------------------------------
# 解释 / 追踪
# ---------------------------------------------------------------------------

def test_explain_usage_breaks_down_sources(svc):
    with SessionLocal() as s:
        submit(s, "e1", "hold", "r1", at(2026, 9, 16, 4), {"amount": 30})
        submit(s, "e2", "hold", "r1", at(2026, 9, 16, 4), {"amount": 20})
        submit(s, "e2", "voucher", "r1", at(2026, 9, 16, 4, 1),
               {"amount": 20})
        s.commit()
        drain(s, at(2026, 9, 16, 5))
        u = qs.explain_usage(s, "acct", "r1", "2026-09-16")["scopes"][0]
        assert u["limit"]["amount"] == 100
        assert u["held"] == {"amount": 30, "serials": ["e1"]}
        assert u["settled"] == {"amount": 20, "serials": ["e2"]}
        assert u["available"] == 50
        assert u["adjustments_pending"] == {"count": 0, "amount": 0,
                                           "serials": [], "adjustment_ids": []}


def test_trace_serial_full_chain(svc):
    with SessionLocal() as s:
        submit(s, "tr1", "hold", "r1", at(2026, 9, 16, 15),
               {"amount": 20, "ttl_s": 60})
        submit(s, "tr1", "voucher", "r1", at(2026, 9, 16, 16, 30),
               {"amount": 20})
        s.commit()
        drain(s, at(2026, 9, 16, 17))
        # tr1 销账发生在上海 9/17（跨周期），进入 9/17 账页
        qs.seal_batch(s, "acct", "2026-09-16", "fin", now=at(2026, 9, 17, 0))
        qs.seal_batch(s, "acct", "2026-09-17", "fin", now=at(2026, 9, 18, 0))
        s.commit()
        submit(s, "tr2", "voucher", "r1", at(2026, 9, 16, 10),
               {"amount": 5})
        s.commit()
        drain(s, at(2026, 9, 17, 1))
        adj = s.query(qs.QuotaAdjustment).filter_by(serial="tr2").one()
        qs.decide_adjustment(s, adj.id, "confirm", "fin", amount=5)
        s.commit()

        chain = qs.trace_serial(s, "tr2")
        stages = [(x["stage"], x["label"]) for x in chain["timeline"]]
        assert any(st == "settle" and "采集端接收消耗凭证" in lb
                   for st, lb in stages)
        assert any(st == "adjust_pending" for st, _ in stages)
        assert any(st == "adjust_posted" and "补账" in lb for st, lb in stages)
        assert all(st != "page" for st, _ in stages)  # 晚到凭证不进原账页

        chain1 = qs.trace_serial(s, "tr1")
        st = [x["stage"] for x in chain1["timeline"]]
        assert st == ["ingest", "hold", "settle", "settle", "page"]
        settle = next(x for x in chain1["timeline"]
                      if x["stage"] == "settle" and "cross_period" in x)
        assert settle["cross_period"] is True


# ---------------------------------------------------------------------------
# HTTP 端到端（三组件经 /quota/tick 手动驱动，便于断言顺序）
# ---------------------------------------------------------------------------

def test_api_end_to_end(svc):
    with TestClient(app) as c:
        # 重投消除
        body = {"serial": "h1", "account": "acct", "rule_name": "r1",
                "occurred_at": at(2026, 9, 16, 4).isoformat(), "amount": 60}
        assert c.post("/quota/holds", json=body).json()["duplicate"] is False
        assert c.post("/quota/holds", json=body).json()["duplicate"] is True

        r = c.post("/quota/tick/gate").json()
        assert r["admitted"] == 1
        assert c.get("/quota/holds", params={"serial": "h1"}).json()[0][
            "status"] == "HELD"

        c.post("/quota/vouchers", json={
            "serial": "h1", "account": "acct", "rule_name": "r1",
            "occurred_at": at(2026, 9, 16, 4, 1).isoformat(), "amount": 40})
        r = c.post("/quota/tick/accountant").json()
        assert r["settled"] == 1

        u = c.get("/quota/usage/acct",
                  params={"scope_rule": "r1",
                          "batch_date": "2026-09-16"}).json()
        assert u["scopes"][0]["available"] == 60

        # trace 可查
        tr = c.get("/quota/trace/h1").json()
        assert {x["stage"] for x in tr["timeline"]} >= {"ingest", "hold",
                                                        "settle"}
        assert c.get("/quota/trace/unknown").status_code == 404

        # 封账后晚到凭证 -> 挂起 -> 财务补账
        c.post("/quota/batches/seal", json={
            "account": "acct", "batch_date": "2026-09-16"})
        page = c.get("/quota/batches/acct/2026-09-16/page").json()
        assert page["immutable"] is True
        c.post("/quota/vouchers", json={
            "serial": "h2", "account": "acct", "rule_name": "r1",
            "occurred_at": at(2026, 9, 16, 6).isoformat(), "amount": 7})
        c.post("/quota/tick/accountant")
        adj = c.get("/quota/adjustments", params={"status": "PENDING"}).json()
        assert [a["serial"] for a in adj] == ["h2"]
        dec = c.post(f"/quota/adjustments/{adj[0]['id']}/decision",
                     json={"decision": "confirm", "amount": 7})
        assert dec.json()["entry"]["kind"] == "supplement"

        # 审计留痕
        audits = c.get("/audit", params={"service": "quota",
                                         "limit": 200}).json()
        kinds = {a["event_type"] for a in audits}
        assert {"EVENT_RECEIVED", "HOLD_ADMITTED", "VOUCHER_SETTLED",
                "BATCH_SEALED", "VOUCHER_SUSPENDED",
                "ADJUSTMENT_CONFIRMED"} <= kinds


def test_api_validation_errors(svc):
    with TestClient(app) as c:
        assert c.post("/quota/accounts",
                      json={"name": "zzz", "timezone": "Bad/Zone"}).status_code == 400
        r = c.post("/quota/holds", json={
            "serial": "bad", "account": "acct", "rule_name": "r1",
            "occurred_at": at(2026, 9, 16, 4).isoformat(), "amount": -1})
        assert r.status_code == 422
