"""纯文本文件处理模块（.txt，纯标准库实现）。

定位
----
轻量的文本文件读写与处理：编码智能探测（BOM → charset-normalizer → utf-8 宽容）、
行尾格式诊断（CRLF/LF/混合）、行级统计与关键词分析、行/段落与 JSON 互转、
就地查找替换与行级清理。无任何第三方必需依赖。

独立运行
--------
    python handlers/txt_handler.py probe     notes.txt
    python handlers/txt_handler.py read      notes.txt -p max_chars=5000
    python handlers/txt_handler.py analyze   notes.txt -p top_n=30
    python handlers/txt_handler.py transform notes.txt -p op=to_json
    python handlers/txt_handler.py edit      notes.txt -p op=replace -p find=旧 -p replace=新
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ParamError  # noqa: E402
from core.utils import (  # noqa: E402
    read_text_smart,
    slugify,
    text_stats,
    top_keywords,
)

MODULE = "txt"

_CRLF = "\r\n"


def _line_ending_info(text: str) -> dict[str, Any]:
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    cr_only = text.count("\r") - crlf
    if crlf and (lf_only or cr_only):
        style = "mixed"
    elif crlf:
        style = "crlf"
    elif lf_only:
        style = "lf"
    elif cr_only:
        style = "cr"
    else:
        style = "none"
    return {"crlf": crlf, "lf": lf_only, "cr": cr_only, "style": style}


def _looks_like_mojibake(text: str) -> int:
    """疑似乱码字符计数：替换符 U+FFFD 与罕见的私用区字符。"""
    return sum(1 for ch in text if ch == "\ufffd" or 0xE000 <= ord(ch) <= 0xF8FF)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class TxtHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "纯文本文件"
    description = (
        "处理 .txt 纯文本：编码智能探测、行尾诊断、统计与关键词分析，"
        "转 JSON/JSONL/Markdown，支持就地查找替换、行过滤与格式清理（零第三方依赖）。"
    )
    extensions = (".txt",)
    kinds = ("txt",)
    optional_deps: dict[str, str] = {}

    # ======================================================== probe
    @action(
        "probe",
        summary="快速概览：编码、行尾风格、行数、空行占比、最长行、开头预览",
        params={"preview_chars": "开头预览字符数（默认 300）"},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        raw = path.read_bytes()
        text, encoding = read_text_smart(path, max_bytes=2_000_000)
        endings = _line_ending_info(text)
        lines = text.splitlines()
        non_empty = [ln for ln in lines if ln.strip()]
        longest = max((len(ln) for ln in lines), default=0)
        bom = "yes" if raw[:3] == b"\xef\xbb\xbf" else ("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "no")
        return {
            "file": path.name,
            "size": len(raw),
            "encoding": encoding,
            "has_bom": bom != "no",
            "bom_type": bom,
            "line_endings": endings,
            "lines": len(lines),
            "non_empty_lines": len(non_empty),
            "empty_line_ratio": round(1 - len(non_empty) / max(1, len(lines)), 3) if lines else 0.0,
            "longest_line": longest,
            "suspect_mojibake_chars": _looks_like_mojibake(text),
            "preview": text[: ctx.bounded_int("preview_chars", 300, low=20, high=10_000)],
        }

    # ======================================================== read
    @action(
        "read",
        summary="结构化读取：全文或带行号片段、文本统计、行尾与编码信息",
        params={
            "max_chars": "输出正文最大字符数（默认 20000，full=true 时忽略）",
            "full": "是否返回全文不截断（默认 false）",
            "line_numbers": "是否输出带行号的行列表（默认 false）",
            "offset": "起始行号（0 基，默认 0）",
            "limit": "最多返回的行数（默认 500）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        text, encoding = read_text_smart(ctx.input_path())
        lines = text.splitlines()
        offset = max(0, ctx.integer("offset", 0) or 0)
        limit = ctx.bounded_int("limit", 500, low=1, high=100_000)
        max_chars = ctx.bounded_int("max_chars", 20_000, low=200, high=5_000_000)
        full = ctx.flag("full", False)

        payload: dict[str, Any] = {
            "file": ctx.input_path().name,
            "encoding": encoding,
            "line_endings": _line_ending_info(text),
            "text_stats": text_stats(text),
        }
        if ctx.flag("line_numbers", False):
            payload["lines"] = [
                {"no": i + 1, "text": ln} for i, ln in enumerate(lines[offset : offset + limit])
            ]
            payload["total_lines"] = len(lines)
        body = text if full else text[:max_chars]
        payload["text"] = body
        payload["text_truncated"] = (not full) and len(text) > max_chars
        return payload

    # ======================================================== analyze
    @action(
        "analyze",
        summary="内容分析：词频关键词、行级分布、重复行 Top、疑似乱码与格式问题诊断",
        params={"top_n": "关键词数量（默认 20）", "duplicate_min": "重复行计入的最小长度（默认 4）"},
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        text, encoding = read_text_smart(ctx.input_path())
        top_n = ctx.bounded_int("top_n", 20, low=1, high=200)
        dup_min = ctx.bounded_int("duplicate_min", 4, low=0, high=1000)
        lines = text.splitlines()
        non_empty = [ln for ln in lines if ln.strip()]
        endings = _line_ending_info(text)
        mojibake = _looks_like_mojibake(text)

        stripped_counter = Counter(
            ln.strip() for ln in non_empty if len(ln.strip()) >= dup_min
        )
        duplicates = [
            {"line": ln, "count": c} for ln, c in stripped_counter.most_common(20) if c > 1
        ]
        length_hist = Counter(len(ln) // 20 * 20 for ln in non_empty)

        issues: list[str] = []
        if mojibake:
            issues.append(f"检测到 {mojibake} 个疑似乱码字符（U+FFFD 或私用区），编码可能不匹配")
        if endings["style"] == "mixed":
            issues.append(
                f"行尾风格混合（CRLF {endings['crlf']} / LF {endings['lf']} / CR {endings['cr']}），建议 normalize"
            )
        if not non_empty:
            issues.append("文件没有非空行内容")
        trailing_ws = sum(1 for ln in non_empty if ln != ln.rstrip())
        if trailing_ws:
            issues.append(f"{trailing_ws} 行带有行尾空白字符")
        no_final_newline = bool(text) and not text.endswith("\n")
        if no_final_newline:
            issues.append("文件末尾缺少换行符（POSIX 规范建议有）")

        return {
            "file": ctx.input_path().name,
            "encoding": encoding,
            "text_stats": text_stats(text),
            "line_endings": endings,
            "keywords": top_keywords(text, top_n=top_n),
            "duplicates": duplicates,
            "length_histogram": [
                {"bucket": f"{k}-{k + 19}", "count": v} for k, v in sorted(length_hist.items())
            ],
            "issues": issues,
        }

    # ======================================================== transform
    @action(
        "transform",
        summary="转换导出：to_json（行数组）/ to_jsonl / to_markdown（段落化）/ normalize（规范行尾与空白）",
        params={
            "op": "to_json | to_jsonl | to_markdown | normalize",
            "encoding": "输出编码（默认 utf-8）",
            "crlf": "normalize 输出行尾：true=CRLF（Windows），false=LF（Unix，默认）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        op = (ctx.text("op", "to_json") or "to_json").strip().lower()
        path = ctx.input_path()
        stem = slugify(path.stem)
        text, encoding = read_text_smart(path)
        out_encoding = ctx.text("encoding", "utf-8")

        if op in ("to_json", "json"):
            lines = text.splitlines()
            target = ctx.out_file(f"{stem}.json")
            target.write_text(
                json.dumps(lines, ensure_ascii=False, indent=2), encoding=out_encoding
            )
            ctx.emit(target, "json")
            return {"op": op, "lines": len(lines), "source_encoding": encoding, "output": str(target)}

        if op in ("to_jsonl", "jsonl"):
            target = ctx.out_file(f"{stem}.jsonl")
            with target.open("w", encoding=out_encoding) as fh:
                for ln in text.splitlines():
                    fh.write(json.dumps({"line": ln}, ensure_ascii=False) + "\n")
            ctx.emit(target, "jsonl")
            return {"op": op, "output": str(target)}

        if op in ("to_markdown", "markdown", "md"):
            paragraphs = [ln.strip() for ln in text.split("\n\n") if ln.strip()]
            body = "\n\n".join(
                p if p.startswith(("#", "-", "*", ">", "|", "```")) else p.replace("\n", "  \n")
                for p in paragraphs
            )
            target = ctx.out_file(f"{stem}.md")
            target.write_text(body, encoding=out_encoding)
            ctx.emit(target, "markdown")
            return {"op": op, "paragraphs": len(paragraphs), "output": str(target)}

        if op in ("normalize", "clean"):
            eol = _CRLF if ctx.flag("crlf", False) else "\n"
            normalized = eol.join(line.rstrip() for line in text.splitlines())
            if normalized:
                normalized += eol
            target = ctx.out_file(f"{stem}.normalized.txt")
            target.write_text(normalized, encoding=out_encoding)
            ctx.emit(target, "text")
            return {
                "op": op,
                "eol": "crlf" if eol == _CRLF else "lf",
                "output": str(target),
            }

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: to_json / to_jsonl / to_markdown / normalize",
        )

    # ======================================================== edit
    @action(
        "edit",
        summary="修改文本：replace（查找替换）/ append / prepend / delete_lines（按模式删行）/ normalize（就地清理）",
        params={
            "op": "replace | append | prepend | delete_lines | normalize",
            "find": "replace / delete_lines：查找内容或正则",
            "replace": "replace：替换内容（默认空串即删除）",
            "regex": "find 是否按正则解释（默认 false）",
            "ignore_case": "是否忽略大小写（默认 false）",
            "text": "append / prepend：追加或前置的文本",
            "in_place": "是否就地保存（默认 true）",
        },
        modifies_input=True,
        tags=("edit",),
    )
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        text, encoding = read_text_smart(path)
        op = (ctx.text("op", "replace") or "replace").strip().lower()

        if op == "replace":
            find = str(ctx.require("find"))
            repl = ctx.text("replace", "")
            pattern = re.compile(
                find if ctx.flag("regex", False) else re.escape(find),
                re.IGNORECASE if ctx.flag("ignore_case", False) else 0,
            )
            new_text, count = pattern.subn(repl, text)
        elif op in ("append", "prepend"):
            extra = str(ctx.require("text"))
            if not extra.endswith("\n"):
                extra += "\n"
            new_text, count = (text + extra) if op == "append" else (extra + text), 1
        elif op == "delete_lines":
            find = str(ctx.require("find"))
            pattern = re.compile(
                find if ctx.flag("regex", False) else re.escape(find),
                re.IGNORECASE if ctx.flag("ignore_case", False) else 0,
            )
            kept = [ln for ln in text.splitlines() if not pattern.search(ln)]
            count = len(text.splitlines()) - len(kept)
            new_text = "\n".join(kept) + ("\n" if kept else "")
        elif op == "normalize":
            eol = _CRLF if ctx.flag("crlf", False) else "\n"
            new_text = eol.join(line.rstrip() for line in text.splitlines())
            if new_text:
                new_text += eol
            count = len(text.splitlines())
        else:
            raise ParamError(f"不支持的 op: {op}", hint="可选: replace / append / prepend / delete_lines / normalize")

        target = self._save_target(ctx, path)
        target.write_text(new_text, encoding=encoding if encoding.endswith("(replace)") is False else "utf-8")
        if target != path:
            ctx.emit(target, "text")
        return {
            "op": op,
            "changed": count,
            "saved_to": str(target),
            "in_place": target == path,
            "backup": ctx.get("__backup__"),
        }

    # ------------------------------------------------------------------ 内部辅助
    @staticmethod
    def _save_target(ctx: ActionContext, original: Path) -> Path:
        if ctx.output is not None:
            return ctx.out_file(f"{slugify(original.stem)}.txt")
        if not ctx.flag("in_place", True):
            return original.with_name(f"{original.stem}_edited{original.suffix}")
        return original


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """txt_handler 独立运行

  python handlers/txt_handler.py probe     notes.txt
  python handlers/txt_handler.py read      notes.txt -p line_numbers=true -p limit=50
  python handlers/txt_handler.py analyze   notes.txt -p top_n=30
  python handlers/txt_handler.py transform notes.txt -p op=to_json -o ./out
  python handlers/txt_handler.py edit      notes.txt -p op=replace -p find=旧词 -p replace=新词
  python handlers/txt_handler.py cap
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(TxtHandler().capabilities(), ensure_ascii=False, indent=2))
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
