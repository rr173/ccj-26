"""片段 DSL 的语义差异：以路径为单位逐项比对两棵表达式树。

评审人看到的不是原始 JSON 文本对比，而是带语义标注的差异：
- 每个差异定位到 JSON 路径（$.args[0]...），标注变更类型（算子/字面量/变量/引用/结构）
- 汇总引用片段、输入变量、算子集合的增减（影响范围判断依据）
纯函数，便于单测。
"""
from __future__ import annotations

from . import dsl


def _node_kind(node):
    if isinstance(node, dict):
        if "op" in node:
            return "op"
        if "var" in node:
            return "var"
        if "ref" in node:
            return "ref"
    if isinstance(node, list):
        return "list"
    return "literal"


def _walk_diff(base, new, path, out):
    kb, kn = _node_kind(base), _node_kind(new)
    if kb != kn:
        out.append({"path": path, "kind": "structure_changed",
                    "from_kind": kb, "to_kind": kn, "from": base, "to": new})
        return

    if kb == "op":
        if base["op"] != new["op"]:
            out.append({"path": f"{path}.op", "kind": "operator_changed",
                        "from": base["op"], "to": new["op"]})
        ba, na = base.get("args", []), new.get("args", [])
        _diff_args(ba, na, f"{path}.args", out)
    elif kb == "var":
        if base["var"] != new["var"]:
            out.append({"path": f"{path}.var", "kind": "variable_changed",
                        "from": base["var"], "to": new["var"]})
        if ("default" in base) != ("default" in new):
            out.append({"path": f"{path}.default",
                        "kind": "default_added" if "default" in new else "default_removed",
                        "from": base.get("default"), "to": new.get("default")})
        elif base.get("default") != new.get("default"):
            out.append({"path": f"{path}.default", "kind": "default_changed",
                        "from": base.get("default"), "to": new.get("default")})
    elif kb == "ref":
        if base["ref"] != new["ref"]:
            out.append({"path": f"{path}.ref", "kind": "reference_changed",
                        "from": base["ref"], "to": new["ref"]})
    elif kb == "list":
        _diff_args(base, new, path, out)
    else:
        if base != new:
            out.append({"path": path, "kind": "literal_changed",
                        "from": base, "to": new})


def _diff_args(ba, na, path, out):
    for i in range(min(len(ba), len(na))):
        _walk_diff(ba[i], na[i], f"{path}[{i}]", out)
    for i in range(len(na), len(ba)):
        out.append({"path": f"{path}[{i}]", "kind": "removed", "value": ba[i]})
    for i in range(len(ba), len(na)):
        out.append({"path": f"{path}[{i}]", "kind": "added", "value": na[i]})


def _refs(expr):
    return sorted(dsl.extract_refs(expr))


def _vars(expr, acc):
    if isinstance(expr, dict):
        if "var" in expr and isinstance(expr["var"], str):
            acc.add(expr["var"])
        for v in expr.values():
            _vars(v, acc)
    elif isinstance(expr, list):
        for v in expr:
            _vars(v, acc)
    return acc


def _ops(expr, acc):
    if isinstance(expr, dict):
        if "op" in expr:
            acc.add(expr["op"])
        for v in expr.values():
            _ops(v, acc)
    elif isinstance(expr, list):
        for v in expr:
            _ops(v, acc)
    return acc


def semantic_diff(base_body, new_body) -> dict:
    """返回逐项语义差异。base_body 为 None 表示新增片段。"""
    changes = []
    if base_body is None:
        changes.append({"path": "$", "kind": "fragment_added", "value": new_body})
        base_refs, base_vars, base_ops = [], set(), set()
    else:
        _walk_diff(base_body, new_body, "$", changes)
        base_refs, base_vars, base_ops = (_refs(base_body), _vars(base_body, set()),
                                          _ops(base_body, set()))

    new_refs, new_vars, new_ops = _refs(new_body), _vars(new_body, set()), _ops(new_body, set())
    return {
        "equal": not changes,
        "changes": changes,
        "change_count": len(changes),
        "refs": {"added": sorted(set(new_refs) - set(base_refs)),
                 "removed": sorted(set(base_refs) - set(new_refs))},
        "variables": {"added": sorted(new_vars - base_vars),
                      "removed": sorted(base_vars - new_vars)},
        "operators": {"added": sorted(new_ops - base_ops),
                      "removed": sorted(base_ops - new_ops)},
    }
