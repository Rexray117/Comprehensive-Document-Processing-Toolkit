#!/usr/bin/env python
"""MCP（Model Context Protocol）stdio 服务器 —— 把本技能直接挂载为 Agent 的工具集。

为什么提供 MCP
--------------
MCP 是当前 Agent / harness 生态最通用的工具接入协议（Claude Desktop / Claude Code /
各类支持 MCP 的 IDE 与编排框架）。实现了它，集成方只需在配置里加一段 JSON，
无需了解本项目的任何 Python 细节。

协议要点
--------
* 传输：stdio，**消息按行分隔的 JSON-RPC 2.0**（兼容旧式 ``Content-Length`` 头帧）；
* ``stdout`` 只允许出现协议消息 —— 服务器运行期间会把业务代码里任何误写的
  ``print`` 重定向到 ``stderr``，避免污染协议流；
* 实现方法：``initialize`` / ``tools/list`` / ``tools/call`` / ``resources/list`` /
  ``resources/read`` / ``ping``，并正确忽略 ``notifications/*``。

用法
----
    python bridges/mcp_server.py                      # 以通用 4 工具模式启动（推荐）
    python bridges/mcp_server.py --granular           # 展开为每动作一个工具
    python bridges/mcp_server.py --print-config       # 打印可直接粘贴的 harness 配置
    python bridges/mcp_server.py --list-tools         # 打印工具定义
    python bridges/mcp_server.py --selftest           # 协议自检（CI 可用）
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, IO, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bridges import spec  # noqa: E402
from core import version  # noqa: E402
from core.dispatcher import Request, run_batch, run_request  # noqa: E402

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

DEBUG = bool(os.environ.get("OMNI_MCP_DEBUG"))

#: 作为 MCP 资源对外暴露的文件（相对技能根目录）
RESOURCE_FILES: tuple[tuple[str, str], ...] = (
    ("SKILL.md", "技能主文档：使用场景、动作语义、全部命令示例"),
    ("README.md", "项目说明：目录结构与快速开始"),
    ("MANIFEST.json", "机器可读的清单：模块、动作、依赖、集成入口"),
    ("bridges/INTEGRATION.md", "集成指南：MCP / stdio / function-calling 三种接法"),
    ("references/api-reference.md", "完整 API 手册（由代码契约自动生成）"),
    ("references/architecture.md", "分层架构与设计取舍"),
    ("references/extension-guide.md", "扩展新模块 / 动作 / OCR 后端"),
)

INSTRUCTIONS = f"""这是「{version.NAME}」（{version.RELEASE}）提供的文件处理工具集，覆盖
PPTX 演示文稿、DOCX 文档、XLSX 工作表、HTML 网页、PNG/JPG 等图片（含 OCR 图像内容识别）。

使用要点：
1. 五类文件共用同一套动作语义 —— probe（探测）/ read（读取）/ analyze（分析）/
   transform（转换导出）/ edit（修改）。先 probe 或 read 摸清结构，再决定是否 transform/edit。
2. module 参数一般传 "auto"，会按文件真实类型自动路由；只有需要强制指定时才填具体模块。
3. edit 默认就地修改输入文件并自动生成 _bak 备份。要保留原文件请传 output 或 options.in_place=false。
4. 不确定某个动作该传什么参数时，先调用 file_capabilities 查契约，不要猜参数名。
5. 遇到依赖缺失或 OCR 不可用类报错，先调用 file_doctor —— 它会直接给出安装命令。
6. transform / edit 的产物路径在返回信封的 artifacts 字段中，可直接引用。
7. 图片语义理解（图里画了什么）请用 analyze 并传 options.vision_payload=true，
   返回的 vision_bridge.data_uri 可交给你的多模态能力继续处理。
