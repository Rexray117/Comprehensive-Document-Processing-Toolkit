# -*- coding: utf-8 -*-
"""捆绑包自验证脚本。

用捆绑 venv 解释器运行捆绑 skill：
  1. doctor 环境自检（7 模块、依赖齐备性、OCR 后端）
  2. 构造真实 txt / PDF 样本，跑一轮 probe/read/analyze/transform/edit

用法（任选其一，两处都用捆绑包内路径）：
  <bundle>/venv/Scripts/python.exe <bundle>/verify_bundle.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
SKILL = BUNDLE / "skill"
sys.path.insert(0, str(SKILL))

# 让依赖解析指向捆绑 venv（脚本本就该用捆绑解释器运行，此处兜底）
ok = True

# ---------------------------------------------------------------- 1. doctor
print("=" * 60)
print("[1/2] doctor 环境自检（捆绑 skill + 捆绑 venv）")
print("=" * 60)
from core.runtime import doctor  # noqa: E402
from core.registry import list_modules  # noqa: E402

report = doctor()
deps = report.get("dependencies") or {}
print("健康状态:", "OK" if report.get("healthy") else "缺失必需依赖")
print("必需依赖缺失:", deps.get("required_missing") or "无")
print("已安装依赖:", f"{deps.get('installed_count')}/{deps.get('total_count')}")
print("OCR 后端:", ", ".join(report.get("ocr_backends", {}).get("available", []) or ["无"]))
print("已注册模块:", ", ".join(m["module"] for m in report.get("modules", [])))
print("handler 加载错误:", report.get("handler_load_errors") or "无")
print("解释器:", sys.executable)

# ---------------------------------------------------------------- 2. 真实处理
print()
print("=" * 60)
print("[2/2] txt / pdf 端到端处理验证")
print("=" * 60)
from core.dispatcher import run  # noqa: E402
from pypdf import PdfWriter  # noqa: E402

work = BUNDLE / "_verify_tmp"
work.mkdir(exist_ok=True)

# txt 样本
txt = work / "sample.txt"
txt.write_text(
    "无人机飞行安全气象系统技术报告\n"
    "风险指数等级模块负责风速、温度、湿度、气压四类气象因子的融合评估。\n"
    "ECharts 用于风险等级热力图渲染，Leaflet 负责风险分区展示。\n",
    encoding="utf-8",
)

# PDF 样本（pypdf 生成空白页 + 手写内容流）
pdf = work / "sample.pdf"
writer = PdfWriter()
page = writer.add_blank_page(width=612, height=792)
try:
    import io

    from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                               NameObject, NumberObject)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 14 Tf 72 720 Td (Hello bundled PDF selftest.) Tj ET")
    stream.update({NameObject("/Length"): NumberObject(len(stream.get_data()))})
    font = DictionaryObject()
    font.update({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    page[NameObject("/Contents")] = writer._add_object(stream)
except Exception:
    pass  # 内容流失败也不影响 probe/split 验证
with pdf.open("wb") as fh:
    writer.write(fh)

cases = [
    ("txt", "probe", {"input": txt}, {}),
    ("txt", "analyze", {"input": txt}, {"top_n": 5}),
    ("txt", "edit", {"input": txt}, {"op": "replace", "find": "低风险", "replace": "低危等级"}),
    ("txt", "transform", {"input": txt}, {"op": "to_json"}),
    ("pdf", "probe", {"input": pdf}, {}),
    ("pdf", "read", {"input": pdf}, {}),
    ("pdf", "transform", {"input": pdf}, {"op": "split", "split_pages": 1}),
]

for module, act, base, opts in cases:
    envelope = run({"module": module, "action": act, "input": str(base["input"]), "options": opts})
    status = "PASS" if envelope.get("ok") else "FAIL"
    if not envelope.get("ok"):
        ok = False
        print(f"[{status}] {module}.{act} :: {json.dumps(envelope.get('error'), ensure_ascii=False)[:120]}")
    else:
        print(f"[{status}] {module}.{act}")

import shutil  # noqa: E402

shutil.rmtree(work, ignore_errors=True)

print()
print("=== 捆绑包验证结果:", "ALL PASS" if ok else "存在失败项", "===")
sys.exit(0 if ok else 1)
