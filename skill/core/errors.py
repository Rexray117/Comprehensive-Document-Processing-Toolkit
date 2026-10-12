"""统一异常体系。

设计目标
--------
1. handler 内部可以自由抛异常，dispatcher 负责兜底，把它翻译成统一的
   Response 信封（envelope），调用方永远拿到同一种结构。
2. 每个异常都带 ``hint`` 与 ``details``，让 Agent 拿到可执行的修复建议，
   而不是一句无信息的 traceback。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional


def pip_hint(packages: str) -> str:
    """生成与当前解释器匹配的 pip 安装命令。

    自动区分「虚拟环境」与「系统环境」，避免把包装到错误的解释器里。
    """
    exe = Path(sys.executable)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    flag = "" if in_venv else ""
    return f'"{exe}" -m pip install {flag}{packages}'.replace("  ", " ").strip()


class ToolkitError(Exception):
    """工具箱所有异常的基类。"""

    code = "TOOLKIT_ERROR"

    def __init__(
        self,
        message: str,
        *,
        hint: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
        }

    def __str__(self) -> str:  # pragma: no cover - 仅影响日志可读性
        if self.hint:
            return f"{self.message} | 建议: {self.hint}"
        return self.message


class DependencyError(ToolkitError):
    """缺少第三方依赖，或依赖版本不满足要求。"""

    code = "DEPENDENCY_MISSING"


class InputError(ToolkitError):
    """输入文件不存在、不可读、或不是预期格式。"""

    code = "INPUT_INVALID"


class UnsupportedError(ToolkitError):
    """文件类型不受任何已注册 handler 支持。"""

    code = "UNSUPPORTED_FORMAT"


class ActionError(ToolkitError):
    """动作不存在、动作执行失败、或动作被禁止。"""

    code = "ACTION_FAILED"


class ParamError(ToolkitError):
    """动作参数缺失、类型错误或取值越界。"""

    code = "PARAM_INVALID"


def require(
    module_name: str,
    pip_name: Optional[str] = None,
    *,
    hint: Optional[str] = None,
):
    """惰性导入第三方库，失败时抛出带安装命令的 DependencyError。

    各 handler 把重依赖放在动作函数内部导入，这样 ``list`` / ``probe`` /
    ``doctor`` 在零依赖环境下依然可用，工具不会因为缺一个库就整体不可用。
    """
    import importlib

    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # noqa: PERF203
        pkg = pip_name or module_name
        raise DependencyError(
            f"缺少依赖库 {pkg}（导入 {module_name} 失败）",
            hint=hint or f"安装命令: {pip_hint(pkg)}",
            details={"module": module_name, "package": pkg, "reason": str(exc)},
        ) from exc
