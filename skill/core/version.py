"""版本与元信息的唯一来源。

所有需要版本号的地方（``capabilities()``、``doctor``、打包产物、MCP ``serverInfo``）
都从这里读取，避免版本号在多处硬编码后互相漂移。

约定
----
* ``SLUG`` 必须是 ASCII：用于目录名、包名、MCP 工具名前缀、zip 文件名。
* ``NAME`` 是对外展示名，允许中文。
* ``VERSION`` 用语义化版本；``RELEASE`` 是对外发布标签。
"""

from __future__ import annotations

#: 对外展示名（中文）
NAME = "综合性办公文件处理技能"

#: ASCII 标识符：MCP 服务名 / 工具名前缀 / 发行包名
SLUG = "omni-office-file-toolkit"

#: 语义化版本
VERSION = "0.2.1"

#: 对外发布标签
RELEASE = "v0.2.1"

#: 发布日期
RELEASE_DATE = "2026-10-12"

#: 遵循的技能标准
SKILL_STANDARD = "agent-skills"

#: 最低 Python 版本
MIN_PYTHON = (3, 9)

#: MCP 协议版本（按新到旧排列，initialize 时与客户端协商）
MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: 一句话说明（给 harness 的工具描述用）
SUMMARY = "统一处理 PPTX、DOCX、XLSX、HTML、PDF、TXT 与图片（含 OCR 图像内容识别）的读取、分析、转换与修改"


def banner() -> str:
    return f"{NAME} {RELEASE} ({SLUG} {VERSION})"


def as_dict() -> dict:
    return {
        "name": NAME,
        "slug": SLUG,
        "version": VERSION,
        "release": RELEASE,
        "release_date": RELEASE_DATE,
        "skill_standard": SKILL_STANDARD,
        "min_python": ".".join(str(p) for p in MIN_PYTHON),
        "summary": SUMMARY,
    }
