"""编辑服务 -> 编译服务的 HTTP 客户端。

测试中可以 monkeypatch 模块级函数，把跨进程调用替换为进程内直连。
"""
import httpx

from .config import COMPILER_URL

_TIMEOUT = 30.0


class CompilerUnavailable(Exception):
    pass


def _post(path: str, payload: dict) -> httpx.Response:
    try:
        return httpx.post(f"{COMPILER_URL}{path}", json=payload, timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise CompilerUnavailable(str(e)) from e


def compile_policy(policy: str, actor: str) -> httpx.Response:
    return _post("/compile", {"policy": policy, "actor": actor})


def compile_affected(fragment: str, actor: str) -> httpx.Response:
    return _post("/compile/affected", {"fragment": fragment, "actor": actor})
