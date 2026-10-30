"""PDF 文档读取模块（pypdf，纯 Python、离线轻量）。

定位
----
以「读」为核心的 PDF 模块：元数据与加密状态探测、按页文本提取、
书签大纲解析、关键词与空页质量诊断、导出文本/Markdown/JSON、按页拆分。
本模块刻意不做写编辑（PDF 写入场景复杂，签名/表单易损坏），
需要修改 PDF 时建议先转 DOCX/HTML 再处理。

独立运行
--------
    python handlers/pdf_handler.py probe     doc.pdf
    python handlers/pdf_handler.py read      doc.pdf -p page_range=1-5
    python handlers/pdf_handler.py analyze   doc.pdf -p top_n=30
    python handlers/pdf_handler.py transform doc.pdf -p op=to_text
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ActionError, ParamError, require  # noqa: E402
from core.utils import slugify, text_stats, top_keywords  # noqa: E402

MODULE = "pdf"

_PDF_DEP = {"pypdf": "pypdf"}


def _reader(path: Path):
    pypdf = require("pypdf", _PDF_DEP["pypdf"])
    try:
        return pypdf.PdfReader(str(path))
    except Exception as exc:
        raise ActionError(
            f"无法打开 PDF: {path.name}",
            hint=f"文件可能损坏或加密。{type(exc).__name__}: {exc}",
        ) from exc


def _flatten_outline(items: Any, depth: int = 1, limit: int = 500) -> list[dict[str, Any]]:
    """递归展平 pypdf 的嵌套书签结构。"""
    out: list[dict[str, Any]] = []
    try:
        entries = list(items)
    except Exception:
        return out
    for entry in entries:
        if isinstance(entry, list):
            out.extend(_flatten_outline(entry, depth + 1, limit - len(out)))
            if len(out) >= limit:
                break
            continue
        try:
            title = str(getattr(entry, "title", "") or "")
            page_index = None
            try:
                page_index = entry.page.indirect_reference.idnum if entry.page else None
            except Exception:
                page_index = None
            out.append({"depth": depth, "title": title, "page_object_id": page_index})
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def _parse_page_range(spec: str, total: int) -> tuple[int, int]:
    """把 '3-7' / '5' / 'all' 解析为 0 基闭区间。"""
    spec = (spec or "all").strip().lower()
    if spec in ("", "all", "*"):
        return 0, total - 1
    if "-" in spec:
        left, _, right = spec.partition("-")
        try:
            start = max(1, int(left))
            end = min(total, int(right))
        except ValueError as exc:
            raise ParamError(f"page_range 格式非法: {spec}", hint="示例: 1-5 或 all") from exc
    else:
        try:
            start = end = int(spec)
        except ValueError as exc:
            raise ParamError(f"page_range 格式非法: {spec}", hint="示例: 3 或 1-5 或 all") from exc
    if start > end:
        raise ParamError(f"page_range 起止颠倒: {spec}")
    return start - 1, end - 1


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class PdfHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "PDF 文档"
    description = (
        "读取 .pdf：元数据与加密探测、按页文本提取（支持页码区间）、书签大纲、"
        "关键词与空页诊断，导出文本/Markdown/JSON、按页拆分。只读模块，不做写编辑。"
    )
    extensions = (".pdf",)
    kinds = ("pdf",)
    required_deps = _PDF_DEP  # pypdf 缺失时 pdf 模块四个动作全部不可用

    # ======================================================== probe
    @action(
        "probe",
        summary="快速概览：页数、元数据、加密状态、PDF 版本、书签数、首页文本预览",
        params={"preview_chars": "首页文本预览字符数（默认 300）"},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        reader = _reader(path)
        encrypted = bool(reader.is_encrypted)
        meta = reader.metadata or {}
        outline_count = 0
        try:
            outline_count = len(list(reader.outline))
        except Exception:
            pass
        preview = ""
        if not encrypted and len(reader.pages):
            try:
                preview = (reader.pages[0].extract_text() or "")[
                    : ctx.bounded_int("preview_chars", 300, low=20, high=10_000)
                ]
            except Exception:
                preview = ""
        return {
            "file": path.name,
            "size": path.stat().st_size,
            "pages": len(reader.pages),
            "encrypted": encrypted,
            "pdf_version": str(getattr(reader, "pdf_header", "") or ""),
            "metadata": {k.strip("/"): str(v) for k, v in dict(meta).items() if v is not None},
            "outline_entries": outline_count,
            "first_page_has_text": bool(preview.strip()),
            "preview": preview,
        }

    # ======================================================== read
    @action(
        "read",
        summary="按页结构化读取：每页文本与字符数、元数据、书签大纲、文本统计",
        params={
            "page_range": "页码区间，如 1-5 或 3 或 all（默认 all，1 基）",
            "max_chars": "文本输出上限（默认 30000）",
            "include_outline": "是否输出书签大纲（默认 true）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        reader = _reader(ctx.input_path())
        if reader.is_encrypted:
            raise ActionError(
                "PDF 已加密，无法提取文本",
                hint="先用口令解密（如 qpdf --decrypt）后再处理。",
            )
        total = len(reader.pages)
        start, end = _parse_page_range(ctx.text("page_range", "all"), total)
        max_chars = ctx.bounded_int("max_chars", 30_000, low=500, high=5_000_000)

        pages: list[dict[str, Any]] = []
        buffer: list[str] = []
        used = 0
        for index in range(start, end + 1):
            try:
                text = (reader.pages[index].extract_text() or "").strip()
            except Exception as exc:
                text = ""
                ctx.warn(f"第 {index + 1} 页文本提取失败: {type(exc).__name__}")
            pages.append(
                {
                    "page": index + 1,
                    "chars": len(text),
                    "has_text": bool(text),
                    "preview": text[:200],
                }
            )
            if used < max_chars and text:
                take = text[: max_chars - used]
                buffer.append(f"\n\n--- 第 {index + 1} 页 ---\n{take}")
                used += len(take)

        payload: dict[str, Any] = {
            "file": ctx.input_path().name,
            "pages_total": total,
            "pages_read": [start + 1, end + 1],
            "metadata": {k.strip("/"): str(v) for k, v in dict(reader.metadata or {}).items() if v is not None},
            "pages": pages,
            "text": "".join(buffer),
            "text_truncated": used >= max_chars,
            "text_stats": text_stats("".join(buffer)),
        }
        if ctx.flag("include_outline", True):
            try:
                payload["outline"] = _flatten_outline(reader.outline)
            except Exception:
                payload["outline"] = []
        return payload

    # ======================================================== analyze
    @action(
        "analyze",
        summary="质量诊断：关键词、逐页字符分布、空页/纯图页识别、加密与元数据完整性检查",
        params={"top_n": "关键词数量（默认 20）"},
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        reader = _reader(ctx.input_path())
        if reader.is_encrypted:
            raise ActionError(
                "PDF 已加密，无法分析内容",
                hint="先用口令解密（如 qpdf --decrypt）后再处理。",
            )
        top_n = ctx.bounded_int("top_n", 20, low=1, high=200)
        total = len(reader.pages)
        meta = {k.strip("/"): str(v) for k, v in dict(reader.metadata or {}).items() if v is not None}

        page_stats: list[dict[str, Any]] = []
        all_text_parts: list[str] = []
        for index, page in enumerate(reader.pages):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                text = ""
            page_stats.append({"page": index + 1, "chars": len(text), "has_text": bool(text)})
            all_text_parts.append(text)
        full_text = "\n".join(all_text_parts)

        empty_pages = [p["page"] for p in page_stats if not p["has_text"]]
        chars_list = [p["chars"] for p in page_stats]

        issues: list[str] = []
        if empty_pages:
            ratio = len(empty_pages) / max(1, total)
            if ratio > 0.5:
                issues.append(
                    f"{len(empty_pages)}/{total} 页无可提取文本（占比 {ratio:.0%}），"
                    "很可能是扫描件/纯图 PDF，本模块无法提取其文字，建议走图像 OCR 路径"
                )
            else:
                issues.append(f"{len(empty_pages)} 个空文本页: {empty_pages[:10]}")
        if not meta.get("Title"):
            issues.append("缺少文档标题元数据（/Title）")
        if not meta.get("Producer") and not meta.get("Creator"):
            issues.append("缺少 Producer/Creator 元数据，来源工具未知")
        if full_text and text_stats(full_text)["chars_no_space"] < 50:
            issues.append("全文有效字符过少，内容可能不完整")

        avg_chars = sum(chars_list) / max(1, len(chars_list))
        return {
            "file": ctx.input_path().name,
            "pages": total,
            "encrypted": False,
            "metadata": meta,
            "page_stats": page_stats,
            "distribution": {
                "avg_chars_per_page": round(avg_chars, 1),
                "max_chars_page": max(chars_list, default=0),
                "min_chars_page": min(chars_list, default=0),
            },
            "empty_pages": empty_pages,
            "keywords": top_keywords(full_text, top_n=top_n),
            "text_stats": text_stats(full_text),
            "issues": issues,
        }

    # ======================================================== transform
    @action(
        "transform",
        summary="转换导出：to_text（全文 txt）/ to_markdown（分页 md）/ to_json（逐页结构化）/ split（按页拆出新 PDF）",
        params={
            "op": "to_text | to_markdown | to_json | split",
            "page_range": "页码区间（默认 all，1 基）",
            "split_pages": "split：每份页数（默认 1）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        op = (ctx.text("op", "to_text") or "to_text").strip().lower()
        path = ctx.input_path()
        stem = slugify(path.stem)
        reader = _reader(path)
        if reader.is_encrypted:
            raise ActionError("PDF 已加密，无法转换", hint="先解密后再处理。")
        total = len(reader.pages)
        start, end = _parse_page_range(ctx.text("page_range", "all"), total)

        if op in ("to_text", "text"):
            parts: list[str] = []
            for index in range(start, end + 1):
                try:
                    text = (reader.pages[index].extract_text() or "").strip()
                except Exception:
                    text = ""
                parts.append(f"--- 第 {index + 1} 页 ---\n{text}".rstrip())
            body = "\n\n".join(parts) + "\n"
            target = ctx.out_file(f"{stem}.txt")
            target.write_text(body, encoding="utf-8")
            ctx.emit(target, "text")
            return {"op": op, "pages": [start + 1, end + 1], "output": str(target)}

        if op in ("to_markdown", "markdown", "md"):
            lines: list[str] = [f"# {meta_title}"] if (meta_title := (reader.metadata or {}).get("/Title")) else ["# PDF 导出"]
            for index in range(start, end + 1):
                try:
                    text = (reader.pages[index].extract_text() or "").strip()
                except Exception:
                    text = ""
                lines.append(f"\n## 第 {index + 1} 页\n\n{text}\n")
            target = ctx.out_file(f"{stem}.md")
            target.write_text("\n".join(lines), encoding="utf-8")
            ctx.emit(target, "markdown")
            return {"op": op, "pages": [start + 1, end + 1], "output": str(target)}

        if op in ("to_json", "json"):
            records = []
            for index in range(start, end + 1):
                try:
                    text = (reader.pages[index].extract_text() or "").strip()
                except Exception:
                    text = ""
                records.append({"page": index + 1, "chars": len(text), "text": text})
            target = ctx.out_file(f"{stem}.json")
            target.write_text(
                json.dumps(
                    {"file": path.name, "pages_total": total, "pages": records},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            ctx.emit(target, "json")
            return {"op": op, "pages": len(records), "output": str(target)}

        if op in ("split", "extract_pages"):
            pypdf = require("pypdf", _PDF_DEP["pypdf"])
            per = ctx.bounded_int("split_pages", 1, low=1, high=1000)
            indices = list(range(start, end + 1))
            chunks = [indices[i : i + per] for i in range(0, len(indices), per)]
            out_dir = ctx.out_dir("pdf_split")
            written: list[str] = []
            for number, chunk in enumerate(chunks, start=1):
                writer = pypdf.PdfWriter()
                for index in chunk:
                    writer.add_page(reader.pages[index])
                name = f"{stem}-p{chunk[0] + 1}" + (f"-p{chunk[-1] + 1}" if len(chunk) > 1 else "") + ".pdf"
                target = out_dir / name
                with target.open("wb") as fh:
                    writer.write(fh)
                written.append(name)
            ctx.emit(out_dir, "directory", f"{len(written)} 个 PDF")
            return {"op": op, "chunks": len(chunks), "output_dir": str(out_dir), "files": written}

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: to_text / to_markdown / to_json / split",
        )


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """pdf_handler 独立运行

  python handlers/pdf_handler.py probe     doc.pdf
  python handlers/pdf_handler.py read      doc.pdf -p page_range=1-5
  python handlers/pdf_handler.py analyze   doc.pdf -p top_n=30
  python handlers/pdf_handler.py transform doc.pdf -p op=to_text -o ./out
  python handlers/pdf_handler.py transform doc.pdf -p op=split -p split_pages=5
  python handlers/pdf_handler.py cap
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(PdfHandler().capabilities(), ensure_ascii=False, indent=2))
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
