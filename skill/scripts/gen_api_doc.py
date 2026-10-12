"""从代码自动生成 API 手册（references/api-reference.md）。

为什么用生成的而不是手写
------------------------
动作参数散落在 5 个 handler 的 ``@action(params=...)`` 里，手写文档必然随迭代腐化。
本脚本直接读取注册中心的 ``capabilities()``，保证「文档 = 代码契约」。

用法::

    python scripts/gen_api_doc.py                 # 写入 references/api-reference.md
    python scripts/gen_api_doc.py --stdout        # 打到标准输出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.registry import all_handlers, list_modules  # noqa: E402

HEADER = """# API 参考手册

> **本文件由 `scripts/gen_api_doc.py` 自动生成，请勿手工编辑。**
> 生成命令：`python scripts/gen_api_doc.py`
> 契约来源：各 handler 的 `@action(...)` 声明，与运行时 `cap <module>` 输出完全一致。

## 调用约定

### 三种入口，同一套契约

| 入口 | 形态 |
| --- | --- |
| Python | `from core.dispatcher import run` → `run({...})` |
| 命令行 | `python -m core.cli run -m <module> -a <action> -i <file> [-o <out>] [-p k=v ...]` |
| JSON 请求体 | `python -m core.cli ask '{"module":...,"action":...,"input":...,"options":{...}}'` |

### 请求字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `module` | 否 | 模块名；`auto`（默认）按文件真实类型自动路由 |
| `action` | 是 | 动作名，见下方各模块 |
| `input` | 是（除标注 `needs_input=false` 的动作） | 输入文件路径 |
| `output` | 否 | 目录或文件路径；省略时编辑类动作就地保存、导出类动作写入 `<名称>_out/` |
| `options` | 否 | 动作参数，键值与 `-p k=v` 等价 |

### 响应信封

```json
{
  "ok": true,
  "module": "pptx",
  "action": "read",
  "input": "/abs/path/deck.pptx",
  "output": null,
  "elapsed_ms": 18,
  "warnings": ["非致命问题提示"],
  "artifacts": [{"path": "/abs/out/deck.md", "kind": "markdown", "bytes": 4213, "note": "4.1 KB"}],
  "data": { },
  "error": null
}
```

失败时：`ok=false`、`data={}`、`error={"code","message","hint","details"}`。
错误码：`DEPENDENCY_MISSING` / `INPUT_INVALID` / `UNSUPPORTED_FORMAT` / `ACTION_FAILED` / `PARAM_INVALID` / `INTERNAL_ERROR`。

### 通用约定

- **跨模块统一动词**：`probe`（探测）/ `read`（读取）/ `analyze`（分析）/ `transform`（转换导出）/ `edit`（修改）。
- **`edit` 默认就地保存**，并在原文件旁生成 `<名称>_bak.<后缀>` 备份；`in_place=false` 则输出 `<名称>_edited.<后缀>`。
- **`transform` 默认输出到 `<输入同名>_out/` 目录**，避免污染源文件；用 `-o` 指定位置。
- 布尔参数用 `true` / `false`；`-p` 的值会自动尝试 JSON 解析，因此 `-p bullets=甲,乙` 会被 handler 按逗号拆成列表。

---

"""

FOOTER = """
---

## 附：模块索引

| 模块 | 展示名 | 支持扩展名 | 动作 |
| --- | --- | --- | --- |
"""


def render_module(cap: dict) -> str:
    lines: list[str] = []
    lines.append(f"## 模块 `{cap['module']}` —— {cap['display_name']}\n")
    lines.append(f"**版本**：{cap['version']}  ")
    lines.append(f"**支持类型**：{', '.join(cap['extensions']) or '—'}  ")
    if cap.get("optional_deps"):
        lines.append(f"**可选依赖**：{', '.join(f'`{k}` → {v}' for k, v in cap['optional_deps'].items())}  ")
    lines.append("")
    lines.append(f"{cap['description']}\n")

    for spec in cap["actions"]:
        flags: list[str] = []
        if spec["produces_files"]:
            flags.append("产出文件")
        if spec["modifies_input"]:
            flags.append("会修改输入文件（自动备份）")
        if not spec["needs_input"]:
            flags.append("无需输入文件")
        if spec["aliases"]:
            flags.append("别名：" + ", ".join(f"`{a}`" for a in spec["aliases"]))
        lines.append(f"### `{spec['action']}`" + (f"  _（{'; '.join(flags)}）_" if flags else ""))
        lines.append("")
        lines.append(spec["summary"] or "（无说明）")
        lines.append("")
        params = spec.get("params") or {}
        if params:
            lines.append("| 参数 | 说明 |")
            lines.append("| --- | --- |")
            for key, desc in params.items():
                lines.append(f"| `{key}` | {desc} |")
            lines.append("")
        else:
            lines.append("_无参数。_\n")
    lines.append("---\n")
    return "\n".join(lines)


def build() -> str:
    parts = [HEADER]
    for module in list_modules():
        parts.append(render_module(all_handlers()[module["module"]].capabilities()))
    parts.append(FOOTER)
    parts.append("| 模块 | 展示名 | 支持扩展名 | 动作 |")
    parts.append("| --- | --- | --- | --- |")
    for module in list_modules():
        parts.append(
            f"| `{module['module']}` | {module['display_name']} | "
            f"{', '.join(module['extensions'])} | {', '.join(module['actions'])} |"
        )
    parts.append("")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从代码契约生成 API 手册")
    parser.add_argument("--stdout", action="store_true", help="输出到标准输出而非文件")
    parser.add_argument("-o", "--output", default=str(_ROOT / "references" / "api-reference.md"))
    args = parser.parse_args(argv)

    content = build()
    if args.stdout:
        print(content)
        return 0
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    module_count = len(list_modules())
    print(f"已生成 {target}（{module_count} 个模块，{len(content)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
