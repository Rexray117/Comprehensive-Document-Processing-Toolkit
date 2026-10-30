"""运行时诊断：解释器信息、依赖健康检查、外部工具探测。

``doctor`` 是排查「脚本跑不起来」的第一入口，输出一份可直接照抄的自检报告。
"""

from __future__ import annotations

import importlib.metadata as md
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from core.errors import pip_hint
from core.utils import PROJECT_ROOT, human_bytes, python_env

#: 依赖清单：import 名 → (pip 包名, 用途, 是否必需)
DEPENDENCIES: list[tuple[str, str, str, bool]] = [
    ("pptx", "python-pptx", "PPTX 读写", True),
    ("docx", "python-docx", "DOCX 读写", True),
    ("openpyxl", "openpyxl", "XLSX 读写与格式", True),
    ("pandas", "pandas", "表格数据分析", True),
    ("numpy", "numpy", "数值计算 / 图像质量指标", True),
    ("PIL", "Pillow", "图像处理", True),
    ("bs4", "beautifulsoup4", "HTML 解析", True),
    ("lxml", "lxml", "HTML/XML 高速解析器", True),
    ("markdownify", "markdownify", "HTML → Markdown", True),
    ("html2text", "html2text", "HTML → Markdown（备选）", False),
    ("pypdf", "pypdf", "PDF 解析（pdf 模块 probe/read/auto 的唯一实现）", True),
    ("filetype", "filetype", "魔数嗅探", True),
    ("charset_normalizer", "charset-normalizer", "编码自动探测", True),
    # --- 可选：OCR 链路（任选其一即可启用图像文本识别） ---
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime", "OCR（推荐：纯 pip、离线、中文准）", False),
    ("onnxruntime", "onnxruntime", "RapidOCR 推理后端", False),
    ("pytesseract", "pytesseract", "OCR（需另装 tesseract 可执行文件）", False),
    ("easyocr", "easyocr", "OCR（PyTorch，体积大）", False),
    # --- 可选：格式增强 ---
    ("mammoth", "mammoth", "DOCX → HTML 高保真转换", False),
    ("opencc", "opencc-python-reimplemented", "繁简转换", False),
    ("xlsxwriter", "XlsxWriter", "XLSX 高级写出（备选引擎）", False),
]

#: 外部可执行文件：命令 → 用途
EXTERNAL_TOOLS: list[tuple[str, str]] = [
    ("soffice", "LibreOffice：PPTX/DOCX/XLSX → PDF/PNG 渲染"),
    ("libreoffice", "LibreOffice（Linux 命令名）"),
    ("tesseract", "Tesseract OCR 引擎（pytesseract 依赖）"),
    ("pdftoppm", "poppler：PDF → PNG"),
    ("ffmpeg", "音视频处理（本包未直接使用，第三方扩展可用）"),
]


def dep_version(module_name: str) -> Optional[str]:
    """返回已安装版本号；未安装返回 None。"""
    if importlib.util.find_spec(module_name) is None:
        return None
    pip_name = next((p for m, p, _, _ in DEPENDENCIES if m == module_name), module_name)
    for candidate in (pip_name, module_name):
        try:
            return md.version(candidate)
        except Exception:
            continue
    return "unknown"


def probe_dependencies() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for module_name, pip_name, purpose, required in DEPENDENCIES:
        version = dep_version(module_name)
        rows.append(
            {
                "module": module_name,
                "package": pip_name,
                "purpose": purpose,
                "required": required,
                "installed": version is not None,
                "version": version,
                "install_cmd": None if version else pip_hint(pip_name),
            }
        )
    return rows


def which_tool(name: str) -> Optional[str]:
    found = shutil.which(name)
    return found


def probe_external() -> list[dict[str, Any]]:
    return [
        {"tool": name, "purpose": purpose, "path": which_tool(name), "available": bool(which_tool(name))}
        for name, purpose in EXTERNAL_TOOLS
    ]


def ocr_backends() -> dict[str, Any]:
    """探测可用的 OCR 后端，按推荐优先级排序。"""
    from ocr.engines import available_backends

    return available_backends()


def doctor(*, verbose: bool = False) -> dict[str, Any]:
    """完整自检报告。"""
    deps = probe_dependencies()
    missing_required = [d for d in deps if d["required"] and not d["installed"]]
    missing_optional = [d for d in deps if not d["required"] and not d["installed"]]
    tools = probe_external()

    try:
        backends = ocr_backends()
    except Exception as exc:  # OCR 模块本身出错不应让 doctor 崩掉
        backends = {"error": f"{type(exc).__name__}: {exc}", "available": []}

    from core.registry import list_modules, load_errors

    report: dict[str, Any] = {
        "python": python_env(),
        "project_root": str(PROJECT_ROOT),
        "cwd": os.getcwd(),
        "healthy": not missing_required,
        "dependencies": {
            "required_missing": [d["package"] for d in missing_required],
            "optional_missing": [d["package"] for d in missing_optional],
            "installed_count": sum(1 for d in deps if d["installed"]),
            "total_count": len(deps),
        },
        # 明细恒可用：doctor.py --verbose / core.cli doctor -v 都读这里，
        # 不再随 verbose 切换 dependencies 的类型（曾导致 TypeError: list indices...）
        "dependency_details": deps,
        "external_tools": tools if verbose else [t for t in tools if t["available"]],
        "ocr_backends": backends,
        "modules": [
            {"module": m["module"], "actions": m["actions"], "extensions": m["extensions"]}
            for m in list_modules()
        ],
        "handler_load_errors": load_errors(),
    }
    if missing_required:
        report["fix_hint"] = pip_hint(" ".join(d["package"] for d in missing_required))
    return report


def install_command(*packages: str) -> str:
    return pip_hint(" ".join(packages))
