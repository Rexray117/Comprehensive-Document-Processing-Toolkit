"""模块注册中心：自动发现 handlers/ 下的所有模块。

「热插拔」原则
--------------
向 ``handlers/`` 目录丢一个 ``*_handler.py``（内含 BaseHandler 子类）即可被
自动注册，不需要修改 registry / dispatcher / cli 中的任何一行代码。

同时记录「加载失败原因」而不是静默忽略，方便 doctor 定位问题。
"""

from __future__ import annotations

import importlib
import pkgutil
import traceback
from typing import Any, Optional

from core.base import BaseHandler
from core.errors import UnsupportedError
from core.utils import KIND_TO_MODULE, PROJECT_ROOT, sniff

HANDLERS_PACKAGE = "handlers"

_HANDLERS: dict[str, BaseHandler] = {}
_LOAD_ERRORS: dict[str, str] = {}
_LOADED = False


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------
def discover_module_names() -> list[str]:
    """列出候选 handler 模块名（支持包内扫描与目录扫描两种方式）。"""
    names: list[str] = []
    try:
        pkg = importlib.import_module(HANDLERS_PACKAGE)
        pkg_paths = list(getattr(pkg, "__path__", []) or [])
    except Exception:
        pkg_paths = []

    if pkg_paths:
        for info in pkgutil.iter_modules(pkg_paths):
            if info.name.startswith("_"):
                continue
            names.append(f"{HANDLERS_PACKAGE}.{info.name}")
    else:  # 兜底：直接扫目录，允许 handlers 不是包（没有 __init__.py）
        hdir = PROJECT_ROOT / HANDLERS_PACKAGE
        if hdir.is_dir():
            for file in sorted(hdir.glob("*_handler.py")):
                if not file.name.startswith("_"):
                    names.append(f"{HANDLERS_PACKAGE}.{file.stem}")
    return names


def _collect(module_obj: Any) -> list[type[BaseHandler]]:
    found: list[type[BaseHandler]] = []
    for value in vars(module_obj).values():
        if (
            isinstance(value, type)
            and issubclass(value, BaseHandler)
            and value is not BaseHandler
            and getattr(value, "module", "base") not in ("base", "")
        ):
            found.append(value)
    return found


def load_handlers(force: bool = False) -> dict[str, BaseHandler]:
    """导入并实例化全部 handler，返回 ``{module: handler}``。"""
    global _LOADED
    if _LOADED and not force:
        return _HANDLERS

    _HANDLERS.clear()
    _LOAD_ERRORS.clear()

    for mod_name in discover_module_names():
        try:
            module_obj = importlib.import_module(mod_name)
        except Exception as exc:
            _LOAD_ERRORS[mod_name] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            continue
        for cls in _collect(module_obj):
            try:
                handler = cls()
            except Exception as exc:  # 实例化失败（通常是类属性写错）
                _LOAD_ERRORS[f"{mod_name}.{cls.__name__}"] = f"{type(exc).__name__}: {exc}"
                continue
            _HANDLERS[handler.module] = handler

    _LOADED = True
    return _HANDLERS


def load_errors() -> dict[str, str]:
    load_handlers()
    return dict(_LOAD_ERRORS)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def all_handlers() -> dict[str, BaseHandler]:
    return load_handlers()


def get_handler(module: str) -> Optional[BaseHandler]:
    return load_handlers().get((module or "").strip().lower())


def require_handler(module: str) -> BaseHandler:
    handler = get_handler(module)
    if handler is None:
        raise UnsupportedError(
            f"未注册的模块: {module}",
            hint=f"已注册模块: {', '.join(sorted(load_handlers())) or '(无)'}",
            details={"load_errors": load_errors()},
        )
    return handler


def list_modules() -> list[dict[str, Any]]:
    """模块总览（CLI ``list`` 使用）。"""
    out: list[dict[str, Any]] = []
    for name, handler in sorted(load_handlers().items()):
        out.append(
            {
                "module": name,
                "display_name": handler.display_name,
                "version": handler.version,
                "description": handler.description,
                "extensions": list(handler.extensions),
                "kinds": list(handler.kinds),
                "actions": handler.action_names(),
                "optional_deps": dict(handler.optional_deps),
            }
        )
    return out


def extension_index() -> dict[str, str]:
    """``{扩展名: 模块名}`` 映射表。"""
    index: dict[str, str] = {}
    for name, handler in load_handlers().items():
        for ext in handler.extensions:
            index.setdefault(ext.lower(), name)
    return index


def module_for_kind(kind: str) -> Optional[str]:
    """kind → 模块名：先问 handler 自己，再查静态映射表。"""
    for name, handler in load_handlers().items():
        if handler.can_handle_kind(kind):
            return name
    return KIND_TO_MODULE.get(kind)


# ---------------------------------------------------------------------------
# 自动路由
# ---------------------------------------------------------------------------
def resolve(path: str) -> tuple[str, dict[str, Any]]:
    """给定文件路径，返回 ``(module, sniff_info)``。"""
    info = sniff(path)
    module = module_for_kind(info["kind"])
    if module is None:
        module = extension_index().get(info["ext"])
    if module is None:
        raise UnsupportedError(
            f"没有可处理该文件的模块: {info['name']} (kind={info['kind']}, ext={info['ext']})",
            hint="可用扩展名: " + ", ".join(sorted(extension_index())),
            details=info,
        )
    return module, info


def handler_for(path: str) -> tuple[BaseHandler, dict[str, Any]]:
    module, info = resolve(path)
    return require_handler(module), info
