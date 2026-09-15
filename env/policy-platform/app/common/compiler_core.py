"""编译核心：依赖图解析（拓扑排序 + 循环检测）与不可变产物构建。纯函数，便于单测。"""
from __future__ import annotations

import json

from . import dsl


class CompileError(Exception):
    pass


class CycleError(CompileError):
    def __init__(self, cycle: list):
        self.cycle = cycle
        super().__init__("dependency cycle detected: " + " -> ".join(cycle))


class MissingRefError(CompileError):
    def __init__(self, ref: str, referrer):
        self.ref = ref
        self.referrer = referrer
        where = f" referenced by '{referrer}'" if referrer else ""
        super().__init__(f"fragment '{ref}'{where} not found")


def resolve_order(entry: str, fragments: dict) -> list:
    """DFS 三色标记求拓扑序（依赖在前）。发现回边即报循环并给出完整环路径。"""
    order: list = []
    state: dict = {}  # 1=在栈中, 2=已完成

    def visit(name: str, ancestors: list) -> None:
        frag = fragments.get(name)
        if frag is None:
            raise MissingRefError(name, ancestors[-1] if ancestors else None)
        s = state.get(name, 0)
        if s == 2:
            return
        if s == 1:
            i = ancestors.index(name)
            raise CycleError(ancestors[i:] + [name])
        state[name] = 1
        for dep in frag.refs:
            visit(dep, ancestors + [name])
        state[name] = 2
        order.append(name)

    visit(entry, [])
    return order


def build_artifact(policy_name: str, entry: str, fragments: dict) -> dict:
    """解析依赖图 -> 逐片段编译为节点图 -> 计算内容哈希，产出不可变产物描述。"""
    order = resolve_order(entry, fragments)
    nodes: dict = {}
    topo: list = []
    counter = [0]
    entries: dict = {}
    for fname in order:
        frag = fragments[fname]
        dsl.validate_expr(frag.body)  # 编译期再校验一次（编辑器之外写入的兜底）
        entries[fname] = dsl.compile_expr(frag.body, fname, nodes, topo, counter, entries)
    dep_chain = [
        {"fragment": f, "version": fragments[f].version, "hash": fragments[f].content_hash}
        for f in order
    ]
    canonical = json.dumps(
        {"policy": policy_name, "entry": entries[entry], "nodes": nodes,
         "topo": topo, "dep_chain": dep_chain},
        sort_keys=True,
    )
    import hashlib

    return {
        "nodes": nodes,
        "topo": topo,
        "entry_node": entries[entry],
        "dep_chain": dep_chain,
        "fragments": order,
        "hash": hashlib.sha256(canonical.encode()).hexdigest(),
    }
