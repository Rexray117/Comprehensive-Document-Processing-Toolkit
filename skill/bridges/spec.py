"""工具契约生成层：把 5 个模块的 ``@action`` 声明翻译成 LLM / harness 可直接消费的工具定义。

为什么要单独一层
----------------
Agent 集成的本质是「把能力描述成工具，让模型决定何时调用」。工具描述写得含糊，
模型就会乱调；写得太多，又会挤爆上下文。本层只做一件事：**从代码契约派生工具定义**，
保证工具描述与实现永远一致，且能在「4 个通用工具」与「25 个细粒度工具」之间切换。

两种模式
--------
* ``generic``（默认）：4 个工具覆盖全部能力，上下文占用最小，适合工具预算紧张的 harness。
* ``granular``：每个 (模块, 动作) 一个工具，参数已按类型展开，适合弱模型或需要强约束的场景。

两者使用同一套底层调度，行为完全一致。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from core import version
from core.registry import all_handlers, list_modules

# ---------------------------------------------------------------------------
# 参数类型推断
# ---------------------------------------------------------------------------
_BOOL_EXACT = {
    "deep", "ocr", "regex", "ignore_case", "verbose", "bold", "italic", "border", "wrap",
    "in_place", "recursive", "unique", "unmerge", "titles_from_data", "categories",
    "embed_base64", "keep_scripts", "auto_orient", "binarize", "vision_payload",
    "progressive", "expand", "main_only", "notes", "include_text", "keep_aspect",
    "strip_exif", "has_header", "corr", "append", "overwrite", "transpose", "dropna",
}
_BOOL_PREFIX = ("include_", "keep_", "allow_", "enable_", "use_", "is_", "has_", "show_", "exclude_")
_INT_EXACT = {
    "top_n", "limit", "count", "start", "quality", "angle", "width", "height", "sample_rows",
    "header_row", "max_rows", "max_cols", "palette_size", "max_side", "radius", "block",
    "opacity", "compress_level", "level", "layout_index", "font_scale", "size", "cols",
    "stroke", "season", "precision", "scale", "dpi", "depth",
}
_FLOAT_EXACT = {"percent", "threshold", "min_confidence", "ocr_min_confidence", "aspect_ratio"}
_ARRAY_EXACT = {"bullets", "columns", "kinds", "tags", "names", "headers"}
_MULTI_TYPE = {
    "slide": ["integer", "array"],
    "rows": ["array", "string"],
    "range": ["string", "array"],
    "box": ["string", "array"],
    "at": ["string", "array"],
    "value": ["string", "number", "boolean", "null"],
    "content": ["string", "number", "boolean", "null"],
    "agg": ["string", "array"],
}
_KEY_ALIASES = {"output": "output_dir"}


def infer_schema(key: str, description: str) -> dict[str, Any]:
    """根据参数名与说明推断 JSON Schema 片段。

    采用「命名约定 + 显式白名单」而不是让作者手写 schema —— 动作声明里
    已经有参数说明，再要求手写类型会显著抬高扩展成本，且容易与实现不一致。
    """
    lowered = key.lower()
    if lowered in _MULTI_TYPE:
        return {"anyOf": [{"type": t} for t in _MULTI_TYPE[lowered]]}
    if lowered in _ARRAY_EXACT:
        return {"type": "array", "items": {"type": "string"}}
    if lowered in _BOOL_EXACT or lowered.startswith(_BOOL_PREFIX):
        return {"type": "boolean"}
    if lowered in _INT_EXACT:
        return {"type": "integer"}
    if lowered in _FLOAT_EXACT:
        return {"type": "number"}
    if lowered.endswith(("_in", "_px", "_ms")):
        return {"type": "number"}
    return {"type": "string"}


# ---------------------------------------------------------------------------
# 通用工具（4 个）
# ---------------------------------------------------------------------------
def _actions_enum() -> list[str]:
    verbs = ["probe", "read", "analyze", "transform", "edit"]
    extra = {
        spec.name
        for handler in all_handlers().values()
        for spec in handler._actions.values()  # noqa: SLF001
    }
    return sorted(set(verbs) | extra)


def build_generic_tools() -> list[dict[str, Any]]:
    modules = ["auto"] + sorted(all_handlers())
    return [
        {
            "name": "file_dispatch",
            "description": (
                f"{version.NAME}：{version.SUMMARY}。"
                "五类文件（.pptx .docx .xlsx .html .png/.jpg 等）统一走同一套动作语义，"
                "module 传 auto 时按文件真实类型自动路由。"
                "动作含义：probe=极速探测结构；read=结构化读取内容；"
                "analyze=内容识别与质量诊断（图片可开 OCR）；"
                "transform=转换导出新文件（Markdown/CSV/JSON/单文件 HTML 等）；"
                "edit=修改文件，默认就地保存并自动生成 _bak 备份。"
                "返回统一信封 {ok, module, action, warnings, artifacts, data, error}。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _actions_enum(),
                        "description": "要执行的动作。模块特有动作也可用（见 file_capabilities）。",
                    },
                    "input": {"type": "string", "description": "输入文件的绝对路径"},
                    "module": {
                        "type": "string",
                        "enum": modules,
                        "default": "auto",
                        "description": "处理模块；auto 表示按文件类型自动识别（推荐）",
                    },
                    "output": {
                        "type": "string",
                        "description": "输出目录或文件路径。省略时：编辑类就地保存，导出类写入 <文件名>_out/",
                    },
                    "options": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "动作参数键值对，例如 {\"op\":\"to_markdown\"}、"
                            "{\"op\":\"replace_text\",\"find\":\"旧\",\"replace\":\"新\"}、"
                            "{\"ocr\":true,\"ocr_langs\":\"chi_sim+eng\"}。"
                            "各动作的完整参数表用 file_capabilities 查询。"
                        ),
                    },
                },
                "required": ["action", "input"],
            },
        },
        {
            "name": "file_capabilities",
            "description": (
                "查询本技能支持的模块与动作契约（含每个动作的全部参数说明）。"
                "在不确定某个动作该传什么参数时先调用它，而不是猜测参数名。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "description": "模块名如 pptx/docx/xlsx/html/image；省略则返回全部模块",
                    }
                },
            },
        },
        {
            "name": "file_doctor",
            "description": (
                "环境自检：Python 版本、依赖是否齐备（缺失时直接给出 pip 安装命令）、"
                "OCR 后端是否可用、可用模块列表。遇到依赖报错时先调用它。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "verbose": {"type": "boolean", "description": "输出完整依赖明细（默认 false）"}
                },
            },
        },
        {
            "name": "file_batch",
            "description": (
                "对目录批量执行同一动作，例如「把 ./docs 下所有 pptx 和 docx 都体检一遍」。"
                "返回逐文件结果与汇总统计。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": _actions_enum()},
                    "target": {"type": "string", "description": "目标目录（或单个文件）的绝对路径"},
                    "module": {"type": "string", "default": "auto"},
                    "output": {"type": "string", "description": "统一输出目录（可选）"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "按文件类型过滤，如 [\"pptx\",\"docx\"]；省略表示不过滤",
                    },
                    "recursive": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "description": "最多处理 N 个文件"},
                    "options": {"type": "object", "additionalProperties": True},
                },
                "required": ["action", "target"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# 细粒度工具（每模块每动作一个）
# ---------------------------------------------------------------------------
def build_granular_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for module_name in sorted(all_handlers()):
        handler = all_handlers()[module_name]
        cap = handler.capabilities()
        for spec in cap["actions"]:
            properties: dict[str, Any] = {
                "input": {"type": "string", "description": "输入文件的绝对路径"},
                "output": {"type": "string", "description": "输出目录或文件路径（可选）"},
            }
            required = ["input"] if spec["needs_input"] else []
            for key, desc in (spec.get("params") or {}).items():
                properties[key] = {**infer_schema(key, desc), "description": desc}
            flags = []
            if spec["produces_files"]:
                flags.append("产出文件")
            if spec["modifies_input"]:
                flags.append("会修改输入文件并自动备份")
            tools.append(
                {
                    "name": f"file_{module_name}_{spec['action']}",
                    "description": (
                        f"[{cap['display_name']}] {spec['summary']}"
                        + (f"（{'; '.join(flags)}）" if flags else "")
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                    "_meta": {"module": module_name, "action": spec["action"]},
                }
            )
    return tools


def build_tools(mode: str = "generic") -> list[dict[str, Any]]:
    """按模式构建工具列表。``mode`` 取 ``generic`` / ``granular`` / ``both``。"""
    mode = (mode or "generic").strip().lower()
    if mode == "granular":
        return build_granular_tools()
    if mode == "both":
        return build_generic_tools() + build_granular_tools()
    return build_generic_tools()


# ---------------------------------------------------------------------------
# 导出为其它生态的工具格式
# ---------------------------------------------------------------------------
def to_openai_tools(specs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """转成 OpenAI / 兼容 Chat Completions 的 function-calling 结构。"""
    out: list[dict[str, Any]] = []
    for spec in specs:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
        )
    return out


def to_langchain_tools(specs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """转成 LangChain / LlamaIndex 常见的 ``{name, description, parameters}`` 结构。"""
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for spec in specs
    ]


def tool_summary() -> dict[str, Any]:
    """工具清单摘要，写入 MANIFEST.json 供 harness 静态注册。"""
    generic = build_generic_tools()
    granular = build_granular_tools()
    return {
        "generic_count": len(generic),
        "granular_count": len(granular),
        "generic_names": [t["name"] for t in generic],
        "granular_names": [t["name"] for t in granular],
        "modules": [m["module"] for m in list_modules()],
    }
