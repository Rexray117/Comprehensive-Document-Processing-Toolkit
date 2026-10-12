#!/usr/bin/env python
"""单行 JSON 桥接器 —— 给「不支持 MCP，但能起子进程」的 harness 用。

协议（极简，便于任何语言实现）
----------------------------
* 进程常驻；stdin 每行一个 JSON 对象，stdout 每行一个 JSON 对象；
* 请求里可带 ``id``（任意 JSON 标量），响应会原样回带，便于调用方做请求/响应配对；
* 顶层是 JSON **数组**时，视为批量请求，逐个执行后返回数组；
* ``stdout`` 只输出协议行，业务代码里的 ``print`` 会被引流到 ``stderr``。

支持的请求形态
--------------
    {"id":1, "cmd":"ping"}
    {"id":2, "cmd":"info"}
    {"id":3, "cmd":"capabilities", "module":"pptx"}
    {"id":4, "cmd":"tools", "mode":"generic"}
    {"id":5, "cmd":"doctor"}
    {"id":6, "cmd":"resources"}
    {"id":7, "cmd":"read_resource", "path":"SKILL.md"}
    {"id":8, "cmd":"batch", "action":"probe", "target":"./docs", "kinds":["pptx","docx"]}
    {"id":9, "action":"read", "input":"deck.pptx", "module":"auto", "options":{}}
    [ {...}, {...} ]
    {"id":10, "cmd":"shutdown"}

用法
----
    python bridges/stdio_bridge.py                 # 常驻交互（harness 用）
    python bridges/stdio_bridge.py --once          # 读完 stdin 即退出（脚本用）
    echo '{"action":"probe","input":"deck.pptx"}' | python bridges/stdio_bridge.py --once
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any, IO, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bridges import spec  # noqa: E402
from core import version  # noqa: E402
from core.dispatcher import Request, run_batch, run_request  # noqa: E402
from core.runtime import doctor  # noqa: E402

COMMANDS = ("ping", "info", "version", "capabilities", "tools", "doctor",
            "resources", "read_resource", "batch", "dispatch", "shutdown", "help")


@contextlib.contextmanager
def stdout_to_stderr():
    """业务执行期间把误写的 print 引流到 stderr，保护协议流。"""
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = real


def _resources() -> list[dict[str, Any]]:
    from bridges.mcp_server import RESOURCE_FILES

    out = []
    for relative, description in RESOURCE_FILES:
        path = _ROOT / relative
        if path.exists():
            out.append({"path": relative, "description": description,
                        "bytes": path.stat().st_size})
    return out


def handle(request: dict[str, Any]) -> dict[str, Any]:
    """处理单个请求，返回响应体（不含 id）。"""
    cmd = str(request.get("cmd") or request.get("command") or "").strip().lower()

    if not cmd and request.get("action"):
        cmd = "dispatch"

    if cmd in ("", "help"):
        return {
            "ok": True,
            "commands": list(COMMANDS),
            "note": "带 action 字段的请求会被当作 dispatch 处理",
            "package": version.as_dict(),
        }

    if cmd == "ping":
        return {"ok": True, "pong": True, "package": version.SLUG, "version": version.VERSION}

    if cmd in ("info", "version"):
        return {"ok": True, "package": version.as_dict()}

    if cmd == "capabilities":
        from core.registry import all_handlers

        module = (request.get("module") or "").strip().lower()
        handlers = all_handlers()
        if module:
            handler = handlers.get(module)
            if handler is None:
                return {"ok": False, "error": {"code": "PARAM_INVALID",
                                               "message": f"未知模块: {module}",
                                               "hint": f"可用模块: {sorted(handlers)}"}}
            return {"ok": True, "data": handler.capabilities()}
        return {"ok": True, "data": {"package": version.as_dict(),
                                     "modules": [h.capabilities() for h in handlers.values()]}}

    if cmd == "tools":
        mode = str(request.get("mode") or "generic")
        tools = spec.build_tools(mode)
        if request.get("format") == "openai":
            tools = spec.to_openai_tools(tools)  # type: ignore[assignment]
        return {"ok": True, "mode": mode, "count": len(tools), "tools": tools}

    if cmd == "doctor":
        report = doctor(verbose=bool(request.get("verbose", False)))
        return {"ok": bool(report.get("healthy")), "data": report}

    if cmd == "resources":
        return {"ok": True, "root": str(_ROOT), "resources": _resources()}

    if cmd == "read_resource":
        relative = request.get("path") or request.get("uri")
        if not relative:
            return {"ok": False, "error": {"code": "PARAM_INVALID", "message": "需要 path 参数"}}
        text = str(relative)
        path = Path(text.replace("file:///", "").replace("file://", ""))
        if not path.is_absolute():
            path = _ROOT / text
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": {"code": "INPUT_INVALID",
                                           "message": f"无法读取: {path}",
                                           "details": {"reason": str(exc)},
                                           "hint": "用 cmd=resources 查看可读清单"}}
        return {"ok": True, "path": str(path), "bytes": len(content.encode("utf-8")),
                "text": content}

    if cmd == "batch":
        action = str(request.get("action") or "")
        target = request.get("target") or request.get("input")
        if not action or not target:
            return {"ok": False, "error": {"code": "PARAM_INVALID",
                                           "message": "batch 需要 action 与 target"}}
        payload = run_batch(
            target,
            action,
            module=str(request.get("module") or "auto"),
            options=dict(request.get("options") or {}),
            output=request.get("output"),
            recursive=bool(request.get("recursive", True)),
            kinds=request.get("kinds"),
            limit=request.get("limit"),
        )
        return {"ok": bool(payload.get("ok")), "data": payload}

    if cmd == "dispatch":
        return run_request(
            Request(
                action=str(request.get("action") or ""),
                input=request.get("input") or request.get("path"),
                module=str(request.get("module") or "auto"),
                output=request.get("output"),
                options=dict(request.get("options") or {}),
            )
        )

    if cmd == "shutdown":
        return {"ok": True, "bye": True}

    return {
        "ok": False,
        "error": {
            "code": "UNSUPPORTED",
            "message": f"未知命令: {cmd}",
            "hint": f"可用命令: {', '.join(COMMANDS)}；或直接给 action 字段触发 dispatch",
        },
    }


def process_line(line: str) -> tuple[Optional[dict[str, Any]], bool]:
    """返回 (响应对象, 是否要求退出)。"""
    text = line.strip()
    if not text:
        return None, False

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"id": None, "ok": False,
                "error": {"code": "PARSE_ERROR", "message": f"JSON 解析失败: {exc.msg}",
                          "details": {"line": exc.lineno, "column": exc.colno}}}, False

    if isinstance(payload, list):
        results = []
        shutdown = False
        for item in payload:
            if not isinstance(item, dict):
                results.append({"id": None, "ok": False,
                                "error": {"code": "INVALID_REQUEST", "message": "批量元素必须是对象"}})
                continue
            body, flag = _handle_one(item)
            results.append(body)
            shutdown = shutdown or flag
        return {"id": None, "batch": True, "count": len(results), "results": results}, shutdown

    if not isinstance(payload, dict):
        return {"id": None, "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "请求必须是 JSON 对象或数组"}}, False

    return _handle_one(payload)


def _handle_one(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    msg_id = payload.get("id")
    cmd = str(payload.get("cmd") or "").strip().lower()
    try:
        with stdout_to_stderr():
            body = handle(payload)
    except Exception as exc:
        body = {"ok": False,
                "error": {"code": "INTERNAL_ERROR",
                          "message": f"{type(exc).__name__}: {exc}",
                          "details": {"traceback": traceback.format_exc(limit=4)}}}
    body = {"id": msg_id, **body}
    return body, cmd == "shutdown"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{version.NAME} {version.RELEASE} —— 单行 JSON 桥接器")
    parser.add_argument("--once", action="store_true", help="读完 stdin 即退出（不常驻）")
    parser.add_argument("--pretty", action="store_true", help="响应美化缩进（仅 --once 时建议使用）")
    args = parser.parse_args(argv)

    stream_in = sys.stdin
    dropped_blank = 0

    while True:
        line = stream_in.readline()
        if not line:
            break
        response, shutdown = process_line(line)
        if response is None:
            dropped_blank += 1
            if args.once:
                continue
            continue
        if args.pretty:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, indent=2, default=str) + "\n")
        else:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
        if shutdown:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
