"""安全表达式 DSL：校验、编译为节点图、带截止时间的求值。

表达式文法（JSON）：
  字面量                  -> number | string | boolean | null
  [expr, ...]             -> 列表字面量（元素可以是任意表达式）
  {"var": "name"}         -> 查询输入变量，可选 "default"
  {"ref": "fragment"}     -> 片段引用（编译期内联为节点图）
  {"op": "...", "args": [expr, ...]}

注意：`if` 的两个分支都会被求值（eager），分支内不要放可能报错的表达式。
"""
from __future__ import annotations

import hashlib
import json
import math
import time


class DSLError(Exception):
    """表达式静态校验或求值时的确定性错误（不触发版本回退）。"""


class PolicyTimeout(Exception):
    """执行超出截止时间，触发安全回退。"""


class MissingInput(DSLError):
    pass


MAX_SLEEP_MS = 2000


def _div(a):
    if a[1] == 0:
        raise DSLError("division by zero")
    return a[0] / a[1]


def _mod(a):
    if a[1] == 0:
        raise DSLError("modulo by zero")
    return a[0] % a[1]


def _sleep(a):
    # 测试/演示辅助：模拟慢节点，用于验证超时回退。上限 2s。
    ms = min(float(a[0]), MAX_SLEEP_MS)
    time.sleep(ms / 1000.0)
    return ms


def _approx_match(a):
    # 仅编译器支持的算子（模拟滚动升级时 runtime 版本落后于 compiler 的场景）：
    # 用该算子的产物在旧 runtime 上会被判定为“部分节点未加载”，从而触发回退。
    return str(a[0]).strip().lower() == str(a[1]).strip().lower()


def _coalesce(a):
    for x in a:
        if x is not None:
            return x
    return None


def _round(a):
    return round(a[0], int(a[1]) if len(a) > 1 else 0)


# op -> (fn, arity)；arity: int | (min, max) | None(变长, 至少 1 个参数)
OPS = {
    "and": (lambda a: all(a), None),
    "or": (lambda a: any(a), None),
    "not": (lambda a: not a[0], 1),
    "eq": (lambda a: a[0] == a[1], 2),
    "ne": (lambda a: a[0] != a[1], 2),
    "lt": (lambda a: a[0] < a[1], 2),
    "lte": (lambda a: a[0] <= a[1], 2),
    "gt": (lambda a: a[0] > a[1], 2),
    "gte": (lambda a: a[0] >= a[1], 2),
    "add": (lambda a: sum(a), None),
    "sub": (lambda a: a[0] - a[1], 2),
    "mul": (lambda a: math.prod(a), None),
    "div": (_div, 2),
    "mod": (_mod, 2),
    "in": (lambda a: a[0] in a[1], 2),
    "contains": (lambda a: a[1] in a[0], 2),
    "startswith": (lambda a: str(a[0]).startswith(str(a[1])), 2),
    "endswith": (lambda a: str(a[0]).endswith(str(a[1])), 2),
    "lower": (lambda a: str(a[0]).lower(), 1),
    "upper": (lambda a: str(a[0]).upper(), 1),
    "concat": (lambda a: "".join(str(x) for x in a), None),
    "len": (lambda a: len(a[0]), 1),
    "abs": (lambda a: abs(a[0]), 1),
    "min": (lambda a: min(a), None),
    "max": (lambda a: max(a), None),
    "round": (_round, (1, 2)),
    "coalesce": (_coalesce, None),
    "if": (lambda a: a[1] if a[0] else a[2], 3),
    "sleep": (_sleep, 1),
}

# 运行时注册表 = OPS；编译器额外支持 COMPILER_EXTRA_OPS（版本偏斜场景）
COMPILER_EXTRA_OPS = {"approx_match": (_approx_match, 2)}
COMPILER_OPS = {**OPS, **COMPILER_EXTRA_OPS}


def canonical_hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _check_arity(name: str, n: int, spec, path: str) -> None:
    if spec is None:
        if n < 1:
            raise DSLError(f"op '{name}' at {path} expects at least 1 arg, got {n}")
    elif isinstance(spec, tuple):
        if not (spec[0] <= n <= spec[1]):
            raise DSLError(f"op '{name}' at {path} expects {spec[0]}..{spec[1]} args, got {n}")
    elif n != spec:
        raise DSLError(f"op '{name}' at {path} expects {spec} args, got {n}")


