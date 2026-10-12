"""HTML 网页处理模块（BeautifulSoup + lxml）。

定位
----
除了常规的正文/链接/表格提取，本模块专门支持两类高频交付场景：

1. **自包含化（``op=inline_assets``）**：把外链 CSS/JS/图片/字体全部内联，
   产出单文件 HTML —— 适合「发出去就能看」的交付物与移动端单文件分发。
2. **结构与合规审查（``analyze``）**：标题层级、图片 alt 覆盖率、meta 完整性、
   外链域名分布、内联样式配色统计，用于交付前的质量把关。

独立运行
--------
    python handlers/html_handler.py read      page.html
    python handlers/html_handler.py analyze   page.html
    python handlers/html_handler.py transform page.html -p op=inline_assets
    python handlers/html_handler.py edit      page.html -p op=set_title -p text=新标题
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ActionError, InputError, ParamError, require  # noqa: E402
from core.utils import read_text_smart, slugify, text_stats, to_json, top_keywords  # noqa: E402

MODULE = "html"

_STRIP_TAGS = ("script", "style", "noscript", "template")
_BOILERPLATE_TAGS = ("nav", "header", "footer", "aside", "form", "iframe")
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(?!data:|https?:|//)([^'\")]+)\1\s*\)", re.IGNORECASE)
_HEX_COLOR_RE = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
_PX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*px", re.IGNORECASE)


def _soup(html: bytes | str):
    bs4 = require("bs4", "beautifulsoup4")
    try:
        return bs4.BeautifulSoup(html, "lxml")
    except Exception:
        return bs4.BeautifulSoup(html, "html.parser")


def _text_of(tag: Any, separator: str = "\n") -> str:
    if tag is None:
        return ""
    for junk in tag.find_all(_STRIP_TAGS):
        junk.decompose()
    return tag.get_text(separator=separator, strip=True)


def _meta_map(soup: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property") or meta.get("http-equiv") or meta.get("itemprop")
        content = meta.get("content")
        if name and content is not None:
            out[str(name).lower()] = str(content)
    return out


def _slugify(text: str, maxlen: int = 60, fallback: str = "section") -> str:
    return slugify(text, maxlen=maxlen, fallback=fallback)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class HtmlHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "HTML 网页"
    description = (
        "处理 .html 网页：提取正文/标题层级/链接/图片/表格/结构化数据，做 SEO 与无障碍审查，"
        "转 Markdown/纯文本，把外链资源内联为单文件自包含 HTML，并支持选择器级增删改。"
    )
    extensions = (".html", ".htm", ".xhtml")
    kinds = ("html", "htm")
    optional_deps = {"bs4": "beautifulsoup4", "lxml": "lxml", "markdownify": "markdownify"}

    # ======================================================== probe
    @action(
        "probe",
        summary="快速概览：标题、meta、各类元素数量、内联资源占比",
        params={},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        raw = ctx.input_path().read_bytes()
        soup = _soup(raw)
        metas = _meta_map(soup)
        images = soup.find_all("img")
        links = soup.find_all("a", href=True)
        inline_imgs = [i for i in images if str(i.get("src", "")).startswith("data:")]
        return {
            "file": ctx.input_path().name,
            "size": len(raw),
            "encoding": getattr(soup, "original_encoding", None) or "unknown",
            "title": (soup.title.string.strip() if soup.title and soup.title.string else None),
            "lang": (soup.html.get("lang") if soup.html else None),
            "meta_keys": sorted(metas),
            "description": metas.get("description"),
            "viewport": metas.get("viewport"),
            "counts": {
                "elements": len(soup.find_all()),
                "scripts": len(soup.find_all("script")),
                "styles_inline": len(soup.find_all("style")),
                "stylesheets_linked": len([l for l in soup.find_all("link")
                                           if "stylesheet" in (l.get("rel") or [])]),
                "headings": len(soup.find_all(re.compile(r"^h[1-6]$"))),
                "images": len(images),
                "images_base64": len(inline_imgs),
                "images_missing_alt": len([i for i in images if not i.get("alt")]),
                "links": len(links),
                "tables": len(soup.find_all("table")),
                "forms": len(soup.find_all("form")),
                "lists": len(soup.find_all(["ul", "ol"])),
                "iframes": len(soup.find_all("iframe")),
                "svg": len(soup.find_all("svg")),
                "json_ld": len([s for s in soup.find_all("script", type="application/ld+json")]),
            },
            "external_links": len([a for a in links if str(a["href"]).startswith(("http://", "https://", "//"))]),
            "is_self_contained": len(inline_imgs) == len(images) and not any(
                l for l in soup.find_all("link") if "stylesheet" in (l.get("rel") or [])
            ),
        }

    # ======================================================== read
    @action(
        "read",
        summary="结构化读取：元信息、标题大纲、正文、链接、图片、表格、列表、结构化数据",
        params={
            "include_text": "是否输出正文纯文本（默认 true）",
            "main_only": "只取 article/main 主体内容（默认 false）",
            "include_links": "输出链接清单（默认 true）",
            "include_images": "输出图片清单（默认 true）",
            "include_tables": "输出表格数据（默认 true）",
            "max_text_chars": "正文最大字符数（默认 20000）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        raw = ctx.input_path().read_bytes()
        soup = _soup(raw)
        max_text = ctx.bounded_int("max_text_chars", 20000, low=200, high=5_000_000)

        body_root = soup
        if ctx.flag("main_only", False):
            body_root = (
                soup.find("article") or soup.find("main")
                or soup.find(attrs={"role": "main"}) or soup.find("body") or soup
            )

        headings: list[dict[str, Any]] = []
        for tag in body_root.find_all(re.compile(r"^h[1-6]$")):
            headings.append(
                {
                    "level": int(tag.name[1]),
                    "text": tag.get_text(strip=True),
                    "id": tag.get("id"),
                }
            )

        payload: dict[str, Any] = {
            "title": (soup.title.string.strip() if soup.title and soup.title.string else None),
            "lang": (soup.html.get("lang") if soup.html else None),
            "meta": _meta_map(soup),
            "headings": headings,
            "counts": {
                "links": len(soup.find_all("a")),
                "images": len(soup.find_all("img")),
                "tables": len(soup.find_all("table")),
                "scripts": len(soup.find_all("script")),
            },
        }

        if ctx.flag("include_text", True):
            text = _text_of(body_root)
            payload["text"] = text[:max_text]
            payload["text_truncated"] = len(text) > max_text
            payload["text_stats"] = text_stats(text)

        if ctx.flag("include_links", True):
            payload["links"] = [
                {
                    "href": str(a["href"]),
                    "text": a.get_text(strip=True)[:120],
                    "external": str(a["href"]).startswith(("http://", "https://", "//")),
                    "rel": a.get("rel"),
                    "target": a.get("target"),
                }
                for a in soup.find_all("a", href=True)
            ][:500]

        if ctx.flag("include_images", True):
            payload["images"] = [
                {
                    "src": str(img.get("src", ""))[:300],
                    "alt": img.get("alt"),
                    "width": img.get("width"),
                    "height": img.get("height"),
                    "is_base64": str(img.get("src", "")).startswith("data:"),
                    "loading": img.get("loading"),
                }
                for img in soup.find_all("img")
            ][:300]

        if ctx.flag("include_tables", True):
            tables: list[dict[str, Any]] = []
            for index, table in enumerate(soup.find_all("table"), start=1):
                rows: list[list[str]] = []
                for tr in table.find_all("tr"):
                    cells = tr.find_all(["td", "th"])
                    if cells:
                        rows.append([c.get_text(strip=True) for c in cells])
                caption = table.find("caption")
                tables.append(
                    {
                        "index": index,
                        "caption": caption.get_text(strip=True) if caption else None,
                        "rows": len(rows),
                        "cols": len(rows[0]) if rows else 0,
                        "data": rows[:200],
                    }
                )
            payload["tables"] = tables

        payload["lists"] = [
            {
                "type": ul.name,
                "items": len(ul.find_all("li", recursive=False)),
                "items_preview": [li.get_text(strip=True)[:80] for li in ul.find_all("li", recursive=False)][:10],
            }
            for ul in soup.find_all(["ul", "ol"])
        ][:50]

        payload["forms"] = [
            {
                "action": form.get("action"),
                "method": (form.get("method") or "get").upper(),
                "fields": [
                    {
                        "name": f.get("name"),
                        "type": f.get("type") or f.name,
                        "required": f.has_attr("required"),
                        "placeholder": f.get("placeholder"),
                    }
                    for f in form.find_all(["input", "select", "textarea", "button"])
                ],
            }
            for form in soup.find_all("form")
        ][:20]

        structured: list[Any] = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                structured.append(json.loads(script.string or "{}"))
            except Exception:
                structured.append({"error": "JSON-LD 解析失败"})
        if structured:
            payload["json_ld"] = structured
        return payload

    # ======================================================== analyze
    @action(
        "analyze",
        summary="质量审查：SEO、无障碍、可读性、外链分布、配色与字号统计、关键词",
        params={"top_n": "关键词数量（默认 20）", "main_only": "只分析主体内容（默认 true）"},
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        raw = ctx.input_path().read_bytes()
        soup = _soup(raw)
        top_n = ctx.bounded_int("top_n", 20, low=1, high=200)
        metas = _meta_map(soup)

        root = soup
        if ctx.flag("main_only", True):
            root = soup.find("article") or soup.find("main") or soup.find("body") or soup
        text = _text_of(root)
        stats = text_stats(text)

        headings = [{"level": int(t.name[1]), "text": t.get_text(strip=True)}
                    for t in soup.find_all(re.compile(r"^h[1-6]$"))]
        h1_count = sum(1 for h in headings if h["level"] == 1)
        jumps = [
            (headings[i - 1], headings[i])
            for i in range(1, len(headings))
            if headings[i]["level"] > headings[i - 1]["level"] + 1
        ]

        images = soup.find_all("img")
        missing_alt = [i for i in images if not i.get("alt")]
        links = soup.find_all("a", href=True)
        domains = Counter(
            urlparse(str(a["href"])).netloc
            for a in links
            if str(a["href"]).startswith(("http://", "https://", "//"))
        )

        css_text = "\n".join(s.string or "" for s in soup.find_all("style"))
        for link in soup.find_all("link"):
            if "stylesheet" in (link.get("rel") or []) and link.get("href"):
                css_text += f"\n/* external: {link.get('href')} */"
        colors = Counter(c.lower() for c in _HEX_COLOR_RE.findall(css_text))
        font_sizes = Counter(
            float(m) for m in _PX_RE.findall(css_text)
        )

        issues: list[str] = []
        if not soup.title or not (soup.title.string or "").strip():
            issues.append("缺少 <title>，影响 SEO 与浏览器标签显示")
        elif len((soup.title.string or "").strip()) > 60:
            issues.append(f"<title> 长度 {len((soup.title.string or '').strip())} 字符，建议不超过 60")
        if not metas.get("description"):
            issues.append("缺少 meta description，影响搜索结果摘要展示")
        if not soup.html or not soup.html.get("lang"):
            issues.append("html 缺少 lang 属性，影响屏幕阅读器与字体回退")
        if h1_count == 0:
            issues.append("缺少 H1 标题，页面语义结构不完整")
        elif h1_count > 1:
            issues.append(f"存在 {h1_count} 个 H1，建议全页只保留一个")
        if jumps:
            issues.append(f"存在 {len(jumps)} 处标题跳级，如 H{jumps[0][0]['level']} → H{jumps[0][1]['level']}")
        if missing_alt:
            issues.append(f"{len(missing_alt)}/{len(images)} 张图片缺少 alt 文本，不满足无障碍要求")
        if not metas.get("viewport"):
            issues.append("缺少 viewport meta，移动端将按桌面宽度缩放")
        if soup.find_all("script") and not any(s.get("defer") or s.get("async") or s.get("type") == "module"
                                               for s in soup.find_all("script") if s.get("src")):
            issues.append("外部脚本既未 defer 也未 async，可能阻塞首屏渲染")
        inline_styles = sum(len(t.get("style", "")) for t in soup.find_all(attrs={"style": True}))
        if inline_styles > 2000 and not css_text.strip():
            issues.append("大量内联样式且无 <style> 块，建议抽取为统一样式表")
        avg_sentence = (stats["chars"] / stats["sentences"]) if stats["sentences"] else 0
        if avg_sentence > 120:
            issues.append(f"平均句长 {avg_sentence:.0f} 字，偏长，可读性下降")

        return {
            "title": (soup.title.string.strip() if soup.title and soup.title.string else None),
            "title_length": len((soup.title.string or "").strip()) if soup.title else 0,
            "meta": metas,
            "headings": headings,
            "heading_stats": {
                "total": len(headings),
                "h1_count": h1_count,
                "max_depth": max((h["level"] for h in headings), default=0),
                "jump_count": len(jumps),
            },
            "text_stats": stats,
            "readability": {
                "avg_sentence_chars": round(avg_sentence, 1),
                "cjk_ratio": round(stats["cjk_chars"] / max(1, stats["chars"]), 3),
            },
            "images": {
                "total": len(images),
                "missing_alt": len(missing_alt),
                "base64": len([i for i in images if str(i.get("src", "")).startswith("data:")]),
                "lazy": len([i for i in images if i.get("loading") == "lazy"]),
            },
            "links": {
                "total": len(links),
                "external": sum(domains.values()),
                "top_domains": [{"domain": d, "count": c} for d, c in domains.most_common(15)],
                "empty_text": len([a for a in links if not a.get_text(strip=True)]),
            },
            "style_stats": {
                "colors": [{"color": c, "count": n} for c, n in colors.most_common(15)],
                "font_sizes_px": [{"px": s, "count": n} for s, n in font_sizes.most_common(15)],
                "inline_style_chars": inline_styles,
                "style_blocks": len(soup.find_all("style")),
                "external_stylesheets": len([l for l in soup.find_all("link")
                                             if "stylesheet" in (l.get("rel") or [])]),
            },
            "keywords": top_keywords(text, top_n=top_n),
            "issues": issues,
        }

    # ======================================================== transform
    @action(
        "transform",
        summary="转换与打包：Markdown / 纯文本 / 自包含单文件 / 抽取 CSS·JS / 净化 / 格式化",
        params={
            "op": "to_markdown | to_text | inline_assets | extract_css | extract_js | extract_tables | "
                  "sanitize | prettify | minify | rewrite_links",
            "base": "rewrite_links：把相对链接改为该基地址",
            "keep_scripts": "sanitize 时是否保留 <script>（默认 false）",
            "embed_base64": "inline_assets 是否把图片转为 base64 data URI（默认 true）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        op = (ctx.text("op", "to_markdown") or "to_markdown").strip().lower()
        path = ctx.input_path()
        stem = slugify(path.stem)
        raw = path.read_bytes()
        soup = _soup(raw)

        if op in ("to_markdown", "markdown", "md"):
            body = self._to_markdown(soup)
            target = ctx.out_file(f"{stem}.md")
            target.write_text(body, encoding="utf-8")
            ctx.emit(target, "markdown")
            return {"op": op, "chars": len(body), "output": str(target)}

        if op in ("to_text", "text"):
            body = _text_of(soup.find("body") or soup)
            target = ctx.out_file(f"{stem}.txt")
            target.write_text(body, encoding="utf-8")
            ctx.emit(target, "text")
            return {"op": op, "chars": len(body), "output": str(target)}

        if op in ("inline_assets", "self_contain", "standalone"):
            stats = self._inline_assets(ctx, soup, path.parent,
                                        embed_images=ctx.flag("embed_base64", True))
            target = ctx.out_file(f"{stem}.standalone.html")
            target.write_text(str(soup), encoding="utf-8")
            ctx.emit(target, "html")
            return {"op": op, "inlined": stats, "output": str(target),
                    "output_size": target.stat().st_size}

        if op in ("extract_css", "css"):
            out_dir = ctx.out_dir("css")
            written: list[str] = []
            for index, style in enumerate(soup.find_all("style"), start=1):
                css = style.string or ""
                if not css.strip():
                    continue
                target = out_dir / f"{stem}-{index}.css"
                target.write_text(css, encoding="utf-8")
                written.append(target.name)
            for index, link in enumerate([l for l in soup.find_all("link")
                                          if "stylesheet" in (l.get("rel") or [])], start=1):
                href = link.get("href")
                candidate = (path.parent / str(href).split("?")[0]).resolve() if href else None
                if candidate and candidate.exists():
                    target = out_dir / candidate.name
                    target.write_bytes(candidate.read_bytes())
                    written.append(target.name)
            if written:
                ctx.emit(out_dir, "directory", f"{len(written)} 个 CSS")
            return {"op": op, "count": len(written), "output_dir": str(out_dir), "files": written}

        if op in ("extract_js", "js"):
            out_dir = ctx.out_dir("js")
            written: list[str] = []
            for index, script in enumerate(soup.find_all("script"), start=1):
                body = script.string or ""
                src = script.get("src")
                if src:
                    candidate = (path.parent / str(src).split("?")[0]).resolve()
                    if candidate.exists():
                        target = out_dir / candidate.name
                        target.write_bytes(candidate.read_bytes())
                        written.append(target.name)
                        continue
                if body.strip():
                    target = out_dir / f"{stem}-inline-{index}.js"
                    target.write_text(body, encoding="utf-8")
                    written.append(target.name)
            if written:
                ctx.emit(out_dir, "directory", f"{len(written)} 个 JS")
            return {"op": op, "count": len(written), "output_dir": str(out_dir), "files": written}

        if op in ("extract_tables", "tables"):
            data = self.read(ActionContext(handler=self, action="read", input=path, output=ctx.output,
                                          options={"include_text": False, "include_links": False,
                                                   "include_images": False, "include_tables": True}))
            tables = data.get("tables", [])
            if not tables:
                return {"op": op, "count": 0, "note": "页面中没有表格"}
            out_dir = ctx.out_dir("tables")
            written: list[str] = []
            for table in tables:
                rows = table.get("data") or []
                if not rows:
                    continue
                target = out_dir / f"{stem}-table{table['index']}.csv"
                lines = [",".join(f'"{str(c).replace(chr(34), chr(34) * 2)}"' for c in row) for row in rows]
                target.write_text("\ufeff" + "\n".join(lines), encoding="utf-8")
                written.append(target.name)
            if written:
                ctx.emit(out_dir, "directory", f"{len(written)} 个 CSV")
            return {"op": op, "count": len(written), "output_dir": str(out_dir), "files": written}

        if op in ("sanitize", "clean"):
            removed = 0
            if not ctx.flag("keep_scripts", False):
                for tag in soup.find_all(["script", "noscript", "iframe", "object", "embed"]):
                    tag.decompose()
                    removed += 1
            for tag in soup.find_all(attrs={"onclick": True, "onload": True, "onerror": True,
                                            "onmouseover": True, "onfocus": True}):
                for attr in ("onclick", "onload", "onerror", "onmouseover", "onfocus"):
                    if tag.has_attr(attr):
                        del tag[attr]
                        removed += 1
            for tag in soup.find_all("a", href=True):
                href = str(tag["href"]).strip().lower()
                if href.startswith("javascript:"):
                    del tag["href"]
                    removed += 1
            target = ctx.out_file(f"{stem}.sanitized.html")
            target.write_text(str(soup), encoding="utf-8")
            ctx.emit(target, "html")
            return {"op": op, "removed": removed, "output": str(target)}

        if op in ("prettify", "format"):
            target = ctx.out_file(f"{stem}.pretty.html")
            target.write_text(soup.prettify(), encoding="utf-8")
            ctx.emit(target, "html")
            return {"op": op, "output": str(target)}

        if op in ("minify", "compress"):
            body = re.sub(r">\s+<", "><", str(soup))
            body = re.sub(r"<!--(?!\[if).*?-->", "", body, flags=re.DOTALL)
            body = re.sub(r"\s{2,}", " ", body).strip()
            target = ctx.out_file(f"{stem}.min.html")
            target.write_text(body, encoding="utf-8")
            ctx.emit(target, "html")
            return {"op": op, "chars_before": len(str(soup)), "chars_after": len(body),
                    "saved_ratio": round(1 - len(body) / max(1, len(str(soup))), 3)}

        if op in ("rewrite_links", "rebase"):
            base = ctx.text("base", "").strip()
            if not base:
                raise ParamError("rewrite_links 需要 base 参数，如 -p base=https://example.com/docs/")
            changed = 0
            for tag in soup.find_all(["a", "img", "script", "link", "source"]):
                attr = "src" if tag.name in ("img", "script", "source") else "href"
                value = tag.get(attr)
                if not value or str(value).startswith(("data:", "http://", "https://", "#", "mailto:", "//")):
                    continue
                tag[attr] = urljoin(base, str(value))
                changed += 1
            target = ctx.out_file(f"{stem}.rebased.html")
            target.write_text(str(soup), encoding="utf-8")
            ctx.emit(target, "html")
            return {"op": op, "changed": changed, "base": base, "output": str(target)}

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: to_markdown / to_text / inline_assets / extract_css / extract_js / "
                 "extract_tables / sanitize / prettify / minify / rewrite_links",
        )

    # ======================================================== edit
    @action(
        "edit",
        summary="修改页面：替换文本/改标题/写 meta/选择器增删改节点/注入 CSS",
        params={
            "op": "replace_text | set_title | add_meta | inject_html | remove_elements | "
                  "set_attribute | add_stylesheet | set_lang",
            "find": "replace_text：查找内容（或 CSS 选择器，配合 target=selector）",
            "replace": "replace_text：替换内容",
            "target": "replace_text：限定在某个选择器内替换",
            "selector": "remove_elements / inject_html / set_attribute：CSS 选择器",
            "text": "set_title / inject_html：文本或 HTML 片段",
            "position": "inject_html：append（默认）| prepend | before | after | body_end | head_end",
            "name": "add_meta / set_attribute：属性名",
            "content": "add_meta / set_attribute：属性值",
            "in_place": "是否就地保存（默认 true）",
        },
        modifies_input=True,
        tags=("edit",),
    )
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        raw = path.read_bytes()
        soup = _soup(raw)
        op = (ctx.text("op", "replace_text") or "replace_text").strip().lower()

        operations = {
            "replace_text": self._op_replace_text,
            "set_title": self._op_set_title,
            "add_meta": self._op_add_meta,
            "inject_html": self._op_inject,
            "remove_elements": self._op_remove,
            "set_attribute": self._op_set_attribute,
            "add_stylesheet": self._op_add_stylesheet,
            "set_lang": self._op_set_lang,
        }
        handler_fn = operations.get(op)
        if handler_fn is None:
            raise ParamError(f"不支持的 op: {op}", hint=f"可选: {', '.join(sorted(operations))}")

        changed = handler_fn(ctx, soup) or 0
        target = self._save_target(ctx, path)
        target.write_text(str(soup), encoding="utf-8")
        if target != path:
            ctx.emit(target, "html")
        return {
            "op": op,
            "changed": changed,
            "saved_to": str(target),
            "in_place": target == path,
            "backup": ctx.get("__backup__"),
        }

    # ------------------------------------------------------------------ edit 子操作
    def _op_replace_text(self, ctx: ActionContext, soup: Any) -> int:
        find = str(ctx.require("find"))
        repl = ctx.text("replace", "")
        regex = ctx.flag("regex", False)
        scope = ctx.text("target", "")
        nodes = soup.select(scope) if scope else [soup]
        if scope and not nodes:
            raise ParamError(f"选择器未匹配到任何元素: {scope}")
        changed = 0
        pattern = re.compile(find if regex else re.escape(find),
                             re.IGNORECASE if ctx.flag("ignore_case", False) else 0)
        skip_tags = {"script", "style", "noscript", "template"}
        for node in nodes:
            for text_node in list(node.find_all(string=True)):
                if text_node.parent is not None and text_node.parent.name in skip_tags:
                    continue
                if pattern.search(str(text_node)):
                    text_node.replace_with(pattern.sub(repl, str(text_node)))
                    changed += 1
        return changed

    def _op_set_title(self, ctx: ActionContext, soup: Any) -> int:
        text = str(ctx.require("text"))
        if soup.title is None:
            if soup.head is None:
                head = soup.new_tag("head")
                (soup.html or soup).insert(0, head)
            soup.head.append(soup.new_tag("title"))
        soup.title.string = text
        return 1

    def _op_add_meta(self, ctx: ActionContext, soup: Any) -> int:
        name = str(ctx.require("name"))
        content = str(ctx.require("content"))
        attr = "property" if name.startswith("og:") or name.startswith("twitter:") else "name"
        existing = soup.find("meta", attrs={attr: name})
        if existing is not None:
            existing["content"] = content
            return 1
        if soup.head is None:
            head = soup.new_tag("head")
            (soup.html or soup).insert(0, head)
        meta = soup.new_tag("meta")
        meta[attr] = name
        meta["content"] = content
        soup.head.append(meta)
        return 1

    def _op_inject(self, ctx: ActionContext, soup: Any) -> int:
        fragment = str(ctx.require("text"))
        selector = ctx.text("selector", "")
        position = ctx.text("position", "append").lower()
        block = _soup(fragment)

        if selector:
            nodes = soup.select(selector)
            if not nodes:
                raise ParamError(f"选择器未匹配到任何元素: {selector}")
            target = nodes[0]
        elif position == "body_end":
            target = soup.body or soup
        elif position == "head_end":
            target = soup.head or soup
        else:
            target = soup.body or soup

        for element in list(block.children):
            if position == "prepend":
                target.insert(0, element)
            elif position == "before":
                target.insert_before(element)
            elif position == "after":
                target.insert_after(element)
            else:
                target.append(element)
        return 1

    def _op_remove(self, ctx: ActionContext, soup: Any) -> int:
        selector = str(ctx.require("selector"))
        nodes = soup.select(selector)
        if not nodes:
            raise ParamError(f"选择器未匹配到任何元素: {selector}")
        for node in nodes:
            node.decompose()
        return len(nodes)

    def _op_set_attribute(self, ctx: ActionContext, soup: Any) -> int:
        selector = str(ctx.require("selector"))
        name = str(ctx.require("name"))
        content = ctx.get("content")
        nodes = soup.select(selector)
        if not nodes:
            raise ParamError(f"选择器未匹配到任何元素: {selector}")
        for node in nodes:
            if content is None or str(content).lower() == "none":
                node.attrs.pop(name, None)
            else:
                node[name] = str(content)
        return len(nodes)

    def _op_add_stylesheet(self, ctx: ActionContext, soup: Any) -> int:
        css = ctx.text("css", "")
        href = ctx.text("href", "")
        if not css and not href:
            raise ParamError("add_stylesheet 需要 css 或 href 参数")
        if soup.head is None:
            head = soup.new_tag("head")
            (soup.html or soup).insert(0, head)
        if css:
            style = soup.new_tag("style")
            style.string = css
            soup.head.append(style)
        else:
            link = soup.new_tag("link")
            link["rel"] = "stylesheet"
            link["href"] = href
            soup.head.append(link)
        return 1

    def _op_set_lang(self, ctx: ActionContext, soup: Any) -> int:
        lang = str(ctx.require("text"))
        if soup.html is None:
            raise ActionError("无法定位 <html> 根元素")
        soup.html["lang"] = lang
        return 1

    # ------------------------------------------------------------------ 内部辅助
    @staticmethod
    def _save_target(ctx: ActionContext, original: Path) -> Path:
        if ctx.output is not None:
            return ctx.out_file(f"{slugify(original.stem)}.html")
        if not ctx.flag("in_place", True):
            return original.with_name(f"{original.stem}_edited{original.suffix}")
        return original

    def _inline_assets(self, ctx: ActionContext, soup: Any, base_dir: Path, *,
                       embed_images: bool) -> dict[str, int]:
        """把外链资源内联，产出单文件 HTML。"""
        stats = {"images": 0, "styles": 0, "scripts": 0, "fonts": 0, "skipped": 0}
        cache: dict[Path, str] = {}

        def data_uri(file_path: Path) -> Optional[str]:
            if file_path in cache:
                return cache[file_path]
            if not file_path.exists() or not file_path.is_file():
                return None
            mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
            uri = f"data:{mime};base64,{payload}"
            cache[file_path] = uri
            return uri

        def resolve(ref: str) -> Optional[Path]:
            ref = ref.split("?")[0].split("#")[0]
            if not ref or ref.startswith(("data:", "http://", "https://", "//", "mailto:", "#", "javascript:")):
                return None
            return (base_dir / ref).resolve()

        if embed_images:
            for img in soup.find_all("img"):
                src = img.get("src")
                path = resolve(str(src)) if src else None
                if path is None:
                    stats["skipped"] += 1
                    continue
                uri = data_uri(path)
                if uri:
                    img["src"] = uri
                    stats["images"] += 1
                else:
                    stats["skipped"] += 1

        def inline_css_urls(css: str, css_dir: Path) -> tuple[str, int]:
            count = 0

            def repl(match: re.Match[str]) -> str:
                nonlocal count
                target = resolve_css_ref(match.group(2), css_dir)
                if target is None:
                    return match.group(0)
                uri = data_uri(target)
                if uri is None:
                    return match.group(0)
                count += 1
                return f"url({uri})"

            return _CSS_URL_RE.sub(repl, css), count

        def resolve_css_ref(ref: str, css_dir: Path) -> Optional[Path]:
            ref = ref.split("?")[0].split("#")[0]
            if not ref or ref.startswith(("data:", "http://", "https://", "//")):
                return None
            return (css_dir / ref).resolve()

        for style in soup.find_all("style"):
            css = style.string or ""
            if not css.strip():
                continue
            inlined, count = inline_css_urls(css, base_dir)
            style.string = inlined
            stats["fonts"] += count

        for link in list(soup.find_all("link")):
            rel = link.get("rel") or []
            href = link.get("href")
            if not href:
                continue
            path = resolve(str(href))
            if "stylesheet" in rel:
                if path is None or not path.exists():
                    stats["skipped"] += 1
                    continue
                css, count = inline_css_urls(path.read_text(encoding="utf-8", errors="replace"), path.parent)
                style = soup.new_tag("style")
                style.string = css
                link.replace_with(style)
                stats["styles"] += 1
                stats["fonts"] += count
            elif "icon" in rel:
                uri = data_uri(path) if path else None
                if uri:
                    link["href"] = uri
                    stats["images"] += 1

        for script in list(soup.find_all("script", src=True)):
            path = resolve(str(script["src"]))
            if path is None or not path.exists():
                stats["skipped"] += 1
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            new_script = soup.new_tag("script")
            for attr, value in script.attrs.items():
                if attr != "src":
                    new_script[attr] = value
            new_script.string = body
            script.replace_with(new_script)
            stats["scripts"] += 1

        if stats["skipped"]:
            ctx.warn(f"{stats['skipped']} 个资源是外链或不存在，无法内联（保持原样）")
        return stats

    @staticmethod
    def _to_markdown(soup: Any) -> str:
        try:
            from markdownify import MarkdownConverter  # noqa: PLC0415
        except ImportError:
            try:
                import html2text  # noqa: PLC0415

                converter = html2text.HTML2Text()
                converter.body_width = 0
                converter.ignore_images = False
                return converter.handle(str(soup))
            except ImportError as exc:
                raise ActionError(
                    "缺少 HTML→Markdown 依赖",
                    hint="pip install markdownify（或 html2text）",
                ) from exc

        class _Converter(MarkdownConverter):
            def convert_img(self, el, text="", parent_tags=None, *args, **kwargs):  # noqa: ANN001
                src = el.get("src", "")
                alt = el.get("alt", "") or "image"
                if str(src).startswith("data:"):
                    return f"![{alt}](<base64 内联图片>)"
                return f"![{alt}]({src})"

        return _Converter(heading_style="ATX", bullets="-", strip=["script", "style"]).convert(str(soup))


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """html_handler 独立运行

  python handlers/html_handler.py probe     page.html
  python handlers/html_handler.py read      page.html -p main_only=true
  python handlers/html_handler.py analyze   page.html -p top_n=30
  python handlers/html_handler.py transform page.html -p op=inline_assets -o ./out
  python handlers/html_handler.py edit      page.html -p op=set_title -p text=新标题
  python handlers/html_handler.py cap
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(HtmlHandler().capabilities(), ensure_ascii=False, indent=2))
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
