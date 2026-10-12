"""统一调度器：把「请求」翻译成「模块 + 动作 + 响应信封」。

三种调用形态共用同一套逻辑：

1. Python 调用：``dispatcher.run({"module": "pptx", "action": "read", "input": "a.pptx"})``
2. 命令行：``python -m core.cli run --action read --input a.pptx``
3. Agent / HTTP 桥接：直接把 JSON 请求体交给 ``run()``（永不抛异常）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from core import registry
from core.errors import ParamError, ToolkitError
from core.result import Response
from core.utils import resolve_path, sniff


@dataclass
class Request:
    """一次调用的完整描述。"""

    action: str
    input: Optional[str] = None  # noqa: A003 - 与 CLI/JSON 字段名保持一致
    module: str = "auto"
    output: Optional[str] = None
    options: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Request":
        if not isinstance(payload, dict):
            raise ParamError(f"请求体必须是对象，收到: {type(payload).__name__}")
        action = payload.get("action") or payload.get("op") or payload.get("method")
        if not action:
            raise ParamError("请求体缺少 action 字段", details={"keys": sorted(payload)})
        return cls(
            action=str(action),
            input=(payload.get("input") or payload.get("path") or payload.get("file")),
            module=str(payload.get("module") or payload.get("handler") or "auto"),
            output=payload.get("output") or payload.get("out"),
            options=dict(payload.get("options") or payload.get("params") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "action": self.action,
            "input": self.input,
            "output": self.output,
            "options": dict(self.options),
        }

    def resolved_module(self) -> tuple[str, Optional[dict[str, Any]]]:
        """解析出实际模块名；``auto`` 时按文件类型自动路由。"""
        module = (self.module or "auto").strip().lower()
        if module in ("", "auto", "*"):
            if not self.input:
                raise ParamError("module=auto 时必须提供 input 以便自动识别类型")
            module, info = registry.resolve(self.input)
            return module, info
        return module, None


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def execute(request: Request) -> Response:
    """执行请求。可能抛 ToolkitError，由 ``run`` 兜底。"""
    module, _info = request.resolved_module()
    handler = registry.require_handler(module)
    return handler.dispatch(
        request.action,
        input=request.input,
        output=request.output,
        options=request.options,
    )


def run_request(request: Request) -> dict[str, Any]:
    """执行请求，永不抛异常，始终返回信封字典。"""
    started = time.perf_counter()
    try:
        response = execute(request)
    except ToolkitError as exc:
        return Response.failure(
            exc,
            module="" if request.module in ("auto", "") else request.module,
            action=request.action,
            input=str(resolve_path(request.input)) if request.input else None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        ).to_dict()
    except Exception as exc:  # 未预期异常：也转成信封，附带类型名
        return Response.failure(
            exc,
            module="" if request.module in ("auto", "") else request.module,
            action=request.action,
            input=str(resolve_path(request.input)) if request.input else None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        ).to_dict()
    return response.to_dict()


def run(payload: dict[str, Any] | Request) -> dict[str, Any]:
    """最外层入口：``dict`` 或 ``Request`` 进，信封 ``dict`` 出。"""
    try:
        request = payload if isinstance(payload, Request) else Request.from_dict(payload)
    except ToolkitError as exc:
        return Response.failure(exc).to_dict()
    return run_request(request)


def run_many(payloads: Iterable[dict[str, Any] | Request]) -> list[dict[str, Any]]:
    return [run(p) for p in payloads]


def run_json(text: str) -> dict[str, Any]:
    """从 JSON 字符串执行（CLI ``ask`` / stdio 桥接用）。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return Response.failure(
            ParamError(f"请求 JSON 解析失败: {exc.msg}", details={"position": exc.pos})
        ).to_dict()
    if isinstance(payload, list):
        return {"ok": True, "batch": True, "results": run_many(payload)}
    return run(payload)


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------
def run_batch(
    target: str | Path,
    action: str,
    *,
    module: str = "auto",
    options: Optional[dict[str, Any]] = None,
    output: Optional[str | Path] = None,
    recursive: bool = True,
    kinds: Optional[Sequence[str]] = None,
    continue_on_error: bool = True,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """对目录（或单文件）批量执行同一动作。"""
    from core.utils import iter_files

    base = resolve_path(target, must_exist=True)
    files = iter_files(base, recursive=recursive, kinds=kinds)
    if limit:
        files = files[:limit]

    results: list[dict[str, Any]] = []
    for file in files:
        req = Request(
            action=action,
            input=str(file),
            module=module,
            options=dict(options or {}),
            output=str(output) if output else None,
        )
        result = run_request(req)
        results.append(result)
        if not result.get("ok") and not continue_on_error:
            break

    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_count == len(results),
        "batch": True,
        "root": str(base),
        "action": action,
        "module": module,
        "total": len(results),
        "succeeded": ok_count,
        "failed": len(results) - ok_count,
        "artifacts": [a for r in results for a in r.get("artifacts", [])],
        "results": results,
    }


__all__ = [
    "Request",
    "execute",
    "run",
    "run_request",
    "run_many",
    "run_json",
    "run_batch",
]