def validate_expr(expr, ops=None, path: str = "$") -> None:
    """静态校验：算子存在、参数个数正确、结构合法。"""
    if ops is None:
        ops = COMPILER_OPS
    if isinstance(expr, dict):
        if "op" in expr:
            name = expr["op"]
            if name not in ops:
                raise DSLError(f"unknown op '{name}' at {path}")
            args = expr.get("args")
            if not isinstance(args, list):
                raise DSLError(f"op '{name}' at {path} requires list 'args'")
            _check_arity(name, len(args), ops[name][1], path)
            for i, a in enumerate(args):
                validate_expr(a, ops, f"{path}.args[{i}]")
        elif "var" in expr:
            if not isinstance(expr["var"], str):
                raise DSLError(f"'var' at {path} must be a string")
            if "default" in expr:
                validate_expr(expr["default"], ops, f"{path}.default")
        elif "ref" in expr:
            if not isinstance(expr["ref"], str):
                raise DSLError(f"'ref' at {path} must be a string")
        else:
            raise DSLError(f"unknown expression object at {path}: {sorted(expr)}")
    elif isinstance(expr, list):
        for i, item in enumerate(expr):
            validate_expr(item, ops, f"{path}[{i}]")
    elif expr is None or isinstance(expr, (bool, int, float, str)):
        return
    else:
        raise DSLError(f"unsupported literal at {path}: {type(expr).__name__}")


def extract_refs(expr) -> set:
    refs = set()
    if isinstance(expr, dict):
        if "ref" in expr and isinstance(expr["ref"], str):
            refs.add(expr["ref"])
        for v in expr.values():
            refs |= extract_refs(v)
    elif isinstance(expr, list):
        for item in expr:
            refs |= extract_refs(item)
    return refs


def compile_expr(expr, frag_name: str, nodes: dict, topo: list, counter: list, entries: dict) -> str:
    """把表达式编译为节点，返回节点 id。ref 直接复用依赖片段的入口节点（须先编译依赖）。"""
    def new_id() -> str:
        counter[0] += 1
        return f"{frag_name}#{counter[0]}"

    if isinstance(expr, dict):
        if "op" in expr:
            args = [compile_expr(a, frag_name, nodes, topo, counter, entries) for a in expr["args"]]
            nid = new_id()
            nodes[nid] = {"kind": "op", "op": expr["op"], "args": args}
        elif "var" in expr:
            nid = new_id()
            node = {"kind": "var", "name": expr["var"]}
            if "default" in expr:
                node["default"] = expr["default"]
            nodes[nid] = node
        elif "ref" in expr:
            return entries[expr["ref"]]
        else:
            raise DSLError(f"unknown expression object in fragment '{frag_name}'")
        topo.append(nid)
        return nid
    if isinstance(expr, list):
        items = [compile_expr(i, frag_name, nodes, topo, counter, entries) for i in expr]
        nid = new_id()
        nodes[nid] = {"kind": "list", "args": items}
        topo.append(nid)
        return nid
    nid = new_id()
    nodes[nid] = {"kind": "const", "value": expr}
    topo.append(nid)
    return nid


def evaluate(nodes: dict, topo: list, entry: str, inputs: dict, deadline: float, ops) -> object:
    """按拓扑序迭代求值（无递归），每个节点前后检查截止时间。"""
    values = {}
    for nid in topo:
        if time.monotonic() > deadline:
            raise PolicyTimeout("execution deadline exceeded")
        node = nodes[nid]
        kind = node["kind"]
        if kind == "const":
            v = node["value"]
        elif kind == "var":
            if node["name"] in inputs:
                v = inputs[node["name"]]
            elif "default" in node:
                v = node["default"]
            else:
                raise MissingInput(f"missing input: {node['name']}")
        elif kind == "list":
            v = [values[a] for a in node["args"]]
        elif kind == "op":
            fn = ops[node["op"]][0]
            args = [values[a] for a in node["args"]]
            try:
                v = fn(args)
            except DSLError:
                raise
            except Exception as e:  # noqa: BLE001 - 统一包装为确定性 DSL 错误
                raise DSLError(f"op '{node['op']}' failed at {nid}: {e}") from e
        else:
            raise DSLError(f"unknown node kind '{kind}' at {nid}")
        values[nid] = v
    if time.monotonic() > deadline:
        raise PolicyTimeout("execution deadline exceeded")
    return values[entry]
