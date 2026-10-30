"""Handler 抽象基类、动作声明装饰器、动作上下文。

扩展一个新模块只需要三步：

1. 新建 ``handlers/xxx_handler.py``；
2. 继承 ``BaseHandler``，声明 ``module / extensions / description``；
3. 用 ``@action("read", summary=...)`` 装饰动作方法。

``registry`` 会自动发现并注册，无需改动任何框架代码。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from core.errors import ActionError, InputError, ParamError
from core.result import Artifact, Response
from core.version import VERSION as _PKG_VERSION
from core.utils import ensure_dir, human_bytes, resolve_path, sniff

# ---------------------------------------------------------------------------
# 动作声明
# ---------------------------------------------------------------------------


@dataclass
class ActionSpec:
    """一个可被调度的动作。"""

    name: str
    func: Callable[..., Any]
    summary: str = ""
    params: dict[str, str] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    produces_files: bool = False
    modifies_input: bool = False
    needs_input: bool = True
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.name,
            "summary": self.summary,
            "params": self.params,
            "aliases": list(self.aliases),
            "produces_files": self.produces_files,
            "modifies_input": self.modifies_input,
            "needs_input": self.needs_input,
            "tags": list(self.tags),
        }


def action(
    name: str,
    *,
    summary: str = "",
    params: Optional[dict[str, str]] = None,
    aliases: Sequence[str] = (),
    produces_files: bool = False,
    modifies_input: bool = False,
    needs_input: bool = True,
    tags: Sequence[str] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个方法声明为可调度动作。

    Args:
        name: 动作名（``probe`` / ``read`` / ``analyze`` / ``transform`` / ``edit``
            为跨模块统一动词，自定义动词也可）。
        params: 参数说明表 ``{参数名: 说明}``，会出现在 ``cap`` 输出里，
            相当于自文档化的接口契约。
        aliases: 别名，便于兼容不同调用方习惯。
        produces_files: 该动作是否会产出文件（影响 Agent 侧的处理策略）。
        modifies_input: 该动作是否会改动输入文件（若为 True，默认先备份）。
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.__toolkit_action__ = ActionSpec(  # type: ignore[attr-defined]
            name=name,
            func=func,
            summary=summary or (func.__doc__ or "").strip().splitlines()[0] if func.__doc__ else summary,
            params=dict(params or {}),
            aliases=tuple(aliases),
            produces_files=produces_files,
            modifies_input=modifies_input,
            needs_input=needs_input,
            tags=tuple(tags),
        )
        return func

    return decorator


# ---------------------------------------------------------------------------
# 动作上下文
# ---------------------------------------------------------------------------


@dataclass
class ActionContext:
    """动作执行上下文：统一提供「读参数 / 读输入 / 写产物」三件事。"""

    handler: "BaseHandler"
    action: str
    input: Optional[Path]
    output: Optional[Path]
    options: dict[str, Any]
    artifacts: list[Artifact] = field(default_factory=list)

    # ----------------------------------------------------------- 参数读取
    def get(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key, default)
        return default if value is None else value

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.options.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on", ""}
        return bool(value)

    def require(self, key: str) -> Any:
        if key not in self.options or self.options[key] is None:
            raise ParamError(f"动作 {self.action} 缺少必填参数: {key}")
        return self.options[key]

    def number(self, key: str, default: Optional[float] = None) -> Optional[float]:
        value = self.options.get(key, default)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ParamError(f"参数 {key} 必须是数字，收到: {value!r}") from exc

    def integer(self, key: str, default: Optional[int] = None) -> Optional[int]:
        value = self.number(key, None if default is None else float(default))
        return None if value is None else int(value)

    def text(self, key: str, default: str = "") -> str:
        value = self.options.get(key, default)
        return default if value is None else str(value)

    def list_of(self, key: str, default: Optional[list] = None) -> list:
        value = self.options.get(key, default)
        if value is None:
            return list(default or [])
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        raise ParamError(f"参数 {key} 应为列表或逗号分隔字符串，收到: {type(value).__name__}")

    def bounded_int(self, key: str, default: int, *, low: int, high: int) -> int:
        value = self.integer(key, default)
        if value is None:
            value = default
        if not (low <= value <= high):
            raise ParamError(f"参数 {key} 需在 [{low}, {high}] 区间，收到: {value}")
        return value

    # ----------------------------------------------------------- 输入
    def input_path(self) -> Path:
        if self.input is None:
            raise ParamError(f"动作 {self.action} 需要输入文件路径")
        return self.input

    def info(self) -> dict[str, Any]:
        """输入文件嗅探结果（缓存在 options 里避免重复读盘）。"""
        cached = self.options.get("__sniff__")
        if isinstance(cached, dict):
            return cached
        result = sniff(self.input_path())
        self.options["__sniff__"] = result
        return result

    # ----------------------------------------------------------- 输出
    @property
    def _output_is_file(self) -> bool:
        if self.output is None:
            return False
        if self.output.exists() and self.output.is_dir():
            return False
        return bool(self.output.suffix)

    def out_dir(self, name: Optional[str] = None) -> Path:
        """产出目录。

        ``output`` 指向文件时用其父目录；未指定时在输入文件旁创建
        ``<stem>_out/``，保证「不污染原文件」。
        """
        if self.output is not None and self._output_is_file:
            base = self.output.parent
        elif self.output is not None:
            base = self.output
        else:
            base = self.input_path().parent / f"{self.input_path().stem}_out"
        if name:
            base = base / name
        return ensure_dir(base)

    def out_file(self, filename: str) -> Path:
        """产出文件路径。显式指定 ``output`` 文件时以其为准（覆盖写）。"""
        if self._output_is_file:
            target = self.output  # type: ignore[assignment]
            assert target is not None
            ensure_dir(target.parent)
            return target
        target = self.out_dir() / filename
        ensure_dir(target.parent)
        return target

    def emit(self, path: str | Path, kind: str = "file", note: str = "") -> Path:
        """登记一个产出文件。"""
        p = resolve_path(path)
        size = p.stat().st_size if p.exists() else 0
        self.artifacts.append(
            Artifact(path=str(p), kind=kind, bytes=size, note=note or human_bytes(size))
        )
        return p

    def warn(self, message: str) -> None:
        self.handler.warn(message)


# ---------------------------------------------------------------------------
# Handler 元类：自动收集 @action
# ---------------------------------------------------------------------------


class HandlerMeta(type):
    def __new__(mcls, name: str, bases: tuple, namespace: dict):
        cls = super().__new__(mcls, name, bases, namespace)
        actions: dict[str, ActionSpec] = {}
        aliases: dict[str, str] = {}
        for base in reversed(cls.__mro__):
            for value in vars(base).values():
                spec = getattr(value, "__toolkit_action__", None)
                if isinstance(spec, ActionSpec):
                    actions[spec.name] = spec
                    for alias in spec.aliases:
                        aliases[alias] = spec.name
        cls._actions = actions  # type: ignore[attr-defined]
        cls._aliases = aliases  # type: ignore[attr-defined]
        return cls


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class BaseHandler(metaclass=HandlerMeta):
    """所有文件处理模块的基类。"""

    #: 模块名，也是 CLI 里的 ``--module`` 取值
    module: str = "base"
    #: 展示名
    display_name: str = "Base"
    #: 触发说明（会写进 capabilities，供 Agent 判断何时使用）
    description: str = ""
    #: 支持的扩展名（小写，含点）
    extensions: tuple[str, ...] = ()
    #: 支持的 kind（由 sniff 产出）
    kinds: tuple[str, ...] = ()
    #: 版本
    version: str = _PKG_VERSION
    #: 该模块声明的可选依赖：``{import 名: pip 包名}``（缺了只是降级）
    optional_deps: dict[str, str] = {}
    #: 该模块的硬依赖：``{import 名: pip 包名}``，缺了整块动作不可用。
    #: 与 optional_deps 分开，避免 `cli list` 把"缺了就废"的包标成"可选"。
    required_deps: dict[str, str] = {}
    #: 动作执行时默认合并的选项
    default_options: dict[str, Any] = {}

    _actions: dict[str, ActionSpec] = {}
    _aliases: dict[str, str] = {}

    def __init__(self, options: Optional[dict[str, Any]] = None) -> None:
        self.options: dict[str, Any] = dict(self.default_options)
        self.options.update(options or {})
        self.warnings: list[str] = []

    # ------------------------------------------------------------ 能力描述
    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def can_handle_kind(self, kind: str) -> bool:
        return kind in self.kinds

    def can_handle_ext(self, ext: str) -> bool:
        return ext.lower() in self.extensions

    def capabilities(self) -> dict[str, Any]:
        """模块自描述，供 ``cap`` / Agent 路由使用。"""
        return {
            "module": self.module,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "extensions": list(self.extensions),
            "kinds": list(self.kinds),
            "optional_deps": dict(self.optional_deps),
            "required_deps": dict(self.required_deps),
            "missing_required_deps": self.missing_required_deps(),
            "actions": [spec.to_dict() for spec in self._actions.values()],
        }

    def missing_required_deps(self) -> list[str]:
        """返回当前解释器下缺失的硬依赖 pip 包名（用于自描述与门禁提示）。"""
        missing: list[str] = []
        for module_name, pip_name in self.required_deps.items():
            try:
                __import__(module_name)
            except Exception:  # noqa: BLE001 - 任何导入异常都算缺失
                missing.append(pip_name)
        return missing

    def action_names(self) -> list[str]:
        return list(self._actions)

    # ------------------------------------------------------------ 调度入口
    def resolve_action(self, name: str) -> Optional[ActionSpec]:
        key = (name or "").strip().lower()
        return self._actions.get(key) or self._actions.get(self._aliases.get(key, ""))

    def dispatch(
        self,
        action: str,
        input: Optional[str | Path] = None,  # noqa: A002 - 与 CLI 参数名保持一致
        output: Optional[str | Path] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> Response:
        """执行一个动作。异常不在此处吞掉，由 dispatcher 统一转成信封。"""
        merged = dict(self.options)
        merged.update(options or {})
        spec = self.resolve_action(action)
        if spec is None:
            raise ActionError(
                f"模块 {self.module} 不支持动作: {action}",
                hint=f"可用动作: {', '.join(self.action_names())}",
                details={"module": self.module, "action": action},
            )

        inp = resolve_path(input, must_exist=True) if input is not None else None
        if spec.needs_input and inp is None:
            raise ParamError(f"动作 {spec.name} 需要 --input 输入文件")

        out = resolve_path(output) if output is not None else None

        if spec.modifies_input and merged.get("backup", True) and inp is not None:
            from core.utils import backup_file

            backup = backup_file(inp, tag=merged.get("backup_tag", "bak"))
            if backup is not None:
                merged["__backup__"] = str(backup)
            merged.setdefault("in_place", True)

        ctx = ActionContext(
            handler=self,
            action=spec.name,
            input=inp,
            output=out,
            options=merged,
        )
        self.warnings = []

        started = time.perf_counter()
        data = spec.func(self, ctx)
        elapsed = int((time.perf_counter() - started) * 1000)

        return Response(
            ok=True,
            module=self.module,
            action=spec.name,
            input=str(inp) if inp is not None else None,
            output=str(out) if out is not None else None,
            elapsed_ms=elapsed,
            data=data if data is not None else {},
            artifacts=[a.to_dict() for a in ctx.artifacts],
            warnings=list(self.warnings),
        )

    # 便于调试
    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} module={self.module} actions={self.action_names()}>"


class StaticHandler(BaseHandler):
    """不处理文件、只提供信息的模块基类（其动作通常 ``needs_input=False``）。"""

    kinds: tuple[str, ...] = ()
