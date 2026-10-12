#!/usr/bin/env python
"""生成工具契约产物（供 harness 静态注册，避免运行时再解析）。

产物
----
* ``bridges/tool_specs.json``            —— MCP 形态（generic + granular 两套）
* ``harness/openai_tools.generic.json``  —— OpenAI / 兼容 Chat Completions 的 function-calling
* ``harness/openai_tools.granular.json``
* ``harness/langchain_tools.generic.json``
* ``harness/TOOL_INDEX.md``              —— 人类可读的工具索引

用法::

    python scripts/gen_tool_specs.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bridges import spec  # noqa: E402
from core import version  # noqa: E402


def _dump(data: Any, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    target.write_text(text + "\n", encoding="utf-8")
    return len(text)


def build_tool_index(generic: list[dict], granular: list[dict]) -> str:
    lines: list[str] = [
        "# 工具索引",
        "",
        f"> 由 `scripts/gen_tool_specs.py` 自动生成 —— {version.NAME} {version.RELEASE}",
        f"> 契约来源：各 handler 的 `@action(...)` 声明，与运行时 `python -m core.cli cap <module>` 一致。",
        "",
        "## 模式一：通用工具（默认，推荐）",
        "",
        f"共 {len(generic)} 个工具，覆盖全部能力，上下文占用最小。",
        "",
        "| 工具名 | 说明 | 必填参数 |",
        "| --- | --- | --- |",
    ]
    for tool in generic:
        schema = tool.get("inputSchema", {})
        required = ", ".join(f"`{r}`" for r in schema.get("required", [])) or "—"
        summary = tool["description"].split("。")[0].replace("|", "/")
        lines.append(f"| `{tool['name']}` | {summary}。 | {required} |")

    lines += [
        "",
        "## 模式二：细粒度工具",
        "",
        f"共 {len(granular)} 个工具（每模块每动作一个），参数已按类型展开，适合弱模型或需要强约束的场景。",
        "",
        "| 工具名 | 说明 | 参数 |",
        "| --- | --- | --- |",
    ]
    for tool in granular:
        props = tool.get("inputSchema", {}).get("properties", {})
        params = ", ".join(f"`{k}`" for k in props if k not in ("input", "output")) or "—"
        lines.append(f"| `{tool['name']}` | {tool['description'].replace('|', '/')} | {params} |")

    lines += [
        "",
        "## 切换方式",
        "",
        "```bash",
        "# MCP：默认通用模式",
        "python bridges/mcp_server.py",
        "# MCP：细粒度模式",
        "python bridges/mcp_server.py --granular",
        "# 导出其它格式",
        'python bridges/stdio_bridge.py --once <<< \'{"cmd":"tools","format":"openai"}\'',
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 harness 可直接注册的工具契约产物")
    parser.add_argument("-o", "--root", default=str(_ROOT), help="技能根目录")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    generic = spec.build_generic_tools()
    granular = spec.build_granular_tools()

    written: list[tuple[str, int]] = []

    written.append(("bridges/tool_specs.json", _dump(
        {
            "package": version.as_dict(),
            "summary": spec.tool_summary(),
            "generic": generic,
            "granular": granular,
        },
        root / "bridges" / "tool_specs.json",
    )))

    written.append(("harness/openai_tools.generic.json", _dump(
        {"package": version.as_dict(), "tools": spec.to_openai_tools(generic)},
        root / "harness" / "openai_tools.generic.json",
    )))
    written.append(("harness/openai_tools.granular.json", _dump(
        {"package": version.as_dict(), "tools": spec.to_openai_tools(granular)},
        root / "harness" / "openai_tools.granular.json",
    )))
    written.append(("harness/langchain_tools.generic.json", _dump(
        {"package": version.as_dict(), "tools": spec.to_langchain_tools(generic)},
        root / "harness" / "langchain_tools.generic.json",
    )))

    index = build_tool_index(generic, granular)
    (root / "harness").mkdir(parents=True, exist_ok=True)
    (root / "harness" / "TOOL_INDEX.md").write_text(index, encoding="utf-8")
    written.append(("harness/TOOL_INDEX.md", len(index)))

    print(f"{version.NAME} {version.RELEASE} —— 工具契约产物")
    for relative, size in written:
        print(f"  ✓ {relative:<46} {size:>7} 字符")
    print(f"\n通用工具 {len(generic)} 个 / 细粒度工具 {len(granular)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