"""


# ---------------------------------------------------------------------------
# 协议 I/O
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    """调试日志一律走 stderr，绝不污染 stdout 协议流。"""
    if DEBUG:
        sys.stderr.write(f"[omni-mcp] {message}\n")
        sys.stderr.flush()


@contextlib.contextmanager
def stdout_to_stderr():
    """在执行业务代码期间，把误写的 print 引流到 stderr。

    stdio 协议服务器最容易被忽视的崩溃原因就是「某处不小心 print 了一行」，
    它会让客户端收到一个非法消息并断开连接。这里做无条件兜底。
    """
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = real


def read_message(stream: IO[bytes]) -> Optional[dict[str, Any]]:
    """读取一条消息，同时兼容「按行分隔」与「Content-Length 头帧」两种编码。"""
    line = stream.readline()
    if not line:
        return None
    if line.lower().startswith(b"content-length:"):
        try:
            length = int(line.split(b":", 1)[1].strip())
        except (ValueError, IndexError):
            raise ValueError(f"非法的 Content-Length 头: {line!r}")
        while True:  # 跳过其余头部直到空行
            header = stream.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        body = stream.read(length)
        return json.loads(body.decode("utf-8"))
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return json.loads(text)


def write_message(payload: dict[str, Any], stream: Optional[IO[str]] = None) -> None:
    """以单行 JSON 写回一条消息（MCP stdio 标准编码）。"""
    target = stream or sys.stdout
    target.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    target.flush()


def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _text_result(text: str, *, is_error: bool = False,
                 structured: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        # 新版客户端可读的结构化结果；未声明 outputSchema，老客户端会安全忽略
        result["structuredContent"] = structured
    if is_error:
        result["isError"] = True
    return result


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------
class McpServer:
    def __init__(self, *, mode: str = "generic") -> None:
        self.mode = mode
        self.tools = spec.build_tools(mode)
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        self.protocol_version = version.MCP_PROTOCOL_VERSIONS[0]

    # ------------------------------------------------------------ 工具分发
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        args = dict(arguments or {})

        if name == "file_capabilities":
            return self._tool_capabilities(args)
        if name == "file_doctor":
            return self._tool_doctor(args)
        if name == "file_batch":
            return self._tool_batch(args)
        if name == "file_dispatch":
            return self._tool_dispatch(args)

        if name.startswith("file_"):
            parts = name.split("_", 2)
            if len(parts) == 3:
                module_name, action = parts[1], parts[2]
                args.setdefault("module", module_name)
                args.setdefault("action", action)
                return self._tool_dispatch(args)

        return _text_result(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "METHOD_NOT_FOUND",
                        "message": f"未知工具: {name}",
                        "hint": "可用工具见 tools/list；通用模式下的工具名以 file_ 开头。",
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            is_error=True,
        )

    def _tool_capabilities(self, args: dict[str, Any]) -> dict[str, Any]:
        from core.registry import all_handlers

        module = (args.get("module") or "").strip().lower()
        handlers = all_handlers()
        if module:
            handler = handlers.get(module)
            if handler is None:
                return _text_result(
                    json.dumps(
                        {
                            "ok": False,
                            "error": {
                                "code": "PARAM_INVALID",
                                "message": f"未知模块: {module}",
                                "hint": f"可用模块: {sorted(handlers)}",
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    is_error=True,
                )
            payload: Any = handler.capabilities()
        else:
            payload = {
                "package": version.as_dict(),
                "modules": [h.capabilities() for h in handlers.values()],
            }
        return _text_result(
            json.dumps(payload, ensure_ascii=False, indent=2),
            structured={"ok": True, "data": payload},
        )

    def _tool_doctor(self, args: dict[str, Any]) -> dict[str, Any]:
        from core.runtime import doctor

        report = doctor(verbose=bool(args.get("verbose", False)))
        return _text_result(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            structured={"ok": bool(report.get("healthy")), "data": report},
        )

    def _tool_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "").strip()
        target = args.get("target") or args.get("input")
        if not action or not target:
            return _text_result(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "PARAM_INVALID",
                            "message": "file_batch 需要 action 与 target 参数",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                is_error=True,
            )
        payload = run_batch(
            target,
            action,
            module=str(args.get("module") or "auto"),
            options=dict(args.get("options") or {}),
            output=args.get("output"),
            recursive=bool(args.get("recursive", True)),
            kinds=args.get("kinds"),
            limit=args.get("limit"),
        )
        summary = {k: v for k, v in payload.items() if k != "results"}
        text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
        text += f"\n\n逐文件结果（{len(payload.get('results', []))} 条）:\n"
        text += json.dumps(
            [
                {
                    "input": r.get("input"),
                    "module": r.get("module"),
                    "ok": r.get("ok"),
                    "error": (r.get("error") or {}).get("message"),
                }
                for r in payload.get("results", [])
            ],
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        return _text_result(text, is_error=not payload.get("ok"),
                            structured={"ok": bool(payload.get("ok")), "data": summary})

    def _tool_dispatch(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action") or "").strip()
        source = args.get("input") or args.get("path")
        if not action:
            return _text_result(
                json.dumps(
                    {"ok": False, "error": {"code": "PARAM_INVALID", "message": "缺少 action 参数"}},
                    ensure_ascii=False, indent=2,
                ),
                is_error=True,
            )
        if not source:
            return _text_result(
                json.dumps(
                    {"ok": False, "error": {"code": "PARAM_INVALID", "message": "缺少 input 参数"}},
                    ensure_ascii=False, indent=2,
                ),
                is_error=True,
            )

        options = dict(args.get("options") or {})
        # 细粒度模式下，参数是平铺的；把除控制字段外的键都并入 options
        reserved = {"action", "input", "path", "module", "output", "options"}
        for key, value in args.items():
            if key not in reserved and value is not None:
                options.setdefault(key, value)

        request = Request(
            action=action,
            input=str(source),
            module=str(args.get("module") or "auto"),
            output=args.get("output"),
            options=options,
        )
        with stdout_to_stderr():
            envelope = run_request(request)

        # 产物单独提示，方便 Agent 直接引用路径
        artifacts = envelope.get("artifacts") or []
        text = json.dumps(envelope, ensure_ascii=False, indent=2, default=str)
        if artifacts:
            text += "\n\n产出文件:\n" + "\n".join(
                f"- [{a.get('kind')}] {a.get('path')}" for a in artifacts
            )
        return _text_result(text, is_error=not envelope.get("ok"),
                            structured={"ok": bool(envelope.get("ok")), "data": envelope})

    # ------------------------------------------------------------ 资源
    def list_resources(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for relative, description in RESOURCE_FILES:
            path = _ROOT / relative
            if not path.exists():
                continue
            out.append(
                {
                    "uri": path.as_uri(),
                    "name": path.name,
                    "title": relative,
                    "description": description,
                    "mimeType": "application/json" if path.suffix == ".json" else "text/markdown",
                }
            )
        return out

    def read_resource(self, uri: str) -> Optional[dict[str, Any]]:
        for entry in self.list_resources():
            if entry["uri"] != uri:
                continue
            try:
                parsed = urlparse(uri)
                path = Path(url2pathname(parsed.path))
                if not path.is_absolute():  # Windows: file:///D:/... → /D:/... 需去掉前导斜杠
                    path = Path(url2pathname(uri.replace("file://", "")))
                text = path.read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:
                log(f"resource read failed for {uri}: {exc}")
                return None
            return {"uri": uri, "mimeType": entry["mimeType"], "text": text}
        return None

    # ------------------------------------------------------------ 消息处理
    def handle(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not isinstance(message, dict):
            return _err(None, INVALID_REQUEST, "消息必须是 JSON 对象")

        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if not method:
            return _err(msg_id, INVALID_REQUEST, "缺少 method 字段")

        if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
            self.initialized = True
            return None

        if method == "initialize":
            requested = params.get("protocolVersion")
            # 协商策略：客户端版本受支持则顺着它，否则回落到我们最新的支持版本
            self.protocol_version = (
                requested
                if requested in version.MCP_PROTOCOL_VERSIONS
                else version.MCP_PROTOCOL_VERSIONS[0]
            )
            self.client_info = params.get("clientInfo") or {}
            log(f"initialize from {self.client_info} -> protocol {self.protocol_version}")
            return _ok(
                msg_id,
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {
                        "name": version.SLUG,
                        "title": version.NAME,
                        "version": version.VERSION,
                    },
                    "instructions": INSTRUCTIONS,
                },
            )

        if method == "ping":
            return _ok(msg_id, {})

        if method == "tools/list":
            tools = []
            for tool in self.tools:
                tools.append({k: v for k, v in tool.items() if not k.startswith("_")})
            return _ok(msg_id, {"tools": tools})

        if method == "tools/call":
            name = params.get("name")
            if not name:
                return _err(msg_id, INVALID_PARAMS, "tools/call 需要 name 参数")
            try:
                return _ok(msg_id, self.call_tool(str(name), params.get("arguments") or {}))
            except Exception as exc:  # 工具内部异常也要变成工具级错误，而不是崩掉连接
                tb = traceback.format_exc(limit=5)
                log(f"tool {name} crashed: {tb}")
                return _ok(
                    msg_id,
                    _text_result(
                        json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "INTERNAL_ERROR",
                                    "message": f"{type(exc).__name__}: {exc}",
                                    "hint": "可调用 file_doctor 检查环境；若为参数问题请用 file_capabilities 核对参数名。",
                                },
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        is_error=True,
                    ),
                )

        if method == "resources/list":
            return _ok(msg_id, {"resources": self.list_resources()})

        if method == "resources/read":
            uri = params.get("uri")
            if not uri:
                return _err(msg_id, INVALID_PARAMS, "resources/read 需要 uri 参数")
            content = self.read_resource(str(uri))
            if content is None:
                return _err(msg_id, INVALID_PARAMS, f"资源不存在或不可读: {uri}")
            return _ok(msg_id, {"contents": [content]})

        if method in ("prompts/list", "logging/setLevel", "completion/complete"):
            # 声明未支持的次要能力：返回空结果而不是报错，避免客户端初始化失败
            if method == "prompts/list":
                return _ok(msg_id, {"prompts": []})
            return None if is_notification else _ok(msg_id, {})

        if is_notification:
            return None
        return _err(msg_id, METHOD_NOT_FOUND, f"未实现的方法: {method}")


# ---------------------------------------------------------------------------
# 运行
# ---------------------------------------------------------------------------
def serve(*, mode: str = "generic", stdin: Optional[IO[bytes]] = None,
          stdout: Optional[IO[str]] = None) -> int:
    stream_in = stdin or sys.stdin.buffer
    server = McpServer(mode=mode)
    log(f"server ready (mode={mode}, tools={len(server.tools)})")

    while True:
        try:
            message = read_message(stream_in)
        except ValueError as exc:
            write_message(_err(None, PARSE_ERROR, str(exc)), stdout)
            continue
        except json.JSONDecodeError as exc:
            write_message(_err(None, PARSE_ERROR, f"JSON 解析失败: {exc}"), stdout)
            continue
        except Exception as exc:  # EOF 之外的读取异常
            log(f"read failed: {type(exc).__name__}: {exc}")
            break

        if message is None:
            break  # 客户端关闭了 stdin

        try:
            response = server.handle(message)
        except Exception as exc:
            log(f"handle crashed: {traceback.format_exc(limit=5)}")
            response = _err(message.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        if response is not None:
            write_message(response, stdout)

    log("server exiting")
    return 0


def print_config(mode: str = "generic") -> int:
    """打印机可直接粘贴的 MCP 配置片段。"""
    python = sys.executable
    server = str(Path(__file__).resolve())
    config = {
        "mcpServers": {
            version.SLUG: {
                "command": python,
                "args": [server] + (["--granular"] if mode == "granular" else []),
                "env": {},
            }
        }
    }
    print(f"# {version.NAME} {version.RELEASE} —— MCP 服务器配置")
    print("# 把下面这段合并进 harness 的 MCP 配置（如 Claude Desktop 的 claude_desktop_config.json）\n")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    print(f"\n# 若 harness 需要「包在容器/沙箱里运行」，请把 command 指向该环境内的 python，")
    print(f"# 并确保已安装依赖：{python} -m pip install -r {_ROOT / 'requirements.txt'}")
    return 0


def selftest(mode: str = "generic", *, verbose: bool = False) -> int:
    """协议自检：不依赖外部客户端，直接喂 JSON-RPC 消息验证全链路。"""
    server = McpServer(mode=mode)
    cases: list[tuple[str, dict[str, Any], Any]] = []

    def expect(label: str, message: dict[str, Any], check) -> None:
        cases.append((label, message, check))  # type: ignore[arg-type]

    tmp_ctx = tempfile.TemporaryDirectory(prefix="omni_mcp_selftest_")
    try:
        tmp = Path(tmp_ctx.name)
        sample = tmp / "sample.html"
        sample.write_text(
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>集成自检样例</title>"
            "<meta name=\"description\" content=\"用于验证 MCP 工具链路\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "</head><body><h1>集成自检</h1><p>风险等级为高，建议限制飞行。</p>"
            "<table><tr><th>因子</th><th>取值</th></tr><tr><td>风速</td><td>12.4 m/s</td></tr></table>"
            "</body></html>",
            encoding="utf-8",
        )

        expect("initialize", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "selftest", "version": "0"}}},
               lambda r: r["result"]["serverInfo"]["version"] == version.VERSION
               and r["result"]["protocolVersion"] == "2025-06-18")

        expect("notifications/initialized", {"jsonrpc": "2.0", "method": "notifications/initialized"},
               lambda r: r is None)

        expect("tools/list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
               lambda r: isinstance(r["result"]["tools"], list) and len(r["result"]["tools"]) > 0
               and all("_meta" not in t for t in r["result"]["tools"]))

        expect("ping", {"jsonrpc": "2.0", "id": 3, "method": "ping"}, lambda r: r["result"] == {})

        expect("resources/list", {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
               lambda r: len(r["result"]["resources"]) >= 3)

        expect("resources/read", {"jsonrpc": "2.0", "id": 5, "method": "resources/read",
                                  "params": {"uri": (_ROOT / "SKILL.md").as_uri()}},
               lambda r: "contents" in r["result"] and len(r["result"]["contents"][0]["text"]) > 100)

        expect("method not found", {"jsonrpc": "2.0", "id": 6, "method": "no/such/method"},
               lambda r: r["error"]["code"] == METHOD_NOT_FOUND)

        expect("file_capabilities", {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                     "params": {"name": "file_capabilities", "arguments": {}}},
               lambda r: "modules" in r["result"]["content"][0]["text"])

        if mode == "generic":
            expect("file_dispatch(read)", {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                           "params": {"name": "file_dispatch",
                                                      "arguments": {"action": "read",
                                                                    "input": str(sample)}}},
                   lambda r: r["result"].get("isError") is not True
                   and r["result"]["structuredContent"]["data"]["module"] == "html"
                   and r["result"]["structuredContent"]["data"]["ok"] is True)
        else:
            expect("file_html_read", {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                      "params": {"name": "file_html_read",
                                                 "arguments": {"input": str(sample)}}},
                   lambda r: r["result"].get("isError") is not True
                   and r["result"]["structuredContent"]["data"]["module"] == "html")

        expect("transform 产出文件", {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                     "params": {"name": "file_dispatch",
                                                "arguments": {"action": "transform",
                                                              "input": str(sample),
                                                              "output": str(tmp / "out"),
                                                              "options": {"op": "to_markdown"}}}},
               lambda r: r["result"].get("isError") is not True
               and len(r["result"]["structuredContent"]["data"]["artifacts"]) >= 1)

        expect("edit 保底不破坏原文件", {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                        "params": {"name": "file_dispatch",
                                                   "arguments": {"action": "edit", "input": str(sample),
                                                                 "options": {"op": "set_title",
                                                                             "text": "改后的标题",
                                                                             "in_place": False}}}},
               lambda r: r["result"].get("isError") is not True
               and sample.exists()
               and "改后的标题" in (tmp / "sample_edited.html").read_text(encoding="utf-8"))

        expect("file_doctor", {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                               "params": {"name": "file_doctor", "arguments": {}}},
               lambda r: "python" in json.dumps(r).lower())

        expect("未知工具", {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                            "params": {"name": "no_such_tool", "arguments": {}}},
               lambda r: r["result"].get("isError") is True)

        passed = failed = 0
        print(f"== {version.NAME} {version.RELEASE} MCP 协议自检 (mode={mode}) ==")
        for label, message, check in cases:
            try:
                response = server.handle(message)
                ok = bool(check(response))
                if verbose and not ok:
                    print(f"  实际响应: {json.dumps(response, ensure_ascii=False)[:700]}")
            except Exception as exc:
                ok = False
                if verbose:
                    print(f"  异常: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            passed += ok
            failed += not ok
    finally:
        tmp_ctx.cleanup()

    print(f"\n通过 {passed} / {passed + failed}")
    return 0 if failed == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"{version.NAME} {version.RELEASE} —— MCP stdio 服务器")
    parser.add_argument("--granular", action="store_true",
                        help="展开为每动作一个工具（默认：通用 4 工具）")
    parser.add_argument("--print-config", action="store_true", help="打印 harness 配置片段后退出")
    parser.add_argument("--list-tools", action="store_true", help="打印工具定义后退出")
    parser.add_argument("--selftest", action="store_true", help="运行协议自检后退出")
    parser.add_argument("-v", "--verbose", action="store_true", help="自检失败时输出详细响应")
    args = parser.parse_args(argv)

    mode = "granular" if args.granular else "generic"

    if args.print_config:
        return print_config(mode)
    if args.list_tools:
        print(json.dumps(spec.build_tools(mode), ensure_ascii=False, indent=2))
        return 0
    if args.selftest:
        return selftest(mode, verbose=args.verbose)
    return serve(mode=mode)


if __name__ == "__main__":
    raise SystemExit(main())
