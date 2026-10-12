"""统一返回信封：Artifact / Response / Timer。

约定（所有 handler 必须遵守）
----------------------------
* 读取类动作 → 数据放在 ``data``；
* 产生文件的动作 → 每个产物登记为 ``Artifact``（路径 + 类型 + 字节数）；
* 非致命问题 → 走 ``warnings``，不要中断流程；
* 致命问题 → 抛异常，由 dispatcher 转成 ``error``。

这样 Agent 侧只需要处理一种结构，无需为每个模块写适配。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class Artifact:
    """一个产出文件。"""

    path: str
    kind: str = "file"
    bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "bytes": self.bytes,
            "note": self.note,
        }


@dataclass
class Timer:
    """``with Timer() as t: ...`` 之后读 ``t.ms``。"""

    ms: int = 0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.ms = int((time.perf_counter() - self._t0) * 1000)


@dataclass
class Response:
    """统一响应信封。"""

    ok: bool = True
    module: str = ""
    action: str = ""
    input: Optional[str] = None
    output: Optional[str] = None
    elapsed_ms: int = 0
    data: Any = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def success(cls, **kw: Any) -> "Response":
        return cls(ok=True, **kw)

    @classmethod
    def failure(
        cls,
        error: Any,
        *,
        module: str = "",
        action: str = "",
        input: Optional[str] = None,
        elapsed_ms: int = 0,
        warnings: Optional[Iterable[str]] = None,
    ) -> "Response":
        if isinstance(error, dict):
            payload = error
        elif hasattr(error, "to_dict"):
            payload = error.to_dict()
        else:  # 未预期异常，保留类型名便于定位
            payload = {
                "code": "INTERNAL_ERROR",
                "message": str(error),
                "hint": None,
                "details": {"type": type(error).__name__},
            }
        return cls(
            ok=False,
            module=module,
            action=action,
            input=input,
            elapsed_ms=elapsed_ms,
            data={},
            artifacts=[],
            warnings=list(warnings or []),
            error=payload,
        )

    # ------------------------------------------------------------------ 输出
    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "module": self.module,
            "action": self.action,
            "input": self.input,
            "output": self.output,
            "elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
            "artifacts": [dict(a) for a in self.artifacts],
            "data": self.data,
            "error": self.error,
        }

    def to_json(self, indent: int = 2, ensure_ascii: bool = False) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=ensure_ascii, default=_json_default)

    def summary(self) -> str:
        """一行人类可读摘要，供 CLI 非 JSON 模式使用。"""
        head = f"[{'OK ' if self.ok else 'FAIL'}] {self.module}.{self.action}"
        if self.input:
            head += f" <- {Path(self.input).name}"
        head += f" ({self.elapsed_ms}ms)"
        lines = [head]
        if self.artifacts:
            lines.append(f"  产物 {len(self.artifacts)} 个:")
            for a in self.artifacts:
                lines.append(f"    - [{a.get('kind')}] {a.get('path')} ({a.get('bytes')}B)")
        if self.warnings:
            lines.append(f"  警告 {len(self.warnings)} 条:")
            for w in self.warnings:
                lines.append(f"    ! {w}")
        if not self.ok and self.error:
            lines.append(f"  错误 [{self.error.get('code')}] {self.error.get('message')}")
            if self.error.get("hint"):
                lines.append(f"  建议: {self.error['hint']}")
        return "\n".join(lines)


def _json_default(obj: Any):
    """json 回退序列化：Path / set / bytes / 其它对象转 str。"""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return f"<bytes:{len(obj)}>"
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy 标量
        try:
            return obj.item()
        except Exception:  # pragma: no cover
            pass
    return str(obj)
