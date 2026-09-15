"""DSL 求值与校验单元测试。"""
import time
from types import SimpleNamespace

import pytest

from app.common import dsl
from app.common.compiler_core import build_artifact


def frag(name, body, version=1):
    return SimpleNamespace(
        name=name, body=body, refs=sorted(dsl.extract_refs(body)),
        version=version, content_hash=dsl.canonical_hash(body))


def run(body, inputs=None, timeout_s=5.0, extra_frags=None):
    frags = {"root": frag("root", body)}
    frags.update(extra_frags or {})
    art = build_artifact("p", "root", frags)
    return dsl.evaluate(art["nodes"], art["topo"], art["entry_node"],
                        inputs or {}, time.monotonic() + timeout_s, dsl.OPS)


def test_arithmetic_and_comparison():
    body = {"op": "and", "args": [
        {"op": "lt", "args": [{"var": "amount"}, 10000]},
        {"op": "gte", "args": [{"op": "add", "args": [1, 2, 3]}, 6]},
    ]}
    assert run(body, {"amount": 500}) is True
    assert run(body, {"amount": 99999}) is False


def test_var_default_and_missing_input():
    assert run({"var": "tier", "default": "standard"}) == "standard"
    with pytest.raises(dsl.MissingInput):
        run({"var": "tier"})


def test_if_and_string_ops():
    body = {"op": "if", "args": [
        {"op": "startswith", "args": [{"var": "email"}, "admin"]},
        "privileged", "normal"]}
    assert run(body, {"email": "admin@x.com"}) == "privileged"
    assert run(body, {"email": "user@x.com"}) == "normal"


def test_list_literal_and_in():
    body = {"op": "in", "args": [{"var": "country"}, ["CN", "SG", "HK"]]}
    assert run(body, {"country": "CN"}) is True
    assert run(body, {"country": "US"}) is False


def test_division_by_zero_is_deterministic_error():
    with pytest.raises(dsl.DSLError, match="division by zero"):
        run({"op": "div", "args": [1, 0]})


def test_timeout_raises_policy_timeout():
    with pytest.raises(dsl.PolicyTimeout):
        run({"op": "add", "args": [{"op": "sleep", "args": [500]}, 1]},
            timeout_s=0.05)


def test_validate_rejects_unknown_op_and_bad_arity():
    with pytest.raises(dsl.DSLError, match="unknown op"):
        dsl.validate_expr({"op": "nosuch", "args": []})
    with pytest.raises(dsl.DSLError, match="expects"):
        dsl.validate_expr({"op": "eq", "args": [1]})


def test_compiler_only_op_validates_but_not_in_runtime_ops():
    dsl.validate_expr({"op": "approx_match", "args": ["a", "b"]})  # 编译器认识
    assert "approx_match" not in dsl.OPS  # 运行时不认识 -> 节点未加载回退


def test_ref_inlines_dependency():
    dep = frag("dep", {"op": "gt", "args": [{"var": "x"}, 10]})
    body = {"op": "and", "args": [{"ref": "dep"}, {"var": "flag", "default": True}]}
    assert run(body, {"x": 20}, extra_frags={"dep": dep}) is True
    assert run(body, {"x": 5}, extra_frags={"dep": dep}) is False
