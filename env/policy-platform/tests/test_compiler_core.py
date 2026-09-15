"""编译核心：依赖图解析、循环检测、拓扑序、内容哈希。"""
from types import SimpleNamespace

import pytest

from app.common import dsl
from app.common.compiler_core import (CycleError, MissingRefError,
                                      build_artifact, resolve_order)


def frag(name, body, version=1):
    return SimpleNamespace(
        name=name, body=body, refs=sorted(dsl.extract_refs(body)),
        version=version, content_hash=dsl.canonical_hash(body))


def test_topo_order_dependencies_first():
    frags = {
        "a": frag("a", {"op": "and", "args": [{"ref": "b"}, {"ref": "c"}]}),
        "b": frag("b", {"op": "eq", "args": [1, 1]}),
        "c": frag("c", {"op": "and", "args": [{"ref": "b"}, True]}),
    }
    order = resolve_order("a", frags)
    assert order.index("b") < order.index("a")
    assert order.index("c") < order.index("a")
    assert order.index("b") < order.index("c")
    assert order[-1] == "a"


def test_cycle_detected_with_path():
    frags = {
        "a": frag("a", {"ref": "b"}),
        "b": frag("b", {"ref": "c"}),
        "c": frag("c", {"ref": "a"}),
    }
    with pytest.raises(CycleError) as ei:
        resolve_order("a", frags)
    assert ei.value.cycle == ["a", "b", "c", "a"]


def test_self_cycle():
    frags = {"a": frag("a", {"op": "not", "args": [{"ref": "a"}]})}
    with pytest.raises(CycleError):
        resolve_order("a", frags)


def test_missing_ref_reports_referrer():
    frags = {"a": frag("a", {"ref": "ghost"})}
    with pytest.raises(MissingRefError) as ei:
        resolve_order("a", frags)
    assert "ghost" in str(ei.value) and "a" in str(ei.value)


def test_artifact_hash_stable_and_content_addressed():
    frags = {
        "a": frag("a", {"op": "and", "args": [{"ref": "b"}, True]}),
        "b": frag("b", {"op": "eq", "args": [1, 1]}),
    }
    a1 = build_artifact("p", "a", frags)
    a2 = build_artifact("p", "a", frags)
    assert a1["hash"] == a2["hash"]  # 相同内容 -> 相同哈希（重复发布去重的依据）

    frags2 = dict(frags)
    frags2["b"] = frag("b", {"op": "eq", "args": [1, 2]}, version=2)
    a3 = build_artifact("p", "a", frags2)
    assert a3["hash"] != a1["hash"]  # 依赖内容变化 -> 哈希变化


def test_dep_chain_records_fragment_versions():
    frags = {
        "a": frag("a", {"ref": "b"}, version=3),
        "b": frag("b", True, version=7),
    }
    art = build_artifact("p", "a", frags)
    chain = {d["fragment"]: d["version"] for d in art["dep_chain"]}
    assert chain == {"a": 3, "b": 7}
