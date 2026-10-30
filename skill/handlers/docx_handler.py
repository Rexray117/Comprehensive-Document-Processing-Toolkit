"""DOCX 文档处理模块（python-docx + OOXML 直读写）。

相对通用方案的三点增强
----------------------
1. **真实文档顺序**：直接遍历 ``w:body`` 子元素，段落与表格严格按正文顺序输出，
   不会出现「先所有段落、后所有表格」的错位问题。
2. **真 OMML 公式支持**：读取时导出公式的原始 OMML XML（可编辑、可迁移），
   写入时接受 OMML 片段直接插入，而不是把公式降级成图片或纯文本。
3. **同源替换算法**：与 PPTX 模块共用 ``core.textops``，跨 run 命中且保留格式。

独立运行
--------
    python handlers/docx_handler.py read  report.docx -p include_formulas=true
    python handlers/docx_handler.py transform report.docx -p op=extract_omml
    python handlers/docx_handler.py edit  report.docx -p op=replace_text -p find=旧 -p replace=新
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ActionError, InputError, ParamError, require  # noqa: E402
from core.textops import build_pattern, replace_in_paragraph, replace_in_runs  # noqa: E402
from core.utils import human_bytes, slugify, text_stats, to_json, top_keywords  # noqa: E402

MODULE = "docx"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_HEADING_STYLE_RE = re.compile(r"(heading|标题|標題)\s*(\d+)", re.IGNORECASE)
_TITLE_STYLE_RE = re.compile(r"^(title|标题|標題)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 文档遍历与解析
# ---------------------------------------------------------------------------
def _heading_level(paragraph: Any) -> Optional[int]:
    """判定标题层级：优先 outlineLvl（语言无关），退回样式名。"""
    try:
        p_pr = paragraph._p.find(f"{W}pPr")  # noqa: SLF001
        if p_pr is not None:
            lvl = p_pr.find(f"{W}outlineLvl")
            if lvl is not None:
                val = lvl.get(f"{W}val")
                if val is not None:
                    return int(val) + 1
    except Exception:
        pass
    name = (getattr(paragraph.style, "name", "") or "").strip()
    if _TITLE_STYLE_RE.match(name):
        return 0
    m = _HEADING_STYLE_RE.search(name)
    if m:
        try:
            return int(m.group(2))
        except ValueError:
            return None
    return None


def _omml_nodes(paragraph: Any) -> list[Any]:
    """取出段落中的 OMML 公式节点（自动去重，避免 oMathPara 嵌套重复）。"""
    el = paragraph._p  # noqa: SLF001
    para_wrappers = list(el.iter(M + "oMathPara"))
    if para_wrappers:
        nodes = [n for wrapper in para_wrappers for n in wrapper.iter(M + "oMath")]
    else:
        nodes = list(el.iter(M + "oMath"))
    seen: set[int] = set()
    unique: list[Any] = []
    for node in nodes:
        if id(node) not in seen:
            seen.add(id(node))
            unique.append(node)
    return unique


def _omml_text(node: Any) -> str:
    """从 OMML 节点抽取线性纯文本（结构信息仍在 XML 里）。"""
    parts: list[str] = []
    for t in node.iter(M + "t"):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def _omml_xml(node: Any) -> str:
    from lxml import etree  # noqa: PLC0415

    return etree.tostring(node, encoding="unicode", pretty_print=False)


def _iter_blocks(doc: Any) -> Iterator[tuple[str, Any]]:
    """按正文真实顺序产出 ``(kind, obj)``，kind 为 ``paragraph`` / ``table``。"""
    from docx.table import Table  # noqa: PLC0415
    from docx.text.paragraph import Paragraph  # noqa: PLC0415

    for child in doc.element.body.iterchildren():
        if child.tag == f"{W}p":
            yield "paragraph", Paragraph(child, doc)
        elif child.tag == f"{W}tbl":
            yield "table", Table(child, doc)


def _table_rows(table: Any) -> list[list[str]]:
    return [[cell.text for cell in row.cells] for row in table.rows]


def _frame_text(frame: Any) -> str:
    try:
        return "\n".join(p.text for p in frame.paragraphs)
    except Exception:
        return ""


def _image_rels(doc: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rel in getattr(doc.part, "rels", {}).values():
        if "image" not in getattr(rel, "reltype", ""):
            continue
        try:
            part = rel.target_part
            rows.append(
                {
                    "partname": str(getattr(part, "partname", "")),
                    "filename": Path(str(getattr(part, "partname", ""))).name,
                    "bytes": len(part.blob),
                    "sha1": getattr(part, "sha1", None),
                    "rel_id": rel.rId,
                }
            )
        except Exception as exc:
            rows.append({"error": str(exc), "rel_id": getattr(rel, "rId", None)})
    return rows


def _safe_add_paragraph(doc: Any, text: str = "", style: Optional[str] = None) -> Any:
    """样式不存在时自动降级为默认样式，避免 KeyError。"""
    if style:
        try:
            return doc.add_paragraph(text, style=style)
        except KeyError:
            pass
    return doc.add_paragraph(text)


class DocxHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "DOCX 文档"
    description = (
        "处理 .docx 文档：按真实正文顺序读取段落/表格/图片/页眉页脚，导出 OMML 公式原始 XML，"
        "转 Markdown、跨 run 查找替换、插入表格与可编辑公式、批量清理空段。"
    )
    extensions = (".docx", ".dotx")
    kinds = ("docx",)
    required_deps = {"docx": "python-docx"}  # docx 模块全部动作的基础
    optional_deps = {"mammoth": "mammoth", "lxml": "lxml"}

    # ======================================================== probe
    @action(
        "probe",
        summary="ZIP 层快速探测：段落/表格/图片/公式/批注的数量与文档体量",
        params={"deep": "true 时用 python-docx 补充节与样式信息（默认 false）"},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        if not zipfile.is_zipfile(path):
            raise InputError("不是合法的 OOXML（ZIP）文件，可能已损坏", details={"path": str(path)})

        info: dict[str, Any] = {
            "file": path.name,
            "size": path.stat().st_size,
            "size_human": human_bytes(path.stat().st_size),
        }
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            media = [n for n in names if n.startswith("word/media/")]
            info.update(
                {
                    "part_count": len(names),
                    "media_count": len(media),
                    "media_bytes": sum(zf.getinfo(n).file_size for n in media),
                    "has_headers": any(n.startswith("word/header") for n in names),
                    "has_footers": any(n.startswith("word/footer") for n in names),
                    "has_footnotes": any(n.startswith("word/footnotes") for n in names),
                    "has_comments": "word/comments.xml" in names,
                    "has_tracked_changes": False,
                    "has_macros": any("vbaProject" in n for n in names),
                    "has_embedded_objects": any(n.startswith("word/embeddings/") for n in names),
                    "styles_part": "word/styles.xml" in names,
                    "numbering_part": "word/numbering.xml" in names,
                    "media_by_ext": dict(Counter(Path(n).suffix.lower() for n in media).most_common()),
                }
            )
            document_xml = zf.read("word/document.xml") if "word/document.xml" in names else b""
            text = document_xml.decode("utf-8", errors="replace")
            info["paragraph_count"] = len(re.findall(r"<w:p[ >]", text))
            info["table_count"] = len(re.findall(r"<w:tbl[ >]", text))
            info["formula_count"] = len(re.findall(r"<m:oMath[ >]", text))
            info["has_tracked_changes"] = ("<w:ins " in text) or ("<w:del " in text)
            info["drawing_count"] = len(re.findall(r"<w:drawing[ >]", text))
            info["hyperlink_count"] = len(re.findall(r"<w:hyperlink[ >]", text))
            info["bookmark_count"] = len(re.findall(r"<w:bookmarkStart[ >]", text))

        if ctx.flag("deep", False):
            docx = require("docx", "python-docx")
            doc = docx.Document(str(path))
            info["sections"] = [self._section_info(s) for s in doc.sections]
            info["styles_in_use"] = dict(
                Counter(p.style.name for p in doc.paragraphs if p.style is not None).most_common(20)
            )
            info["core_properties"] = self._core_properties(doc)
        return info

    # ======================================================== read
    @action(
        "read",
        summary="按正文真实顺序读取段落/表格/图片/公式/页眉页脚",
        params={
            "include_tables": "是否输出表格数据（默认 true）",
            "include_headers": "是否输出页眉页脚（默认 true）",
            "include_formulas": "是否输出 OMML 原始 XML（默认 true）",
            "include_images": "是否输出图片清单（默认 true）",
            "include_empty": "是否保留空段落（默认 false）",
            "max_formula_xml": "单个公式 XML 最大字符数（默认 4000，超出截断）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        docx = require("docx", "python-docx")
        doc = docx.Document(str(ctx.input_path()))
        include_tables = ctx.flag("include_tables", True)
        include_empty = ctx.flag("include_empty", False)
        include_formulas = ctx.flag("include_formulas", True)
        max_xml = ctx.bounded_int("max_formula_xml", 4000, low=100, high=1_000_000)

        blocks: list[dict[str, Any]] = []
        outline: list[dict[str, Any]] = []
        formulas: list[dict[str, Any]] = []
        styles: Counter[str] = Counter()

        for index, (kind, obj) in enumerate(_iter_blocks(doc)):
            if kind == "paragraph":
                text = obj.text or ""
                style_name = getattr(obj.style, "name", None)
                styles[str(style_name)] += 1
                if not text.strip() and not include_empty:
                    continue
                level = _heading_level(obj)
                entry: dict[str, Any] = {
                    "block": index,
                    "type": "heading" if level is not None else "paragraph",
                    "level": level,
                    "style": style_name,
                    "text": text,
                    "alignment": str(getattr(obj.alignment, "name", obj.alignment)),
                }
                nodes = _omml_nodes(obj)
                if nodes:
                    entry["formula_count"] = len(nodes)
                    if include_formulas:
                        entry["formulas"] = [
                            {
                                "text": _omml_text(n),
                                "xml": (lambda x: x if len(x) <= max_xml else x[:max_xml] + "<!--truncated-->")(_omml_xml(n)),
                            }
                            for n in nodes
                        ]
                    for n in nodes:
                        formulas.append(
                            {
                                "block": index,
                                "text": _omml_text(n),
                                "paragraph": text,
                                "xml": (lambda x: x if len(x) <= max_xml else x[:max_xml] + "<!--truncated-->")(_omml_xml(n)),
                            }
                        )
                blocks.append(entry)
                if level is not None and text.strip():
                    outline.append({"level": level, "text": text.strip(), "block": index})
            else:  # table
                if not include_tables:
                    continue
                rows = _table_rows(obj)
                blocks.append(
                    {
                        "block": index,
                        "type": "table",
                        "rows": len(rows),
                        "cols": len(rows[0]) if rows else 0,
                        "data": rows,
                    }
                )
                styles["<table>"] += 1

        result: dict[str, Any] = {
            "block_count": len(blocks),
            "outline": outline,
            "formulas_total": len(formulas),
            "sections": [self._section_info(s) for s in doc.sections],
            "core_properties": self._core_properties(doc),
            "styles_used": dict(styles.most_common(30)),
            "blocks": blocks,
        }
        if include_formulas:
            result["formulas"] = formulas
        if ctx.flag("include_images", True):
            result["images"] = _image_rels(doc)
        if ctx.flag("include_headers", True):
            result["headers_footers"] = self._headers_footers(doc)
        return result

    # ======================================================== analyze
    @action(
        "analyze",
        summary="文档分析：大纲层级、字数统计、关键词、样式分布、公式/图片、结构问题诊断",
        params={"top_n": "关键词数量（默认 20）", "max_heading_level": "视为过深的标题层级（默认 4）"},
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        docx = require("docx", "python-docx")
        doc = docx.Document(str(ctx.input_path()))
        top_n = ctx.bounded_int("top_n", 20, low=1, high=200)
        max_level = ctx.bounded_int("max_heading_level", 4, low=1, high=9)

        texts: list[str] = []
        outline: list[dict[str, Any]] = []
        styles: Counter[str] = Counter()
        paragraph_count = 0
        empty_count = 0
        table_count = 0
        table_rows_total = 0
        formula_count = 0
        image_count = 0
        deepest = 0
        level_jumps: list[str] = []
        prev_level: Optional[int] = None

        for _, (kind, obj) in enumerate(_iter_blocks(doc)):
            if kind == "paragraph":
                paragraph_count += 1
                text = obj.text or ""
                styles[str(getattr(obj.style, "name", None))] += 1
                if not text.strip():
                    empty_count += 1
                    continue
                texts.append(text)
                level = _heading_level(obj)
                if level is not None:
                    outline.append({"level": level, "text": text.strip()})
                    deepest = max(deepest, level)
                    if prev_level is not None and level > prev_level + 1:
                        level_jumps.append(f"「{text.strip()[:20]}」由 H{prev_level} 跳到 H{level}")
                    prev_level = level
                formula_count += len(_omml_nodes(obj))
            else:
                table_count += 1
                rows = _table_rows(obj)
                table_rows_total += len(rows)
                for row in rows:
                    texts.extend(row)

        full_text = "\n".join(texts)
        images = _image_rels(doc)
        image_count = len(images)
        stats = text_stats(full_text)

        issues: list[str] = []
        if not outline:
            issues.append("未检测到标题层级，无法生成目录，建议对章节套用 Heading 样式")
        if level_jumps:
            issues.append(f"存在 {len(level_jumps)} 处标题跳级：" + "；".join(level_jumps[:3]))
        if deepest > max_level:
            issues.append(f"标题层级最深达 H{deepest}，超出建议的 H{max_level}，结构可能过深")
        if paragraph_count and empty_count / paragraph_count > 0.35:
            issues.append(f"空段落占比 {empty_count / paragraph_count:.0%}，建议用段间距代替空行")
        if table_count and table_rows_total / max(1, table_count) > 30:
            issues.append("存在行数很多的表格，建议拆分或改为附录")
        if formula_count:
            issues.append(f"含 {formula_count} 个 OMML 公式，转换其它格式时请用 extract_omml 保留原始结构")

        return {
            "paragraph_count": paragraph_count,
            "empty_paragraph_count": empty_count,
            "table_count": table_count,
            "table_rows_total": table_rows_total,
            "formula_count": formula_count,
            "image_count": image_count,
            "image_bytes": sum(i.get("bytes", 0) for i in images),
            "outline": outline,
            "outline_depth": deepest,
            "text_stats": stats,
            "keywords": top_keywords(full_text, top_n=top_n),
            "styles_used": dict(styles.most_common(30)),
            "headers_footers": self._headers_footers(doc),
            "issues": issues,
        }

    # ======================================================== transform
    @action(
        "transform",
        summary="导出 Markdown / 纯文本 / 图片 / OMML 公式 / HTML / 原始 XML",
        params={
            "op": "to_markdown | to_text | export_images | extract_omml | to_html | dump_xml",
            "formula_as": "to_markdown 时公式写法：text（默认，纯文本）或 omml（嵌入原始 XML 注释）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        # target 是"想要什么产物"的自然叫法（docx/pdf/xlsx 三处习惯不一致），
        # 未给 op 时按同义键兜底，避免 options.target="pdf" 被静默忽略后仍返回 ok=true。
        op = (ctx.text("op") or ctx.text("target") or "to_markdown").strip().lower()
        stem = slugify(ctx.input_path().stem)

        if op in ("to_markdown", "markdown", "md"):
            data = self.read(ctx)
            body = self._to_markdown(data, ctx.text("formula_as", "text"))
            target = ctx.out_file(f"{stem}.md")
            target.write_text(body, encoding="utf-8")
            ctx.emit(target, "markdown")
            return {"op": op, "blocks": data["block_count"], "chars": len(body),
                    "formulas": data.get("formulas_total", 0)}

        if op in ("to_text", "text"):
            data = self.read(ctx)
            lines: list[str] = []
            for block in data["blocks"]:
                if block["type"] == "table":
                    for row in block.get("data", []):
                        lines.append("\t".join(row))
                    lines.append("")
                else:
                    if block.get("level") is not None:
                        lines.append("\n" + block["text"])
                    else:
                        lines.append(block["text"])
            body = "\n".join(lines)
            target = ctx.out_file(f"{stem}.txt")
            target.write_text(body, encoding="utf-8")
            ctx.emit(target, "text")
            return {"op": op, "chars": len(body)}

        if op in ("export_images", "images"):
            docx = require("docx", "python-docx")
            doc = docx.Document(str(ctx.input_path()))
            out_dir = ctx.out_dir("images")
            written: list[str] = []
            for index, info in enumerate(_image_rels(doc), start=1):
                rel = next((r for r in doc.part.rels.values() if getattr(r, "rId", None) == info.get("rel_id")), None)
                if rel is None:
                    continue
                filename = info.get("filename") or f"image-{index}.bin"
                target = out_dir / filename
                if target.exists():
                    target = target.with_name(f"{target.stem}-{index}{target.suffix}")
                target.write_bytes(rel.target_part.blob)
                written.append(target.name)
            ctx.emit(out_dir, "directory", f"{len(written)} 张图片")
            return {"op": op, "output_dir": str(out_dir), "count": len(written), "files": written}

        if op in ("extract_omml", "omml"):
            data = self.read(ctx)
            formulas = data.get("formulas", [])
            out_dir = ctx.out_dir("omml")
            xml_dir = ctx.out_dir("omml/xml")
            files: list[str] = []
            for index, formula in enumerate(formulas, start=1):
                target = xml_dir / f"formula-{index:03d}.xml"
                target.write_text(
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + formula["xml"],
                    encoding="utf-8",
                )
                files.append(target.name)
            index_file = out_dir / f"{stem}.omml.json"
            index_file.write_text(
                to_json({k: v for k, v in data.items() if k != "blocks"}), encoding="utf-8"
            )
            ctx.emit(index_file, "json")
            if files:
                ctx.emit(xml_dir, "directory", f"{len(files)} 个 OMML 文件")
            return {"op": op, "count": len(formulas), "xml_dir": str(xml_dir), "files": files,
                    "note": "每个 XML 均为可直接复制回 Word 的真 OMML 公式片段"}

        if op in ("to_html", "html"):
            try:
                mammoth = require("mammoth", "mammoth")
                result = mammoth.convert_to_html(str(ctx.input_path()))
                body, messages = result.value, result.messages
            except Exception:
                ctx.warn("未安装 mammoth，改用内置轻量 Markdown→HTML 渲染")
                data = self.read(ctx)
                body = self._markdown_to_html(self._to_markdown(data, "text"))
                messages = []
            target = ctx.out_file(f"{stem}.html")
            target.write_text(
                "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
                f"<title>{ctx.input_path().stem}</title>\n"
                "<style>body{max-width:900px;margin:40px auto;font-family:-apple-system,'Segoe UI',"
                "'Microsoft YaHei',sans-serif;line-height:1.75;padding:0 16px}"
                "table{border-collapse:collapse;width:100%;margin:1em 0}"
                "td,th{border:1px solid #d0d0d0;padding:6px 10px}th{background:#f5f5f5}</style>\n"
                "</head>\n<body>\n" + body + "\n</body>\n</html>\n",
                encoding="utf-8",
            )
            ctx.emit(target, "html")
            return {"op": op, "chars": len(body), "messages": len(messages)}

        if op in ("dump_xml", "xml"):
            path = ctx.input_path()
            with zipfile.ZipFile(path) as zf:
                candidates = [n for n in zf.namelist() if n.startswith("word/") and n.endswith(".xml")]
                out_dir = ctx.out_dir("xml")
                written = []
                for name in candidates:
                    target = out_dir / Path(name).name
                    target.write_bytes(zf.read(name))
                    written.append(target.name)
            ctx.emit(out_dir, "directory", f"{len(written)} 个 XML")
            return {"op": op, "output_dir": str(out_dir), "count": len(written), "files": written}

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: to_markdown / to_text / export_images / extract_omml / to_html / dump_xml",
        )

    # ======================================================== edit
    @action(
        "edit",
        summary="修改文档：查找替换（保格式）/追加段落标题/插表格/插入 OMML 公式/清理空段/写属性",
        params={
            "op": "replace_text | add_paragraph | append_heading | insert_table | insert_formula | "
                  "append_markdown | remove_empty_paragraphs | set_core_properties",
            "find": "replace_text：查找内容",
            "replace": "replace_text：替换内容",
            "regex": "replace_text：正则模式（默认 false）",
            "text": "add_paragraph / append_heading：文本内容",
            "level": "append_heading：标题层级（默认 1）",
            "style": "add_paragraph：段落样式名",
            "rows": "insert_table：二维数组或 CSV 行文本",
            "formula": "insert_formula：OMML 片段（含 <m:oMath>）",
            "in_place": "是否就地保存（默认 true）",
        },
        modifies_input=True,
        tags=("edit",),
    )
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        docx = require("docx", "python-docx")
        path = ctx.input_path()
        doc = docx.Document(str(path))
        op = (ctx.text("op", "replace_text") or "replace_text").strip().lower()

        operations = {
            "replace_text": self._op_replace_text,
            "add_paragraph": self._op_add_paragraph,
            "append_heading": self._op_append_heading,
            "insert_table": self._op_insert_table,
            "insert_formula": self._op_insert_formula,
            "append_markdown": self._op_append_markdown,
            "remove_empty_paragraphs": self._op_remove_empty,
            "set_core_properties": self._op_set_core_properties,
        }
        handler_fn = operations.get(op)
        if handler_fn is None:
            raise ParamError(f"不支持的 op: {op}", hint=f"可选: {', '.join(sorted(operations))}")

        changed = handler_fn(ctx, doc) or 0
        target = self._save_target(ctx, path)
        doc.save(str(target))
        if target != path:
            ctx.emit(target, "docx")
        return {
            "op": op,
            "changed": changed,
            "saved_to": str(target),
            "in_place": target == path,
            "backup": ctx.get("__backup__"),
            "paragraph_count": len(doc.paragraphs),
        }

    # ------------------------------------------------------------------ edit 子操作
    def _op_replace_text(self, ctx: ActionContext, doc: Any) -> int:
        find = str(ctx.require("find"))
        repl = ctx.text("replace", "")
        pattern = build_pattern(find, regex=ctx.flag("regex", False),
                                ignore_case=ctx.flag("ignore_case", False))
        scope = ctx.text("scope", "all").strip().lower()
        total = 0

        if scope in ("all", "body"):
            for paragraph in doc.paragraphs:
                total += replace_in_paragraph(paragraph, pattern, repl)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for paragraph in cell.paragraphs:
                            total += replace_in_paragraph(paragraph, pattern, repl)
        if scope in ("all", "headers", "footer", "headers_footers"):
            for section in doc.sections:
                for frame in (section.header, section.footer,
                              section.first_page_header, section.first_page_footer,
                              section.even_page_header, section.even_page_footer):
                    try:
                        for paragraph in frame.paragraphs:
                            total += replace_in_paragraph(paragraph, pattern, repl)
                    except Exception:
                        continue
        if scope in ("all", "omml"):
            # 公式里的 m:t 文本节点：直接做 XML 级替换
            for node in doc.element.body.iter(M + "t"):
                if node.text and pattern.search(node.text):
                    node.text = pattern.sub(repl, node.text)
                    total += 1
        return total

    def _op_add_paragraph(self, ctx: ActionContext, doc: Any) -> int:
        text = str(ctx.require("text"))
        style = ctx.text("style", "") or None
        _safe_add_paragraph(doc, text, style)
        return 1

    def _op_append_heading(self, ctx: ActionContext, doc: Any) -> int:
        text = str(ctx.require("text"))
        level = ctx.bounded_int("level", 1, low=1, high=9)
        style = f"Heading {level}"
        try:
            doc.add_heading(text, level=level)
        except Exception:
            _safe_add_paragraph(doc, text, style)
        return 1

    def _op_insert_table(self, ctx: ActionContext, doc: Any) -> int:
        rows = self._coerce_rows(ctx.get("rows"))
        if not rows:
            raise ParamError("insert_table 需要 rows 参数，如 -p rows='[[\"名称\",\"数值\"],[\"A\",\"1\"]]'")
        width = max(len(r) for r in rows)
        table = doc.add_table(rows=len(rows), cols=width)
        try:
            table.style = ctx.text("table_style", "Table Grid")
        except Exception:
            pass
        for r, row in enumerate(rows):
            for c in range(width):
                cell = table.cell(r, c)
                cell.text = str(row[c]) if c < len(row) else ""
                if r == 0 and ctx.flag("bold_header", True):
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
        return len(rows)

    def _op_insert_formula(self, ctx: ActionContext, doc: Any) -> int:
        """插入真 OMML 公式（保持可编辑，非图片）。"""
        from docx.oxml import parse_xml  # noqa: PLC0415

        raw = str(ctx.require("formula")).strip()
        if not raw.startswith("<"):
            raise ParamError("formula 参数必须是 OMML XML 片段，例如 <m:oMath>...</m:oMath>")
        paragraph = doc.add_paragraph()
        try:
            node = parse_xml(raw)
        except Exception as exc:
            raise ActionError(
                f"OMML 解析失败: {exc}",
                hint="确认片段是合法 XML 且带 m: 命名空间；可用 extract_omml 导出的片段作为模板。",
            ) from exc
        if node.tag == f"{M}oMathPara":
            for child in list(node):
                paragraph._p.append(child)  # noqa: SLF001
        elif node.tag == f"{M}oMath":
            paragraph._p.append(node)  # noqa: SLF001
        else:
            raise ParamError(f"未识别的 OMML 根元素: {node.tag}")
        return 1

    def _op_append_markdown(self, ctx: ActionContext, doc: Any) -> int:
        text = str(ctx.require("text"))
        added = 0
        pending_table: list[list[str]] = []

        def flush_table() -> None:
            nonlocal added, pending_table
            if not pending_table:
                return
            rows = [r for r in pending_table if not re.fullmatch(r"\s*\|?[\s:\-|]+\|?\s*", "|".join(r) or "")]
            if rows:
                table = doc.add_table(rows=len(rows), cols=len(rows[0]))
                try:
                    table.style = "Table Grid"
                except Exception:
                    pass
                for r, row in enumerate(rows):
                    for c, value in enumerate(row):
                        if c < len(rows[0]):
                            table.cell(r, c).text = value
                added += len(rows)
            pending_table = []

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                pending_table.append([c.strip() for c in stripped.strip("|").split("|")])
                continue
            flush_table()
            if not stripped:
                continue
            heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if heading:
                level = len(heading.group(1))
                try:
                    doc.add_heading(heading.group(2), level=level)
                except Exception:
                    _safe_add_paragraph(doc, heading.group(2), f"Heading {level}")
                added += 1
                continue
            if re.match(r"^[-*+]\s+", stripped):
                _safe_add_paragraph(doc, re.sub(r"^[-*+]\s+", "", stripped), "List Bullet")
                added += 1
                continue
            if re.match(r"^\d+[.)]\s+", stripped):
                _safe_add_paragraph(doc, re.sub(r"^\d+[.)]\s+", "", stripped), "List Number")
                added += 1
                continue
            if stripped.startswith(">"):
                _safe_add_paragraph(doc, stripped.lstrip("> ").strip(), "Quote")
                added += 1
                continue
            if re.fullmatch(r"[-*_]{3,}", stripped):
                continue
            _safe_add_paragraph(doc, stripped)
            added += 1
        flush_table()
        return added

    def _op_remove_empty(self, ctx: ActionContext, doc: Any) -> int:
        removed = 0
        for paragraph in list(doc.paragraphs):
            if paragraph.text.strip():
                continue
            if _omml_nodes(paragraph) or paragraph._p.findall(f".//{W}drawing"):  # noqa: SLF001
                continue
            paragraph._p.getparent().remove(paragraph._p)  # noqa: SLF001
            removed += 1
        return removed

    def _op_set_core_properties(self, ctx: ActionContext, doc: Any) -> int:
        props = doc.core_properties
        mapping = {
            "title": "title", "author": "author", "subject": "subject",
            "keywords": "keywords", "comments": "comments", "category": "category",
            "last_modified_by": "last_modified_by", "revision": "revision",
        }
        changed = 0
        for key, attr in mapping.items():
            value = ctx.get(key)
            if value is not None:
                setattr(props, attr, value if attr == "revision" else str(value))
                changed += 1
        return changed

    # ------------------------------------------------------------------ 内部辅助
    @staticmethod
    def _coerce_rows(raw: Any) -> list[list[str]]:
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = [[c.strip() for c in line.split(",")] for line in raw.splitlines() if line.strip()]
            raw = parsed
        if not isinstance(raw, (list, tuple)):
            return []
        rows: list[list[str]] = []
        for row in raw:
            if isinstance(row, (list, tuple)):
                rows.append([str(c) for c in row])
            else:
                rows.append([str(row)])
        return rows

    @staticmethod
    def _save_target(ctx: ActionContext, original: Path) -> Path:
        if ctx.output is not None:
            return ctx.out_file(f"{slugify(original.stem)}.docx")
        if not ctx.flag("in_place", True):
            return original.with_name(f"{original.stem}_edited{original.suffix}")
        return original

    @staticmethod
    def _section_info(section: Any) -> dict[str, Any]:
        return {
            "orientation": str(getattr(section, "orientation", None)),
            "page_width_in": round(section.page_width.inches, 2) if section.page_width else None,
            "page_height_in": round(section.page_height.inches, 2) if section.page_height else None,
            "left_margin_in": round(section.left_margin.inches, 2) if section.left_margin else None,
            "right_margin_in": round(section.right_margin.inches, 2) if section.right_margin else None,
            "top_margin_in": round(section.top_margin.inches, 2) if section.top_margin else None,
            "bottom_margin_in": round(section.bottom_margin.inches, 2) if section.bottom_margin else None,
            "header_distance_in": round(section.header_distance.inches, 2) if section.header_distance else None,
        }

    @staticmethod
    def _core_properties(doc: Any) -> dict[str, Any]:
        props = doc.core_properties
        return {
            key: (str(getattr(props, key)) if getattr(props, key, None) is not None else None)
            for key in (
                "title", "author", "subject", "keywords", "comments", "category",
                "created", "modified", "last_modified_by", "revision",
            )
        }

    @staticmethod
    def _headers_footers(doc: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for index, section in enumerate(doc.sections):
            for label in ("header", "footer", "first_page_header", "first_page_footer",
                          "even_page_header", "even_page_footer"):
                try:
                    text = _frame_text(getattr(section, label)).strip()
                except Exception:
                    continue
                if text:
                    out[f"section{index}:{label}"] = text
        return out

    # ------------------------------------------------------------------ Markdown
    @staticmethod
    def _to_markdown(data: dict[str, Any], formula_as: str) -> str:
        lines: list[str] = []
        title = (data.get("core_properties") or {}).get("title")
        lines.append(f"# {title or '文档'}\n")
        if data.get("outline"):
            lines.append("## 目录\n")
            for item in data["outline"]:
                indent = "  " * max(0, int(item.get("level", 1)) - 1)
                lines.append(f"{indent}- {item['text']}")
            lines.append("")

        for block in data.get("blocks", []):
            if block.get("type") == "table":
                rows = block.get("data") or []
                if not rows:
                    continue
                lines.append("")
                lines.append("| " + " | ".join(str(c).replace("\n", " ").replace("|", "\\|") for c in rows[0]) + " |")
                lines.append("|" + " --- |" * len(rows[0]))
                for row in rows[1:]:
                    lines.append("| " + " | ".join(str(c).replace("\n", " ").replace("|", "\\|") for c in row) + " |")
                lines.append("")
                continue

            level = block.get("level")
            text = (block.get("text") or "").rstrip()
            if level is not None:
                hashes = "#" * max(1, min(6, int(level) + 1))
                lines.append(f"\n{hashes} {text}\n")
            elif text.strip():
                lines.append(text)
            for formula in block.get("formulas", []) or []:
                if formula_as == "omml":
                    lines.append(f"\n<!-- OMML: {formula['xml'][:2000]} -->\n")
                else:
                    lines.append(f"\n$$\n{formula['text']}\n$$\n")
        return "\n".join(lines)

    @staticmethod
    def _markdown_to_html(md: str) -> str:
        """极简 Markdown → HTML（仅在未安装 mammoth 时兜底）。"""
        html: list[str] = []
        in_table = False
        for raw in md.splitlines():
            line = raw.rstrip()
            if line.startswith("|") and line.endswith("|"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    continue
                if not in_table:
                    html.append("<table>")
                    in_table = True
                    html.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
                else:
                    html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
                continue
            if in_table:
                html.append("</table>")
                in_table = False
            heading = re.match(r"^(#{1,6})\s+(.*)$", line)
            if heading:
                lvl = len(heading.group(1))
                html.append(f"<h{lvl}>{heading.group(2)}</h{lvl}>")
            elif line.strip():
                html.append(f"<p>{line}</p>")
        if in_table:
            html.append("</table>")
        return "\n".join(html)


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """docx_handler 独立运行

  python handlers/docx_handler.py probe     report.docx
  python handlers/docx_handler.py read      report.docx -p include_tables=false
  python handlers/docx_handler.py analyze   report.docx -p top_n=30
  python handlers/docx_handler.py transform report.docx -p op=extract_omml -o ./out
  python handlers/docx_handler.py edit      report.docx -p op=replace_text -p find=旧 -p replace=新
  python handlers/docx_handler.py cap
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(DocxHandler().capabilities(), ensure_ascii=False, indent=2))
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
