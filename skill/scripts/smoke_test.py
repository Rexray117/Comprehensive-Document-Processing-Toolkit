"""端到端冒烟测试：自动造样例文件 → 跑通全部模块的四个动作 → 输出通过率报告。

这是改完代码后最应该先跑的一条命令 —— 它同时验证「依赖是否齐、模块是否能自动
发现、动作契约是否被破坏、产物是否真的落盘」。

用法::

    python scripts/smoke_test.py             # 全量（OCR 测试自动跳过）
    python scripts/smoke_test.py --ocr       # 含 OCR 测试（需已装 OCR 后端）
    python scripts/smoke_test.py --keep      # 保留样例文件与产物以便人工检查
    python scripts/smoke_test.py --json      # 机器可读报告

退出码：0 = 全部通过；1 = 存在失败用例。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.dispatcher import Request, run_request  # noqa: E402
from core.registry import list_modules  # noqa: E402

WORKDIR = _ROOT / "_smoke"


# ---------------------------------------------------------------------------
# 样例文件生成
# ---------------------------------------------------------------------------
def make_image(path: Path, *, kind: str = "screenshot") -> Path:
    from PIL import Image, ImageDraw

    if kind == "screenshot":
        img = Image.new("RGB", (1280, 720), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 1280, 64], fill=(24, 60, 110))
        draw.rectangle([0, 64, 220, 720], fill=(244, 247, 251))
        for i in range(6):
            draw.rectangle([250, 110 + i * 90, 900, 170 + i * 90], outline=(200, 210, 225), width=2)
            draw.rectangle([268, 130 + i * 90, 420, 150 + i * 90], fill=(70, 110, 170))
        draw.rectangle([250, 660, 1240, 700], fill=(230, 240, 250))
    else:
        import random

        img = Image.new("RGB", (900, 600))
        pixels = img.load()
        for y in range(600):
            for x in range(0, 900, 3):
                base = int(120 + 60 * (y / 600))
                pixels[x, y] = (base, base, min(255, base + 20))
                pixels[min(899, x + 1), y] = (base + random.randint(-25, 25),) * 3
                pixels[min(899, x + 2), y] = (base - random.randint(0, 30),) * 3
    img.save(path)
    return path


def make_pptx(path: Path, image: Optional[Path] = None) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "低空无人机飞行安全气象系统"
    slide.placeholders[1].text = "风险指数等级模块 · 技术验证"

    slide2 = prs.slides.add_slide(prs.slide_layouts[1])
    slide2.shapes.title.text = "风险等级阈值"
    frame = slide2.placeholders[1].text_frame
    frame.text = "低风险：风速 < 5 m/s"
    for text in (
        "中风险：5 ≤ 风速 < 10 m/s",
        "高风险：10 ≤ 风速 < 15 m/s",
        "极高风险：风速 ≥ 15 m/s 时必须停飞",
    ):
        frame.add_paragraph().text = text
    slide2.notes_slide.notes_text_frame.text = "阈值需结合温度与湿度做加权修正。"

    slide3 = prs.slides.add_slide(prs.slide_layouts[5])
    slide3.shapes.title.text = "季节性因子权重"
    table = slide3.shapes.add_table(3, 3, Inches(0.8), Inches(2.0), Inches(6.5), Inches(1.8)).table
    payload = [
        ["季节", "风速权重", "湿度权重"],
        ["夏季", "0.50", "0.30"],
        ["冬季", "0.60", "0.20"],
    ]
    for r, row in enumerate(payload):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    if image is not None:
        slide3.shapes.add_picture(str(image), Inches(8.2), Inches(4.6), width=Inches(3.6))
    prs.save(str(path))
    return path


def make_docx(path: Path, image: Optional[Path] = None) -> Path:
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading("南方电网无人机巡检全要素安全辅助决策系统", 0)
    doc.add_heading("一、系统概述", level=1)
    doc.add_paragraph(
        "本系统面向输配电线路无人机巡检场景，打通任务规划、气象研判、风险预警与报告生成四个环节。"
    )
    doc.add_heading("1.1 功能范围", level=2)
    doc.add_paragraph("风险指数等级模块负责把气象因子映射为可执行的飞行风险等级判断。")
    doc.add_heading("二、因子与阈值", level=1)
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    data = [["因子", "低风险", "高风险"], ["风速", "< 5 m/s", "≥ 10 m/s"], ["能见度", "> 5 km", "< 2 km"]]
    for r, row in enumerate(data):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    if image is not None:
        doc.add_paragraph("下图为本模块的可视化界面示意：")
        doc.add_picture(str(image), width=Inches(4.5))
    doc.save(str(path))
    return path


def make_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "巡检记录"
    ws.append(["日期", "区域", "设备编号", "飞行时长(min)", "缺陷数", "风险等级"])
    rows = [
        ["2026-03-01", "华东", "UAV-001", 42, 0, "低"],
        ["2026-03-02", "华东", "UAV-002", 38, 1, "中"],
        ["2026-03-05", "华南", "UAV-003", 55, 2, "高"],
        ["2026-03-07", "华南", "UAV-004", 47, 0, "低"],
        ["2026-03-11", "华北", "UAV-005", 61, 3, "极高"],
        ["2026-03-12", "华北", "UAV-005", 33, 0, "低"],
    ]
    for row in rows:
        ws.append(row)
    ws["H1"] = "缺陷合计"
    ws["H2"] = "=SUM(E2:E7)"
    ws["H3"] = "=AVERAGE(D2:D7)"
    ws.freeze_panes = "A2"
    ws.merge_cells("H5:I5")
    ws["H5"] = "备注：极高风险需人工复核"

    ws2 = wb.create_sheet("气象因子")
    ws2.append(["月份", "平均风速", "平均湿度", "平均温度"])
    for month, wind, humidity, temp in [
        (1, 6.2, 62, 4.1), (2, 5.8, 65, 6.3), (3, 7.1, 58, 12.5),
        (4, 8.4, 55, 18.2), (5, 9.0, 61, 23.6), (6, 7.6, 72, 27.4),
    ]:
        ws2.append([month, wind, humidity, temp])
    wb.save(str(path))
    return path


def make_html(path: Path, image: Optional[Path] = None, css: Optional[Path] = None) -> Path:
    img_tag = f'<img src="{image.name}" alt="系统界面示意" width="640">' if image else '<img src="missing.png">'
    css_tag = f'<link rel="stylesheet" href="{css.name}">' if css else ""
    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>低空无人机飞行安全气象系统 · 风险指数等级模块</title>
<meta name="description" content="风险指数等级模块的技术说明与阈值定义。">
<meta name="viewport" content="width=device-width, initial-scale=1">
{css_tag}
<style>
  body {{ font-family: "Microsoft YaHei", sans-serif; color: #1F2937; background: #FFFFFF; }}
  h1 {{ font-size: 32px; color: #123C6E; }}
  h2 {{ font-size: 24px; color: #2A5A8F; }}
  .badge {{ background: #E11D48; color: #FFFFFF; padding: 4px 8px; }}
</style>
</head>
<body>
<h1>风险指数等级模块</h1>
<h2>一、模块定位</h2>
<p>该模块把风速、温度、湿度、气压等气象因子映射为无人机可执行的飞行风险等级。</p>
<h3>1.1 阈值定义</h3>
<table>
  <caption>风险等级阈值表</caption>
  <tr><th>等级</th><th>风速</th><th>处置建议</th></tr>
  <tr><td>低</td><td>&lt; 5 m/s</td><td>正常执行</td></tr>
  <tr><td>中</td><td>5 - 10 m/s</td><td>谨慎执行</td></tr>
  <tr><td>高</td><td>10 - 15 m/s</td><td>限制执行</td></tr>
</table>
<ul>
  <li>等级配色：低=绿、中=黄、高=橙、极高=红</li>
  <li>数据刷新：每 10 分钟</li>
</ul>
{img_tag}
<p>参考：<a href="https://www.example.com/docs">技术文档</a> 与
<a href="/local/page.html">本地页面</a></p>
<script src="app.js"></script>
<script>console.log("inline script");</script>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")
    return path


def build_fixtures(workdir: Path) -> dict[str, Path]:
    src = workdir / "sources"
    src.mkdir(parents=True, exist_ok=True)
    out = workdir / "outputs"
    out.mkdir(parents=True, exist_ok=True)
    (src / "app.js").write_text('console.log("external script");\n', encoding="utf-8")
    (src / "theme.css").write_text(
        "h1 { color: #123C6E; }\n.badge { background: #E11D48; }\n", encoding="utf-8"
    )

    shot = make_image(src / "screen.png", kind="screenshot")
    photo = make_image(src / "photo.jpg", kind="photo")
    return {
        "pptx": make_pptx(src / "deck.pptx", shot),
        "docx": make_docx(src / "report.docx", shot),
        "xlsx": make_xlsx(src / "book.xlsx"),
        "html": make_html(src / "page.html", shot, src / "theme.css"),
        "image": shot,
        "photo": photo,
        "outputs": out,
    }


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
@dataclass
class Case:
    module: str
    action: str
    fixture: str
    options: dict[str, Any] = field(default_factory=dict)
    expect_artifacts: int = 0
    expect_keys: tuple[str, ...] = ()


def build_cases(fixtures: dict[str, Path], outputs: Path, *, ocr: bool) -> list[Case]:
    return [
        # ---------------- PPTX ----------------
        Case("pptx", "probe", "pptx", {"deep": True}, expect_keys=("slide_count",)),
        Case("pptx", "read", "pptx", {"include_notes": True, "include_runs": True},
             expect_keys=("slides", "core_properties")),
        Case("pptx", "analyze", "pptx", {"top_n": 15},
             expect_keys=("outline", "keywords", "fonts")),
        Case("pptx", "transform", "pptx", {"op": "to_markdown", "output": str(outputs / "pptx")},
             expect_artifacts=1),
        Case("pptx", "edit", "pptx", {"op": "replace_text", "find": "极高风险", "replace": "极高风险(停飞)",
                                      "in_place": False}),
        Case("pptx", "edit", "pptx", {"op": "set_title", "slide": 2, "text": "风险等级阈值（修订）",
                                      "in_place": False}),
        Case("pptx", "transform", "pptx", {"op": "export_images", "output": str(outputs / "pptx-media")},
             expect_artifacts=1),
        # ---------------- DOCX ----------------
        Case("docx", "probe", "docx", {"deep": True}, expect_keys=("paragraph_count",)),
        Case("docx", "read", "docx", {"include_tables": True, "include_formulas": True},
             expect_keys=("blocks", "sections")),
        Case("docx", "analyze", "docx", {"top_n": 15}, expect_keys=("outline", "text_stats", "issues")),
        Case("docx", "transform", "docx", {"op": "to_markdown", "output": str(outputs / "docx")},
             expect_artifacts=1),
        Case("docx", "transform", "docx", {"op": "export_images", "output": str(outputs / "docx-media")},
             expect_artifacts=1),
        Case("docx", "edit", "docx", {"op": "replace_text", "find": "风险指数等级模块",
                                      "replace": "风险指数等级模块 V2", "in_place": False}),
        Case("docx", "edit", "docx", {"op": "insert_table", "in_place": False,
                                      "rows": [["参数", "取值"], ["刷新周期", "10 分钟"]]}),
        Case("docx", "edit", "docx", {"op": "append_heading", "text": "三、附录", "level": 1, "in_place": False}),
        # ---------------- XLSX ----------------
        Case("xlsx", "probe", "xlsx", {"deep": True}, expect_keys=("sheet_names", "sheets")),
        Case("xlsx", "read", "xlsx", {"sheet": "all", "max_rows": 50}, expect_keys=("sheets",)),
        Case("xlsx", "analyze", "xlsx", {"sheet": "巡检记录", "by": "区域", "agg": "sum,count"},
             expect_keys=("columns", "issues")),
        Case("xlsx", "transform", "xlsx", {"op": "to_csv", "output": str(outputs / "xlsx")},
             expect_artifacts=1),
        Case("xlsx", "transform", "xlsx", {"op": "pivot", "index": "区域", "values": "缺陷数",
                                           "aggfunc": "sum", "output": str(outputs / "xlsx")},
             expect_artifacts=1),
        Case("xlsx", "edit", "xlsx", {"op": "set_cell", "sheet": "巡检记录", "cell": "H8", "value": "已复核",
                                      "in_place": False}),
        Case("xlsx", "edit", "xlsx", {"op": "set_formula", "sheet": "巡检记录", "cell": "H9",
                                      "formula": "=COUNTIF(F2:F7,\"高\")", "in_place": False}),
        Case("xlsx", "edit", "xlsx", {"op": "format_range", "range": "A1:F1", "bold": True,
                                      "fill": "123C6E", "color": "FFFFFF", "in_place": False}),
        Case("xlsx", "edit", "xlsx", {"op": "autofit", "in_place": False}),
        Case("xlsx", "edit", "xlsx", {"op": "add_chart", "sheet": "气象因子", "chart_type": "line",
                                      "title": "月度风速趋势", "in_place": False}),
        # ---------------- HTML ----------------
        Case("html", "probe", "html", {}, expect_keys=("counts", "title")),
        Case("html", "read", "html", {"main_only": False, "include_tables": True},
             expect_keys=("headings", "links", "tables")),
        Case("html", "analyze", "html", {"top_n": 15}, expect_keys=("issues", "heading_stats")),
        Case("html", "transform", "html", {"op": "to_markdown", "output": str(outputs / "html")},
             expect_artifacts=1),
        Case("html", "transform", "html", {"op": "inline_assets", "output": str(outputs / "html-selfcontained")},
             expect_artifacts=1),
        Case("html", "transform", "html", {"op": "extract_tables", "output": str(outputs / "html-tables")},
             expect_artifacts=1),
        Case("html", "edit", "html", {"op": "set_title", "text": "风险指数等级模块 · 修订版", "in_place": False}),
        Case("html", "edit", "html", {"op": "add_meta", "name": "author", "content": "技术组", "in_place": False}),
        Case("html", "edit", "html", {"op": "remove_elements", "selector": "script", "in_place": False}),
        # ---------------- IMAGE ----------------
        Case("image", "probe", "image", {"exif": True}, expect_keys=("width", "height", "ocr_backends")),
        Case("image", "read", "image", {"include_exif": True, "include_palette": True},
             expect_keys=("palette", "metrics")),
        Case("image", "analyze", "image", {"ocr": ocr, "vision_payload": ocr},
             expect_keys=("classification", "palette", "metrics")),
        Case("image", "transform", "image", {"op": "compress", "quality": 70, "output": str(outputs / "img")},
             expect_artifacts=1),
        Case("image", "transform", "image", {"op": "resize", "percent": 50, "output": str(outputs / "img")},
             expect_artifacts=1),
        Case("image", "transform", "image", {"op": "to_base64", "output": str(outputs / "img")},
             expect_artifacts=1),
        Case("image", "transform", "image", {"op": "thumbnail", "max_side": 256, "output": str(outputs / "img")},
             expect_artifacts=1),
        Case("image", "edit", "image", {"op": "redact", "box": "300,150,700,300", "in_place": False}),
        Case("image", "edit", "image", {"op": "blur_region", "box": "250,110,900,170", "radius": 10,
                                        "in_place": False}),
        Case("image", "edit", "image", {"op": "add_border", "width": 6, "color": "#123C6E", "in_place": False}),
        # ---------------- 自动路由与批量 ----------------
        Case("auto", "probe", "photo", {}, expect_keys=("format",)),
        Case("auto", "read", "xlsx", {"sheet": "气象因子"}, expect_keys=("sheets",)),
    ]


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def run_cases(cases: list[Case], fixtures: dict[str, Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        source = fixtures.get(case.fixture)
        options = dict(case.options)
        output = options.pop("output", None)
        request = Request(
            action=case.action,
            input=str(source) if source else None,
            module=case.module,
            output=output,
            options=options,
        )
        started = time.perf_counter()
        try:
            payload = run_request(request)
        except Exception as exc:  # 兜底：连 run_request 都炸了
            payload = {"ok": False, "error": {"code": "INTERNAL_ERROR",
                                              "message": f"{type(exc).__name__}: {exc}",
                                              "trace": traceback.format_exc(limit=3)}}
        elapsed = int((time.perf_counter() - started) * 1000)

        status = "PASS"
        detail = ""
        if not payload.get("ok"):
            status = "FAIL"
            err = payload.get("error") or {}
            detail = f"[{err.get('code')}] {str(err.get('message'))[:150]}"
        else:
            if len(payload.get("artifacts", [])) < case.expect_artifacts:
                status = "FAIL"
                detail = f"产物数量不足: 期望 ≥{case.expect_artifacts}，实际 {len(payload.get('artifacts', []))}"
            else:
                data = payload.get("data") or {}
                missing = [k for k in case.expect_keys if k not in data]
                if missing:
                    status = "FAIL"
                    detail = f"响应缺少字段: {missing}（实际字段: {list(data)[:12]}）"
                elif payload.get("warnings"):
                    status = "WARN"
                    detail = "; ".join(payload["warnings"])[:150]

        rows.append(
            {
                "module": payload.get("module") or case.module,
                "action": case.action,
                "fixture": case.fixture,
                "status": status,
                "elapsed_ms": payload.get("elapsed_ms", elapsed),
                "artifacts": len(payload.get("artifacts", [])),
                "detail": detail,
            }
        )
    return rows


def print_report(rows: list[dict[str, Any]]) -> None:
    passed = sum(1 for r in rows if r["status"] == "PASS")
    warned = sum(1 for r in rows if r["status"] == "WARN")
    failed = [r for r in rows if r["status"] == "FAIL"]

    print()
    print("=" * 92)
    print(f"{'模块':<8}{'动作':<11}{'样例':<8}{'状态':<7}{'耗时ms':>7}{'产物':>6}  说明")
    print("-" * 92)
    for row in rows:
        print(
            f"{row['module']:<8}{row['action']:<11}{row['fixture']:<8}{row['status']:<7}"
            f"{row['elapsed_ms']:>7}{row['artifacts']:>6}  {row['detail']}"
        )
    print("=" * 92)
    print(f"总计 {len(rows)} 个用例：通过 {passed}，警告 {warned}，失败 {len(failed)}")
    if failed:
        print()
        print("失败明细:")
        for row in failed:
            print(f"  ✗ {row['module']}.{row['action']} ({row['fixture']}): {row['detail']}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="omni-file-toolkit 端到端冒烟测试")
    parser.add_argument("--ocr", action="store_true", help="包含 OCR 相关用例")
    parser.add_argument("--keep", action="store_true", help="保留样例与产物目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--workdir", default=str(WORKDIR), help="工作目录（默认 ./_smoke）")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    if workdir.exists() and not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"工作目录: {workdir}")
    print("生成样例文件 ...")
    try:
        fixtures = build_fixtures(workdir)
    except Exception as exc:
        print(f"样例生成失败（通常意味着核心依赖未安装）: {type(exc).__name__}: {exc}")
        print(f"修复: python scripts/doctor.py  然后按提示安装依赖")
        return 1
    for key, value in fixtures.items():
        if key != "outputs":
            print(f"  - {key:<7} {value.name}")

    modules = [m["module"] for m in list_modules()]
    print(f"已发现模块: {', '.join(modules) if modules else '(无！handlers 目录可能缺失)'}")

    outputs = fixtures["outputs"]
    cases = build_cases(fixtures, outputs, ocr=args.ocr)
    print(f"执行 {len(cases)} 个用例 ...")
    rows = run_cases(cases, fixtures)

    if args.json:
        print(json.dumps({"workdir": str(workdir), "results": rows,
                          "passed": sum(1 for r in rows if r["status"] == "PASS"),
                          "failed": sum(1 for r in rows if r["status"] == "FAIL")},
                         ensure_ascii=False, indent=2))
    else:
        print_report(rows)

    if not args.keep:
        print(f"（样例已保留在 {workdir}，可用 --keep 明确保留 / 手动删除）")
    return 0 if all(r["status"] != "FAIL" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
