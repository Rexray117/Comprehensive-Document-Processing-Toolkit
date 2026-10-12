"""PPTX 演示文稿处理模块（python-pptx + OOXML 直读）。

能力概览
--------
* ``probe``     ZIP 层快速探测（不依赖 python-pptx，零依赖也能跑）
* ``read``      结构化读取：每页标题 / 形状 / 表格 / 图表 / 备注 / 图片
* ``analyze``   内容识别：大纲、字数、关键词、字体、配色、空页、图片 OCR
* ``transform`` 导出：Markdown / 纯文本 / 大纲 JSON / 图片 / 媒体 / PDF 渲染
* ``edit``      修改：跨 run 查找替换（保格式）、改标题、增删页、复制页、加文本框

独立运行
--------
    python handlers/pptx_handler.py probe  deck.pptx
    python handlers/pptx_handler.py read   deck.pptx -p include_notes=false
    python handlers/pptx_handler.py edit   deck.pptx -p op=replace_text -p find=旧 -p replace=新
    python handlers/pptx_handler.py cap                 # 查看本模块能力契约
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # 支持 `python handlers/pptx_handler.py ...` 直接运行
    sys.path.insert(0, str(_ROOT))

from core.base import BaseHandler, ActionContext, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ActionError, InputError, ParamError, require  # noqa: E402
from core.textops import build_pattern, replace_in_text_frame  # noqa: E402
from core.utils import human_bytes, slugify, text_stats, to_json, top_keywords  # noqa: E402

MODULE = "pptx"

# python-pptx 的 MSO_SHAPE_TYPE 名称映射（避免强依赖枚举导入失败）
_SHAPE_TYPE_NAMES = {
    1: "AUTO_SHAPE", 2: "CALLOUT", 3: "CHART", 4: "COMMENT", 5: "FREEFORM",
    6: "GROUP", 7: "EMBEDDED_OLE_OBJECT", 8: "FORM_CONTROL", 9: "LINE",
    10: "LINKED_OLE_OBJECT", 11: "MEDIA", 12: "OLE_CONTROL_OBJECT",
    13: "PICTURE", 14: "PLACEHOLDER", 15: "TEXT_BOX", 16: "SCRIPT_ANCHOR",
    17: "TABLE", 18: "CANVAS", 19: "DIAGRAM", 20: "INK", 21: "SIGNOFF",
    22: "SMART_ART", 23: "WEB_VIDEO", 24: "CONTENT_APP",
}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _emu_inches(value: Any) -> Optional[float]:
    try:
        return round(int(value) / 914400, 3)
    except (TypeError, ValueError):
        return None


def _shape_type_name(shape: Any) -> str:
    try:
        raw = getattr(shape, "shape_type", None)
        if raw is None:
            return "UNKNOWN"
        code = int(raw)
    except (TypeError, ValueError):
        return str(getattr(shape, "shape_type", "UNKNOWN"))
    return _SHAPE_TYPE_NAMES.get(code, str(code))


def _iter_text_frames(shape: Any) -> Iterator[Any]:
    """递归取出形状内所有文本框（含组合形状、表格单元格）。"""
    try:
        if getattr(shape, "has_text_frame", False):
            yield shape.text_frame
    except Exception:
        pass
    if _shape_type_name(shape) == "GROUP":
        try:
            for child in shape.shapes:
                yield from _iter_text_frames(child)
        except Exception:
            pass
    try:
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    yield cell.text_frame
    except Exception:
        pass


def _table_rows(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        rows.append([cell.text for cell in row.cells])
    return rows


def _chart_info(chart: Any) -> dict[str, Any]:
    info: dict[str, Any] = {"chart_type": str(getattr(chart, "chart_type", "unknown"))}
    try:
        info["has_title"] = bool(chart.has_title)
        if chart.has_title:
            info["title"] = chart.chart_title.text_frame.text
    except Exception:
        pass
    series: list[dict[str, Any]] = []
    try:
        for s in chart.series:
            values: list[Any] = []
            try:
                values = [v for v in s.values]
            except Exception:
                pass
            series.append({"name": str(getattr(s, "name", "")), "values": values})
    except Exception:
        pass
    info["series"] = series
    try:
        cats = list(chart.plots[0].categories)
        info["categories"] = [str(c) for c in cats]
    except Exception:
        pass
    return info


def _picture_info(shape: Any) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        image = shape.image
        info = {
            "filename": getattr(image, "filename", None),
            "ext": getattr(image, "ext", None),
            "bytes": len(image.blob),
            "sha1": getattr(image, "sha1", None),
            "dpi": getattr(image, "dpi", None),
            "size_px": list(getattr(image, "size", ()) or []),
        }
    except Exception as exc:
        info["error"] = f"读取图片失败: {exc}"
    return info


def _shape_to_dict(shape: Any, *, include_runs: bool = False, depth: int = 0) -> dict[str, Any]:
    kind = _shape_type_name(shape)
    data: dict[str, Any] = {
        "name": getattr(shape, "name", ""),
        "type": kind,
        "position": {
            "left_in": _emu_inches(getattr(shape, "left", None)),
            "top_in": _emu_inches(getattr(shape, "top", None)),
            "width_in": _emu_inches(getattr(shape, "width", None)),
            "height_in": _emu_inches(getattr(shape, "height", None)),
        },
    }
    try:
        data["is_placeholder"] = bool(shape.is_placeholder)
        if shape.is_placeholder:
            data["placeholder_idx"] = shape.placeholder_format.idx
            data["placeholder_type"] = str(shape.placeholder_format.type)
    except Exception:
        data["is_placeholder"] = False

    if kind == "GROUP" and depth < 3:
        try:
            data["children"] = [
                _shape_to_dict(c, include_runs=include_runs, depth=depth + 1) for c in shape.shapes
            ]
        except Exception:
            data["children"] = []
        return data

    try:
        if shape.has_text_frame:
            tf = shape.text_frame
            data["text"] = tf.text
            paragraphs: list[dict[str, Any]] = []
            for para in tf.paragraphs:
                entry: dict[str, Any] = {
                    "level": int(getattr(para, "level", 0) or 0),
                    "text": "".join(r.text for r in para.runs),
                    "alignment": str(getattr(para, "alignment", None)),
                }
                if include_runs:
                    entry["runs"] = [
                        {
                            "text": r.text,
                            "bold": r.font.bold,
                            "italic": r.font.italic,
                            "size_pt": round(r.font.size.pt, 1) if r.font.size else None,
                            "font": r.font.name,
                            "color": (
                                str(r.font.color.rgb) if r.font.color and r.font.color.type is not None else None
                            ),
                        }
                        for r in para.runs
                    ]
                paragraphs.append(entry)
            data["paragraphs"] = [p for p in paragraphs if p["text"].strip()]
            if data["paragraphs"]:
                first = data["paragraphs"][0]["runs"][0] if include_runs and data["paragraphs"][0].get("runs") else {}
                if first.get("font"):
                    data["font"] = first["font"]
                if first.get("size_pt"):
                    data["font_size_pt"] = first["size_pt"]
    except Exception as exc:
        data["text_error"] = str(exc)

    try:
        if shape.has_table:
            rows = _table_rows(shape.table)
            data["table"] = {
                "rows": len(rows),
                "cols": len(rows[0]) if rows else 0,
                "data": rows,
            }
    except Exception:
        pass

    try:
        if shape.has_chart:
            data["chart"] = _chart_info(shape.chart)
    except Exception:
        pass

    if kind == "PICTURE":
        data["image"] = _picture_info(shape)

    try:
        if getattr(shape, "has_text_frame", False) and shape.text_frame.is_placeholder:
            pass
    except Exception:
        pass
    return data


def _slide_title(slide: Any) -> Optional[str]:
    try:
        if slide.shapes.title is not None:
            return slide.shapes.title.text
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 跨 run 文本替换（保留原格式）
# ---------------------------------------------------------------------------
def _rewrite_runs(runs: list[Any], matches: list[tuple[int, int, str]]) -> int:
    """把匹配区间替换结果按 run 边界重新分配，最大程度保留原格式。

    策略：每个字符归属它所在的 run；替换文本写入「匹配起点」所在的 run，
    被吞掉的字符从各自的 run 中移除（其余 run 的格式因此得以保留）。
    """
    texts = [r.text for r in runs]
    joined = "".join(texts)
    if not matches:
        return 0

    owner: list[int] = []
    for idx, t in enumerate(texts):
        owner.extend([idx] * len(t))

    starts = {s: (e, repl) for s, e, repl in matches if 0 <= s < max(1, len(joined) + 1)}
    chunks: list[list[str]] = [[] for _ in texts]
    i, n = 0, len(joined)
    while i < n:
        if i in starts:
            end, repl = starts[i]
            chunks[owner[i]].append(repl)
            i = max(end, i + 1)
        else:
            chunks[owner[i]].append(joined[i])
            i += 1

    for idx, run in enumerate(runs):
        new_text = "".join(chunks[idx])
        if new_text != texts[idx]:
            run.text = new_text
    return len(matches)


def _replace_in_text_frame(tf: Any, pattern: re.Pattern[str], repl: str) -> int:
    total = 0
    for para in tf.paragraphs:
        runs = list(para.runs)
        if not runs:
            continue
        joined = "".join(r.text for r in runs)
        matches: list[tuple[int, int, str]] = []
        for m in pattern.finditer(joined):
            if m.end() == m.start():
                continue
            replacement = m.expand(repl) if "\\" in repl else repl
            matches.append((m.start(), m.end(), replacement))
        if matches:
            total += _rewrite_runs(runs, matches)
    return total


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class PptxHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "PPTX 演示文稿"
    description = (
        "处理 .pptx 演示文稿：读取幻灯片文本/表格/图表/备注/图片、统计大纲与关键词、"
        "导出 Markdown 或图片、跨 run 查找替换保格式、增删与复制幻灯片。"
    )
    extensions = (".pptx", ".ppsx", ".potx")
    kinds = ("pptx",)
    optional_deps = {"pptx": "python-pptx", "fitz": "pymupdf"}

    # ======================================================== probe
    @action(
        "probe",
        summary="ZIP 层快速探测：页数、媒体、备注、尺寸，不依赖 python-pptx",
        params={"deep": "true 时额外用 python-pptx 解析版式与占位符（默认 false）"},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        info: dict[str, Any] = {"file": Path(info_name := path.name).name, "size_human": human_bytes(path.stat().st_size)}
        if not zipfile.is_zipfile(path):
            raise InputError("不是合法的 OOXML（ZIP）文件，可能已损坏", details={"path": str(path)})

        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            slides = sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
            media = [n for n in names if n.startswith("ppt/media/")]
            notes = [n for n in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)]
            charts = [n for n in names if n.startswith("ppt/charts/chart")]
            embeds = [n for n in names if n.startswith("ppt/embeddings/")]
            info.update(
                {
                    "slide_count": len(slides),
                    "layout_count": len([n for n in names if re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", n)]),
                    "master_count": len([n for n in names if re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", n)]),
                    "notes_slide_count": len(notes),
                    "chart_count": len(charts),
                    "embedded_object_count": len(embeds),
                    "media_count": len(media),
                    "media_bytes": sum(zf.getinfo(n).file_size for n in media),
                    "has_theme": any(n.startswith("ppt/theme/") for n in names),
                    "has_embedded_fonts": any(n.startswith("ppt/fonts/") for n in names),
                    "has_macros": any(n.endswith(".bin") and "vbaProject" in n for n in names),
                    "part_count": len(names),
                }
            )
            info["media_by_ext"] = dict(
                Counter(Path(n).suffix.lower() for n in media).most_common()
            )

        if ctx.flag("deep", False):
            Presentation = require("pptx", "python-pptx").Presentation
            prs = Presentation(str(path))
            info["slide_size_in"] = {
                "width": _emu_inches(prs.slide_width),
                "height": _emu_inches(prs.slide_height),
            }
            info["layouts"] = [lay.name for lay in prs.slide_layouts]
            info["core_properties"] = self._core_properties(prs)
        return info

    # ======================================================== read
    @action(
        "read",
        summary="结构化读取全部幻灯片（标题/形状/表格/图表/备注/图片）",
        params={
            "include_notes": "是否读备注页（默认 true）",
            "include_tables": "是否读表格内容（默认 true）",
            "include_charts": "是否读图表数据（默认 true）",
            "include_empty": "是否保留空文本框（默认 false）",
            "slide_range": "指定页码，如 '1-5' 或 '2,4,7'（默认全部）",
            "include_runs": "是否细化到 run 级字体信息（默认 false）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        Presentation = require("pptx", "python-pptx").Presentation
        prs = Presentation(str(ctx.input_path()))
        all_slides = list(prs.slides)
        wanted = self._parse_range(ctx.text("slide_range", ""), len(all_slides))

        include_notes = ctx.flag("include_notes", True)
        include_empty = ctx.flag("include_empty", False)
        include_runs = ctx.flag("include_runs", False)

        slides: list[dict[str, Any]] = []
        for idx, slide in enumerate(all_slides, start=1):
            if idx not in wanted:
                continue
            entry: dict[str, Any] = {
                "index": idx,
                "layout": self._layout_name(slide),
                "title": _slide_title(slide),
                "shapes": [],
            }
            for shape in slide.shapes:
                shape_data = _shape_to_dict(shape, include_runs=include_runs)
                if not include_empty and not self._shape_has_content(shape_data):
                    continue
                entry["shapes"].append(shape_data)
            if include_notes and self._has_notes(slide):
                entry["notes"] = self._notes_text(slide)
            slides.append(entry)

        return {
            "slide_count": len(all_slides),
            "returned": len(slides),
            "slide_size_in": {
                "width": _emu_inches(prs.slide_width),
                "height": _emu_inches(prs.slide_height),
            },
            "core_properties": self._core_properties(prs),
            "slides": slides,
        }

    # ======================================================== analyze
    @action(
        "analyze",
        summary="内容识别：大纲、字数、关键词、字体/配色分布、空页检测、图片 OCR",
        params={
            "top_n": "关键词数量（默认 20）",
            "ocr": "是否对幻灯片内图片做 OCR（默认 false）",
            "ocr_langs": "OCR 语言，默认 chi_sim+eng",
            "ocr_backend": "auto / rapidocr / tesseract / easyocr",
            "min_chars_per_slide": "低于该字数视为稀疏页（默认 10）",
        },
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        Presentation = require("pptx", "python-pptx").Presentation
        prs = Presentation(str(ctx.input_path()))
        top_n = ctx.bounded_int("top_n", 20, low=1, high=200)
        min_chars = ctx.bounded_int("min_chars_per_slide", 10, low=0, high=100000)

        texts: list[str] = []
        outline: list[dict[str, Any]] = []
        fonts: Counter[str] = Counter()
        font_sizes: Counter[float] = Counter()
        colors: Counter[str] = Counter()
        empty_slides: list[int] = []
        sparse_slides: list[int] = []
        image_total = 0
        image_bytes = 0
        chart_slides: list[int] = []
        table_count = 0
        picture_paths: list[tuple[int, Path]] = []
        notes_chars = 0

        for idx, slide in enumerate(prs.slides, start=1):
            slide_chars = 0
            title = _slide_title(slide)
            if title:
                outline.append({"slide": idx, "level": 0, "text": title.strip()})
                texts.append(title)
                slide_chars += len(title)

            for shape in slide.shapes:
                for tf in _iter_text_frames(shape):
                    for para in tf.paragraphs:
                        for run in para.runs:
                            texts.append(run.text)
                            slide_chars += len(run.text)
                            if run.font.name:
                                fonts[str(run.font.name)] += 1
                            if run.font.size:
                                font_sizes[round(run.font.size.pt, 1)] += 1
                            try:
                                if run.font.color and run.font.color.type is not None:
                                    colors[str(run.font.color.rgb)] += 1
                            except Exception:
                                pass
                        lvl = int(getattr(para, "level", 0) or 0)
                        ptxt = "".join(r.text for r in para.runs).strip()
                        if ptxt and lvl > 0:
                            outline.append({"slide": idx, "level": lvl, "text": ptxt})
                try:
                    if shape.has_table:
                        table_count += 1
                except Exception:
                    pass
                try:
                    if shape.has_chart:
                        chart_slides.append(idx)
                except Exception:
                    pass
                if _shape_type_name(shape) == "PICTURE":
                    image_total += 1
                    blob_len = 0
                    try:
                        blob_len = len(shape.image.blob)
                        image_bytes += blob_len
                    except Exception:
                        pass
                    picture_paths.append((idx, Path("")))
            if self._has_notes(slide):
                note = self._notes_text(slide) or ""
                notes_chars += len(note)
                texts.append(note)

            if slide_chars == 0 and not (title or "").strip():
                empty_slides.append(idx)
            elif slide_chars < min_chars:
                sparse_slides.append(idx)

        full_text = "\n".join(texts)
        result: dict[str, Any] = {
            "slide_count": len(prs.slides),
            "outline": outline,
            "text_stats": text_stats(full_text),
            "notes_chars": notes_chars,
            "keywords": top_keywords(full_text, top_n=top_n),
            "fonts": dict(fonts.most_common(20)),
            "font_sizes": {str(k): v for k, v in font_sizes.most_common(20)},
            "colors": dict(colors.most_common(20)),
            "structure": {
                "tables": table_count,
                "chart_slides": sorted(set(chart_slides)),
                "pictures": image_total,
                "picture_bytes": image_bytes,
            },
            "empty_slides": empty_slides,
            "sparse_slides": sparse_slides,
            "issues": [],
        }
        if empty_slides:
            result["issues"].append(f"存在 {len(empty_slides)} 页无任何文本: {empty_slides}")
        if image_total and not chart_slides and not table_count:
            result["issues"].append("内容几乎全靠图片承载，建议抽取图片文字以提升可检索性")
        if fonts and len(fonts) > 6:
            result["issues"].append(f"字体种类偏多（{len(fonts)} 种），可能影响视觉统一性")
        if sum(font_sizes.values()) and font_sizes:
            tiny = sum(c for s, c in font_sizes.items() if s < 12)
            if tiny > sum(font_sizes.values()) * 0.4:
                result["issues"].append("小于 12pt 的文字占比过高，投影场景可读性差")

        if ctx.flag("ocr", False):
            result["ocr"] = self._ocr_all(ctx, prs)

        return result

    # ======================================================== transform
    @action(
        "transform",
        summary="导出为 Markdown / 纯文本 / 大纲 JSON / 图片 / 媒体 / PDF",
        params={
            "op": "to_markdown | to_text | to_outline_json | export_images | export_media | render",
            "render_format": "render 时的目标格式：pdf（默认）或 png",
            "layout_placeholder": "是否导出版式占位提示（to_markdown 时，默认 false）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        op = (ctx.text("op", "to_markdown") or "to_markdown").strip().lower()
        stem = slugify(ctx.input_path().stem)

        if op in ("to_markdown", "markdown", "md", "to_text", "text", "to_outline_json", "outline_json"):
            data = self.read(ctx)
            if op in ("to_markdown", "markdown", "md"):
                body = self._to_markdown(data, ctx.flag("layout_placeholder", False))
                target = ctx.out_file(f"{stem}.md")
                target.write_text(body, encoding="utf-8")
                ctx.emit(target, "markdown")
                return {"op": op, "slides": len(data["slides"]), "chars": len(body)}
            if op in ("to_text", "text"):
                lines = []
                for s in data["slides"]:
                    lines.append(f"=== Slide {s['index']}" + (f": {s['title']}" if s.get("title") else " ==="))
                    for shape in s["shapes"]:
                        if shape.get("text"):
                            lines.append(shape["text"])
                        for row in (shape.get("table", {}) or {}).get("data", []):
                            lines.append(" | ".join(row))
                    if s.get("notes"):
                        lines.append(f"[备注] {s['notes']}")
                    lines.append("")
                body = "\n".join(lines)
                target = ctx.out_file(f"{stem}.txt")
                target.write_text(body, encoding="utf-8")
                ctx.emit(target, "text")
                return {"op": op, "chars": len(body)}
            target = ctx.out_file(f"{stem}.outline.json")
            write = json.loads(to_json(data))
            target.write_text(to_json(write), encoding="utf-8")
            ctx.emit(target, "json")
            return {"op": op, "slides": len(data["slides"])}

        if op in ("export_images", "export_media", "images", "media"):
            return self._export_media(ctx, stem, only_images=(op in ("export_images", "images")))

        if op in ("render", "pdf", "png"):
            return self._render(ctx, stem, ctx.text("render_format", "pdf" if op == "render" else op))

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: to_markdown / to_text / to_outline_json / export_images / export_media / render",
        )

    # ======================================================== edit
    @action(
        "edit",
        summary="修改演示文稿：查找替换（保格式）/改标题/增删复制页/加文本框/写属性",
        params={
            "op": "replace_text | set_title | set_notes | add_slide | delete_slide | "
                  "duplicate_slide | append_bullets | add_textbox | move_slide | set_core_properties",
            "find": "replace_text：查找内容",
            "replace": "replace_text：替换内容",
            "regex": "replace_text：是否按正则（默认 false）",
            "slide": "目标页码（1 基）",
            "text": "set_title / set_notes：新文本",
            "title": "add_slide：标题",
            "bullets": "add_slide / append_bullets：要点列表",
            "layout_index": "add_slide：版式索引（默认 1）",
            "in_place": "是否就地保存（默认 true；false 则另存为 <名称>_edited.pptx）",
        },
        modifies_input=True,
        tags=("edit",),
    )
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        Presentation = require("pptx", "python-pptx").Presentation
        path = ctx.input_path()
        prs = Presentation(str(path))
        op = (ctx.text("op", "replace_text") or "replace_text").strip().lower()

        handler_fn = {
            "replace_text": self._op_replace_text,
            "set_title": self._op_set_title,
            "set_notes": self._op_set_notes,
            "add_slide": self._op_add_slide,
            "delete_slide": self._op_delete_slide,
            "duplicate_slide": self._op_duplicate_slide,
            "append_bullets": self._op_append_bullets,
            "add_textbox": self._op_add_textbox,
            "move_slide": self._op_move_slide,
            "set_core_properties": self._op_set_core_properties,
        }.get(op)
        if handler_fn is None:
            raise ParamError(
                f"不支持的 op: {op}",
                hint=f"可选: {', '.join(sorted(['replace_text','set_title','set_notes','add_slide','delete_slide','duplicate_slide','append_bullets','add_textbox','move_slide','set_core_properties']))}",
            )

        changed = handler_fn(ctx, prs) or 0
        target = self._save_target(ctx, path)
        prs.save(str(target))
        if target != path:
            ctx.emit(target, "pptx")
        return {
            "op": op,
            "changed": changed,
            "saved_to": str(target),
            "in_place": target == path,
            "backup": ctx.get("__backup__"),
            "slide_count": len(prs.slides._sldIdLst),  # noqa: SLF001
        }

    # ------------------------------------------------------------------ edit 子操作
    def _op_replace_text(self, ctx: ActionContext, prs: Any) -> int:
        find = ctx.require("find")
        repl = ctx.text("replace", "")
        pattern = build_pattern(str(find), regex=ctx.flag("regex", False),
                                ignore_case=ctx.flag("ignore_case", False))
        scope = ctx.text("scope", "all").strip().lower()

        targets = self._slide_targets(prs, scope)
        notes_too = ctx.flag("notes", True) and scope in ("all", "notes")
        total = 0
        for slide in targets:
            for shape in slide.shapes:
                for tf in _iter_text_frames(shape):
                    total += replace_in_text_frame(tf, pattern, repl)
            if notes_too and self._has_notes(slide):
                total += replace_in_text_frame(self._notes_frame(slide), pattern, repl)
        return total

    def _op_set_title(self, ctx: ActionContext, prs: Any) -> int:
        slide = self._slide_at(prs, ctx.integer("slide", 1) or 1)
        text = ctx.require("text")
        try:
            if slide.shapes.title is not None:
                slide.shapes.title.text = str(text)
                return 1
        except Exception:
            pass
        # 没有标题占位符时退化：改第一个文本框
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                shape.text_frame.text = str(text)
                return 1
        raise ActionError(f"第 {ctx.integer('slide', 1)} 页没有可写入的标题或文本框")

    def _op_set_notes(self, ctx: ActionContext, prs: Any) -> int:
        slide = self._slide_at(prs, ctx.integer("slide", 1) or 1)
        text = str(ctx.require("text"))
        frame = self._notes_frame(slide, create=True)
        frame.text = text
        return 1

    def _op_add_slide(self, ctx: ActionContext, prs: Any) -> int:
        layouts = prs.slide_layouts
        idx = ctx.integer("layout_index", 1) or 0
        if not (0 <= idx < len(layouts)):
            raise ParamError(f"layout_index 越界: {idx}（有效范围 0..{len(layouts) - 1}）")
        slide = prs.slides.add_slide(layouts[idx])
        title = ctx.text("title", "")
        if title and slide.shapes.title is not None:
            slide.shapes.title.text = title
        bullets = ctx.list_of("bullets")
        if bullets:
            self._fill_body_placeholder(slide, bullets)
        position = ctx.text("position", "end")
        if position != "end":
            try:
                self._move_slide_el(prs, len(prs.slides) - 1, int(position) - 1)
            except Exception:
                ctx.warn(f"插入到位置 {position} 失败，已追加到末尾")
        return 1

    def _op_delete_slide(self, ctx: ActionContext, prs: Any) -> int:
        indices = [int(i) - 1 for i in ctx.list_of("slide")] or [int(ctx.require("slide")) - 1]
        sld_ids = prs.slides._sldIdLst  # noqa: SLF001
        elements = list(sld_ids)
        for i in sorted(set(indices), reverse=True):
            if not (0 <= i < len(elements)):
                raise ParamError(f"页码越界: {i + 1}（共 {len(elements)} 页）")
            el = elements[i]
            r_id = el.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            try:
                if r_id:
                    prs.part.drop_rel(r_id)
            except Exception:
                pass
            sld_ids.remove(el)
        return len(indices)

    def _op_duplicate_slide(self, ctx: ActionContext, prs: Any) -> int:
        """复制一页（含图片关系重绑定）。注意：组合形状内的图片可能丢失。"""
        src_index = (ctx.integer("slide", len(prs.slides)) or len(prs.slides)) - 1
        src = self._slide_at(prs, src_index + 1)
        new_slide = prs.slides.add_slide(src.slide_layout)

        # 清空新页里版式自动生成的占位符，避免与复制内容重复
        for shape in list(new_slide.shapes):
            shape._element.getparent().remove(shape._element)  # noqa: SLF001

        from pptx.oxml.ns import qn  # noqa: PLC0415

        for shape in src.shapes:
            new_el = copy.deepcopy(shape._element)  # noqa: SLF001
            new_slide.shapes._spTree.insert_element_before(new_el, "p:extLst")  # noqa: SLF001
            if _shape_type_name(shape) != "PICTURE":
                continue
            try:
                blip = new_el.find(".//" + qn("a:blip"))
                if blip is None:
                    continue
                old_rid = blip.get(qn("r:embed"))
                if not old_rid:
                    continue
                image_part = src.part.related_part(old_rid)
                new_rid = new_slide.part.relate_to(image_part, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
                blip.set(qn("r:embed"), new_rid)
            except Exception as exc:
                ctx.warn(f"复制图片关系失败（该图可能显示为空）: {exc}")
        return 1

    def _op_append_bullets(self, ctx: ActionContext, prs: Any) -> int:
        slide = self._slide_at(prs, ctx.integer("slide", 1) or 1)
        bullets = ctx.list_of("bullets")
        if not bullets:
            raise ParamError("append_bullets 需要 bullets 参数，如 -p bullets=甲,乙,丙")
        frame = self._body_frame(slide)
        added = 0
        for text in bullets:
            para = frame.add_paragraph()
            para.text = str(text)
            added += 1
        return added

    def _op_add_textbox(self, ctx: ActionContext, prs: Any) -> int:
        from pptx.util import Inches  # noqa: PLC0415

        slide = self._slide_at(prs, ctx.integer("slide", 1) or 1)
        text = str(ctx.require("text"))
        box = slide.shapes.add_textbox(
            Inches(ctx.number("left_in", 1.0) or 1.0),
            Inches(ctx.number("top_in", 1.0) or 1.0),
            Inches(ctx.number("width_in", 6.0) or 6.0),
            Inches(ctx.number("height_in", 1.5) or 1.5),
        )
        box.text_frame.text = text
        return 1

    def _op_move_slide(self, ctx: ActionContext, prs: Any) -> int:
        src = int(ctx.require("slide")) - 1
        dst = int(ctx.require("to")) - 1
        self._move_slide_el(prs, src, dst)
        return 1

    def _op_set_core_properties(self, ctx: ActionContext, prs: Any) -> int:
        props = prs.core_properties
        mapping = {
            "title": "title", "author": "author", "subject": "subject",
            "keywords": "keywords", "comments": "comments", "category": "category",
            "last_modified_by": "last_modified_by",
        }
        changed = 0
        for key, attr in mapping.items():
            value = ctx.get(key)
            if value is not None:
                setattr(props, attr, str(value))
                changed += 1
        return changed

    # ------------------------------------------------------------------ 内部辅助
    @staticmethod
    def _slide_at(prs: Any, index: int) -> Any:
        slides = list(prs.slides)
        if not (1 <= index <= len(slides)):
            raise ParamError(f"页码越界: {index}（共 {len(slides)} 页）")
        return slides[index - 1]

    def _slide_targets(self, prs: Any, scope: str) -> list[Any]:
        slides = list(prs.slides)
        if scope in ("all", "notes", ""):
            return slides
        if scope.startswith("slide:"):
            nums = self._parse_range(scope.split(":", 1)[1], len(slides))
            return [s for i, s in enumerate(slides, start=1) if i in nums]
        return slides

    @staticmethod
    def _move_slide_el(prs: Any, src: int, dst: int) -> None:
        sld_ids = prs.slides._sldIdLst  # noqa: SLF001
        elements = list(sld_ids)
        if not (0 <= src < len(elements)) or not (0 <= dst < len(elements)):
            raise ParamError(f"移动页码越界: src={src + 1}, dst={dst + 1}（共 {len(elements)} 页）")
        el = elements[src]
        sld_ids.remove(el)
        sld_ids.insert(dst, el)

    @staticmethod
    def _parse_range(spec: str, total: int) -> set[int]:
        if not spec or not spec.strip():
            return set(range(1, total + 1))
        out: set[int] = set()
        for chunk in spec.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "-" in chunk:
                start, _, end = chunk.partition("-")
                try:
                    lo, hi = int(start), int(end)
                except ValueError:
                    continue
                out.update(range(max(1, lo), min(total, hi) + 1))
            else:
                try:
                    num = int(chunk)
                except ValueError:
                    continue
                if 1 <= num <= total:
                    out.add(num)
        return out or set(range(1, total + 1))

    @staticmethod
    def _save_target(ctx: ActionContext, original: Path) -> Path:
        if ctx.output is not None:
            return ctx.out_file(f"{slugify(original.stem)}.pptx")
        if not ctx.flag("in_place", True):
            return original.with_name(f"{original.stem}_edited{original.suffix}")
        return original

    @staticmethod
    def _core_properties(prs: Any) -> dict[str, Any]:
        props = prs.core_properties
        return {
            key: (str(getattr(props, key)) if getattr(props, key, None) is not None else None)
            for key in (
                "title", "author", "subject", "keywords", "comments", "category",
                "created", "modified", "last_modified_by", "revision",
            )
        }

    @staticmethod
    def _layout_name(slide: Any) -> Optional[str]:
        try:
            return slide.slide_layout.name
        except Exception:
            return None

    @staticmethod
    def _has_notes(slide: Any) -> bool:
        try:
            return bool(slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip())
        except Exception:
            return False

    @staticmethod
    def _notes_text(slide: Any) -> Optional[str]:
        try:
            return slide.notes_slide.notes_text_frame.text
        except Exception:
            return None

    @staticmethod
    def _notes_frame(slide: Any, create: bool = False) -> Any:
        try:
            return slide.notes_slide.notes_text_frame
        except Exception as exc:
            raise ActionError(f"该页没有备注页，无法写入: {exc}") from exc

    @staticmethod
    def _body_frame(slide: Any) -> Any:
        for shape in slide.shapes:
            if getattr(shape, "is_placeholder", False) and not getattr(shape, "has_table", False):
                try:
                    if shape.placeholder_format.idx != 0 and shape.has_text_frame:
                        return shape.text_frame
                except Exception:
                    continue
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                return shape.text_frame
        raise ActionError("该页没有可用于追加要点的文本框")

    @staticmethod
    def _fill_body_placeholder(slide: Any, bullets: list[str]) -> None:
        for shape in slide.shapes:
            try:
                if shape.is_placeholder and shape.placeholder_format.idx != 0 and shape.has_text_frame:
                    tf = shape.text_frame
                    tf.text = str(bullets[0])
                    for extra in bullets[1:]:
                        tf.add_paragraph().text = str(extra)
                    return
            except Exception:
                continue

    @staticmethod
    def _shape_has_content(shape_data: dict[str, Any]) -> bool:
        if (shape_data.get("text") or "").strip():
            return True
        if shape_data.get("table") or shape_data.get("chart") or shape_data.get("image"):
            return True
        return bool(shape_data.get("children"))

    # ------------------------------------------------------------------ 导出
    def _export_media(self, ctx: ActionContext, stem: str, *, only_images: bool) -> dict[str, Any]:
        path = ctx.input_path()
        out_dir = ctx.out_dir("media" if not only_images else "images")
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".emf", ".wmf", ".svg"}
        count = 0
        total_bytes = 0
        names: list[str] = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.startswith("ppt/media/"):
                    continue
                ext = Path(name).suffix.lower()
                if only_images and ext not in image_exts:
                    continue
                payload = zf.read(name)
                target = ctx.out_dir("media" if not only_images else "images") / Path(name).name
                if target.exists():
                    target = target.with_name(f"{target.stem}-{count + 1}{target.suffix}")
                target.write_bytes(payload)
                count += 1
                total_bytes += len(payload)
                names.append(target.name)
        ctx.emit(out_dir, "directory", f"{count} 个媒体文件")
        return {
            "op": "export_images" if only_images else "export_media",
            "output_dir": str(out_dir),
            "count": count,
            "bytes": total_bytes,
            "files": names[:200],
        }

    def _render(self, ctx: ActionContext, stem: str, fmt: str) -> dict[str, Any]:
        fmt = (fmt or "pdf").strip().lower()
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            raise ActionError(
                "未检测到 LibreOffice（soffice），无法渲染",
                hint="安装 LibreOffice 后重试，或改用 op=export_images 直接抽图。",
            )
        out_dir = ctx.out_dir()
        cmd = [
            soffice, "--headless", "--norestore", "--convert-to", "pdf",
            "--outdir", str(out_dir), str(ctx.input_path()),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        pdf_path = out_dir / f"{ctx.input_path().stem}.pdf"
        if not pdf_path.exists():
            raise ActionError(
                "LibreOffice 转换失败",
                details={"returncode": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]},
            )
        ctx.emit(pdf_path, "pdf")
        result: dict[str, Any] = {"op": "render", "format": "pdf", "pdf": str(pdf_path)}

        if fmt == "png":
            try:
                fitz = require("fitz", "pymupdf")
            except Exception as exc:
                ctx.warn(f"未安装 pymupdf，跳过 PNG 输出: {exc}")
                return result
            doc = fitz.open(str(pdf_path))
            png_dir = ctx.out_dir("png")
            files = []
            for i, page in enumerate(doc, start=1):
                target = png_dir / f"{stem}-{i:02d}.png"
                page.get_pixmap(dpi=150).save(str(target))
                files.append(target.name)
            doc.close()
            ctx.emit(png_dir, "directory", f"{len(files)} 张 PNG")
            result.update({"png_dir": str(png_dir), "png_count": len(files), "png_files": files})
        return result

    # ------------------------------------------------------------------ OCR
    def _ocr_all(self, ctx: ActionContext, prs: Any) -> dict[str, Any]:
        from ocr import engines  # noqa: PLC0415

        if not engines.available_backends()["usable"]:
            ctx.warn("OCR 后端不可用，已跳过图片文字识别")
            return {"available": False, "reason": "无可用 OCR 后端"}
        langs = ctx.text("ocr_langs", "chi_sim+eng")
        backend = ctx.text("ocr_backend", "auto")

        tmp_dir = ctx.out_dir("_ocr_tmp")
        results: list[dict[str, Any]] = []
        for idx, slide in enumerate(prs.slides, start=1):
            for shape in slide.shapes:
                if _shape_type_name(shape) != "PICTURE":
                    continue
                try:
                    blob = shape.image.blob
                    ext = shape.image.ext
                except Exception:
                    continue
                tmp = tmp_dir / f"slide{idx}_{abs(hash(blob)) % 10**8}.{ext}"
                tmp.write_bytes(blob)
                try:
                    ocr = engines.recognize(tmp, backend=backend, langs=langs)
                    if ocr.text.strip():
                        results.append({"slide": idx, "text": ocr.text, "backend": ocr.backend,
                                        "mean_confidence": round(ocr.mean_confidence, 3)})
                except Exception as exc:
                    ctx.warn(f"第 {idx} 页图片 OCR 失败: {exc}")
                finally:
                    tmp.unlink(missing_ok=True)
        return {"available": True, "backend": backend, "images_with_text": len(results), "results": results}

    # ------------------------------------------------------------------ Markdown
    @staticmethod
    def _to_markdown(data: dict[str, Any], include_layout: bool) -> str:
        lines: list[str] = []
        title = (data.get("core_properties") or {}).get("title")
        lines.append(f"# {title or '演示文稿'}\n")
        if include_layout and data.get("slide_size_in"):
            size = data["slide_size_in"]
            lines.append(f"> 画布尺寸: {size['width']} × {size['height']} 英寸\n")
        for slide in data.get("slides", []):
            heading = slide.get("title") or f"Slide {slide['index']}"
            lines.append(f"\n## {slide['index']}. {heading}\n")
            if slide.get("layout"):
                lines.append(f"*(版式: {slide['layout']})*\n")
            for shape in slide.get("shapes", []):
                if shape.get("text", "").strip():
                    for para in shape.get("paragraphs", []) or []:
                        indent = "  " * int(para.get("level", 0))
                        text = para.get("text", "").strip()
                        if text:
                            lines.append(f"{indent}- {text}")
                    if not shape.get("paragraphs") and shape.get("text", "").strip():
                        lines.append(f"- {shape['text'].strip()}")
                table = shape.get("table")
                if table and table.get("data"):
                    rows = table["data"]
                    lines.append("")
                    lines.append("| " + " | ".join(str(c).replace("\n", " ") for c in rows[0]) + " |")
                    lines.append("|" + " --- |" * len(rows[0]))
                    for row in rows[1:]:
                        lines.append("| " + " | ".join(str(c).replace("\n", " ") for c in row) + " |")
                    lines.append("")
                chart = shape.get("chart")
                if chart:
                    lines.append(f"\n**图表** ({chart.get('chart_type')})")
                    for serie in chart.get("series", []):
                        lines.append(f"- {serie.get('name')}: {serie.get('values')}")
                image = shape.get("image")
                if image and image.get("filename"):
                    lines.append(f"\n![{image['filename']}]({image['filename']})")
            if slide.get("notes"):
                lines.append(f"\n> **备注**: {slide['notes'].strip()}\n")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """pptx_handler 独立运行

  python handlers/pptx_handler.py probe   deck.pptx [--json]
  python handlers/pptx_handler.py read    deck.pptx -p include_notes=false
  python handlers/pptx_handler.py analyze deck.pptx -p top_n=30
  python handlers/pptx_handler.py transform deck.pptx -p op=to_markdown -o ./out
  python handlers/pptx_handler.py edit    deck.pptx -p op=replace_text -p find=旧 -p replace=新
  python handlers/pptx_handler.py cap

参数一律通过 -p key=value 传入；加 --json 输出机器可读结果。
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(PptxHandler().capabilities(), ensure_ascii=False, indent=2))
        return 0
    action_name, rest = args[0], args[1:]
    cli_args = ["run", "-m", MODULE, "-a", action_name]
    if rest and not rest[0].startswith("-"):
        cli_args += ["-i", rest[0]]
        rest = rest[1:]
    cli_args += ["--json"] if "--json" in rest else []
    cli_args += [tok for tok in rest if tok != "--json"]
    return cli_main(cli_args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
