"""通用工具：文件类型嗅探、路径安全、文本统计、分词、编码探测。

本模块刻意不依赖任何第三方库（除可选的 filetype / charset-normalizer），
保证在「零依赖」环境下 ``list`` / ``probe`` / ``doctor`` 仍可运行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from core.errors import InputError

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 文件类型（kind）→ 处理模块（module）
KIND_TO_MODULE: dict[str, str] = {
    "pptx": "pptx",
    "docx": "docx",
    "xlsx": "xlsx",
    "html": "html",
    "htm": "html",
    "png": "image",
    "jpg": "image",
    "jpeg": "image",
    "gif": "image",
    "bmp": "image",
    "webp": "image",
    "tiff": "image",
    "tif": "image",
}

# 扩展名 → kind（魔数无法识别时的兜底）
EXT_TO_KIND: dict[str, str] = {
    ".pptx": "pptx", ".ppsx": "pptx", ".potx": "pptx",
    ".docx": "docx", ".dotx": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xltx": "xlsx",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    ".png": "png", ".jpg": "jpg", ".jpeg": "jpeg", ".gif": "gif",
    ".bmp": "bmp", ".webp": "webp", ".tif": "tiff", ".tiff": "tiff",
    ".pdf": "pdf", ".csv": "csv", ".tsv": "tsv", ".txt": "txt",
    ".md": "md", ".json": "json", ".xml": "xml", ".zip": "zip",
    ".ppt": "pptx-legacy", ".doc": "docx-legacy", ".xls": "xlsx-legacy",
}

# MIME → kind
MIME_TO_KIND: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/x-7z-compressed": "7z",
    "application/vnd.rar": "rar",
    "text/html": "html",
    "text/plain": "txt",
}

# OOXML 容器内部标志文件 → kind
_OOXML_MARKERS = (
    ("ppt/presentation.xml", "pptx"),
    ("word/document.xml", "docx"),
    ("xl/workbook.xml", "xlsx"),
)

# 图片类 kind 集合
IMAGE_KINDS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff"}


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def resolve_path(path: str | os.PathLike[str], *, must_exist: bool = False) -> Path:
    """展开 ~ 与环境变量，转绝对路径；``must_exist`` 时校验存在性。"""
    raw = os.path.expandvars(os.path.expanduser(str(path)))
    p = Path(raw)
    if not p.is_absolute():
        p = (Path.cwd() / p)
    p = p.resolve()
    if must_exist and not p.exists():
        raise InputError(
            f"输入文件不存在: {p}",
            hint="检查路径拼写；Windows 路径含空格时请用引号包裹；也可使用绝对路径。",
        )
    return p


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    """创建目录（含父目录），返回绝对路径。"""
    p = resolve_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """在目录下生成不冲突的文件路径：``name.ext`` → ``name-1.ext`` → ..."""
    directory = ensure_dir(directory)
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    idx = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{idx}{suffix}"
        idx += 1
    return candidate


def backup_file(path: str | os.PathLike[str], *, tag: str = "bak") -> Optional[Path]:
    """原文件旁生成备份，返回备份路径（文件不存在时返回 None）。"""
    src = resolve_path(path)
    if not src.exists() or not src.is_file():
        return None
    dst = src.with_name(f"{src.stem}_{tag}{src.suffix}")
    shutil.copy2(src, dst)
    return dst


def human_bytes(n: int | float) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= step
    return f"{val:.1f} TB"


def slugify(text: str, *, maxlen: int = 60, fallback: str = "untitled") -> str:
    """生成安全的文件名片段（保留中文，替换非法字符）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(text)).strip(" ._")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:maxlen]


def sha1_of(path: str | os.PathLike[str], *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 文件类型嗅探
# ---------------------------------------------------------------------------
def _ooxml_kind(path: Path) -> Optional[str]:
    """通过 OOXML 容器内部标志文件判定真实类型（比扩展名可靠）。"""
    try:
        if not zipfile.is_zipfile(path):
            return None
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    for marker, kind in _OOXML_MARKERS:
        if marker in names:
            return kind
    return None


def _magic_guess(path: Path) -> tuple[Optional[str], Optional[str]]:
    """基于文件头魔数猜测类型，返回 (kind, mime)。"""
    try:
        import filetype  # type: ignore
    except ImportError:
        return None, None
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
        guess = filetype.guess(head)
    except Exception:
        return None, None
    if guess is None:
        return None, None
    mime = getattr(guess, "mime", None)
    kind = MIME_TO_KIND.get(mime or "", None) or (guess.extension or "").lower()
    return kind or None, mime


def sniff(path: str | os.PathLike[str]) -> dict[str, Any]:
    """嗅探文件真实类型。

    判定优先级：OOXML 容器 → 魔数 → 扩展名。

    Returns:
        ``{path, name, ext, kind, mime, by, size, size_human}``
    """
    p = resolve_path(path, must_exist=True)
    if p.is_dir():
        raise InputError(f"输入是目录而不是文件: {p}", hint="请传入具体文件路径。")

    ext = p.suffix.lower()
    size = p.stat().st_size
    kind: Optional[str] = None
    mime: Optional[str] = None
    by: Optional[str] = None

    kind = _ooxml_kind(p)
    if kind:
        by = "container"
        mime = {
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }.get(kind)

    if kind is None:
        magic_kind, magic_mime = _magic_guess(p)
        if magic_kind:
            kind, mime, by = magic_kind, magic_mime, "magic"

    if kind is None:
        kind = EXT_TO_KIND.get(ext)
        by = "extension" if kind else "unknown"

    if kind == "jpeg":
        kind = "jpg"

    return {
        "path": str(p),
        "name": p.name,
        "ext": ext,
        "kind": kind or "unknown",
        "module": KIND_TO_MODULE.get(kind or "", None),
        "mime": mime,
        "detected_by": by,
        "size": size,
        "size_human": human_bytes(size),
        "is_ooxml": (by == "container"),
    }


def _is_own_output(path: Path) -> bool:
    """判断文件是否位于本技能的输出/缓存目录内。"""
    skip_names = {"_out", "_smoke", "__pycache__", ".git", "node_modules", "_preview"}
    for part in path.parts[:-1]:
        lowered = part.lower()
        if lowered in skip_names or lowered.endswith("_out"):
            return True
    return False


def iter_files(
    root: str | os.PathLike[str],
    *,
    recursive: bool = True,
    kinds: Optional[Iterable[str]] = None,
) -> list[Path]:
    """遍历目录下符合类型的文件（``kinds`` 为空表示不过滤）。"""
    base = resolve_path(root, must_exist=True)
    allowed = {k.lower() for k in kinds} if kinds else None
    if base.is_file():
        return [base]
    pattern = "**/*" if recursive else "*"
    out: list[Path] = []
    for child in sorted(base.glob(pattern)):
        if not child.is_file():
            continue
        # 跳过本技能自己产出的目录（<stem>_out/）与运行时垃圾目录：
        # 批量跑「目录 → 转换」时，第二轮递归会把上一轮的产物当成新输入，
        # 表现为 UNSUPPORTED_FORMAT 噪音与产物套娃。
        if _is_own_output(child):
            continue
        if allowed is None:
            out.append(child)
        else:
            try:
                kind = sniff(child)["kind"]
            except Exception:
                continue
            if kind in allowed:
                out.append(child)
    return out


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------
def read_text_smart(path: str | os.PathLike[str], *, max_bytes: Optional[int] = None) -> tuple[str, str]:
    """智能读取文本文件，返回 ``(text, encoding)``。

    编码探测顺序：BOM → charset-normalizer → chardet → utf-8 宽容模式。
    """
    p = resolve_path(path, must_exist=True)
    raw = p.read_bytes()
    if max_bytes:
        raw = raw[:max_bytes]

    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace"), "utf-16"

    for mod_name in ("charset_normalizer", "chardet"):
        try:
            mod = __import__(mod_name)
        except ImportError:
            continue
        if mod_name == "charset_normalizer":
            best = mod.from_bytes(raw).best()
            if best and best.encoding:
                return raw.decode(best.encoding, errors="replace"), best.encoding
        else:  # chardet
            guess = mod.detect(raw)
            if guess.get("encoding") and guess.get("confidence", 0) > 0.6:
                return raw.decode(guess["encoding"], errors="replace"), guess["encoding"]
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def decode_warning(text: str, encoding: str) -> str | None:
    """解码质量提示：``errors="replace"`` 会静默插入 U+FFFD，此处把它显式暴露出来。

    返回警告文案；文本干净时返回 ``None``。调用方应把结果交给 ``ctx.warn()``，
    避免「读出来的中文变成 �」却报告 ``ok=True / warnings=[]`` 的静默数据损坏。
    """
    bad = text.count("\ufffd")
    if bad == 0:
        return None
    return (
        f"文本解码存在 {bad} 处损坏字符（U+FFFD）：按 {encoding} 解码失败后以 replace 兜底，"
        "内容可能与原文件不一致。请用 encoding=gb18030/utf-8 显式重读，并核对关键数据。"
    )


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]*")
_CJK_SEG_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\.])\s*")

STOPWORDS = {
    # 英文
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "her", "was", "one",
    "our", "out", "day", "get", "has", "him", "his", "how", "its", "may", "new", "now",
    "old", "see", "two", "way", "who", "boy", "did", "use", "this", "that", "with",
    "have", "from", "they", "will", "would", "there", "their", "what", "about", "which",
    "when", "make", "like", "time", "just", "know", "take", "into", "your", "some",
    "could", "them", "than", "then", "only", "come", "over", "also", "back", "after",
    "use", "two", "how", "our", "work", "first", "well", "even", "want", "because",
    # 中文
    "我们", "你们", "他们", "这个", "那个", "什么", "可以", "已经", "因为", "所以",
    "但是", "如果", "进行", "通过", "以及", "并且", "或者", "而且", "以及", "对于",
    "关于", "根据", "由于", "为了", "一个", "一些", "这些", "那些", "自己", "这样",
    "那么", "以上", "以下", "其中", "包括", "需要", "必须", "应该", "能够", "不会",
    "没有", "就是", "不是", "还是", "只是", "非常", "比较", "同时", "另外", "此外",
    "目前", "现在", "主要", "相关", "相应", "其他", "其它", "每个", "各个", "各种",
    "如何", "是否", "以及", "使得", "从而", "因此", "例如", "比如", "等等", "方面",
}


def tokenize(text: str, *, min_len: int = 2) -> list[str]:
    """中英混合分词。

    英文按词切分；中日韩按「字符二元组」切分（无需外部分词器，
    对术语/关键词统计足够有效，且完全离线）。
    """
    tokens: list[str] = []
    for m in _WORD_RE.finditer(text or ""):
        word = m.group(0).lower()
        if len(word) >= min_len:
            tokens.append(word)
    for seg in _CJK_SEG_RE.finditer(text or ""):
        s = seg.group(0)
        if len(s) == 1:
            tokens.append(s)
        else:
            tokens.extend(s[i : i + 2] for i in range(len(s) - 1))
    return tokens


def top_keywords(
    text: str,
    *,
    top_n: int = 20,
    min_len: int = 2,
    extra_stopwords: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """词频 Top-N（已过滤停用词），返回 ``[{word, count}]``。"""
    from collections import Counter

    stop = set(STOPWORDS)
    if extra_stopwords:
        stop |= {str(w).lower() for w in extra_stopwords}
    counter = Counter(t for t in tokenize(text, min_len=min_len) if t not in stop)
    return [{"word": w, "count": c} for w, c in counter.most_common(max(1, top_n))]


def text_stats(text: str) -> dict[str, Any]:
    """文本统计：字符数、中文字数、英文词数、行数、段落数、句数。"""
    text = text or ""
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin_words = len(_WORD_RE.findall(text))
    lines = text.splitlines()
    paragraphs = [ln for ln in lines if ln.strip()]
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    return {
        "chars": len(text),
        "chars_no_space": len(re.sub(r"\s+", "", text)),
        "cjk_chars": cjk,
        "latin_words": latin_words,
        "lines": len(lines),
        "non_empty_lines": len(paragraphs),
        "sentences": len(sentences),
        "estimated_words": cjk + latin_words,
    }


def truncate(text: str, limit: int = 200, ellipsis: str = "…") -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + ellipsis


# ---------------------------------------------------------------------------
# 杂项
# ---------------------------------------------------------------------------
def col_index(letter: str) -> int:
    """Excel 列字母 → 0 基索引：``A`` → 0，``AA`` → 26。"""
    letter = str(letter).strip().upper()
    if not letter.isalpha():
        raise ValueError(f"非法列标识: {letter!r}")
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def col_letter(index: int) -> str:
    """0 基索引 → Excel 列字母：``0`` → A，``26`` → AA。"""
    if index < 0:
        raise ValueError("列索引不能为负")
    letters = ""
    n = int(index) + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        if out != out:  # NaN
            return None
        return out
    except (TypeError, ValueError):
        return None


def jdefault(obj: Any):
    """JSON 回退序列化（供各 handler 复用）。"""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return f"<bytes:{len(obj)}>"
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)


def to_json(data: Any, *, indent: int = 2) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False, default=jdefault)


def write_json(data: Any, path: str | os.PathLike[str], *, indent: int = 2) -> Path:
    target = resolve_path(path)
    ensure_dir(target.parent)
    target.write_text(to_json(data, indent=indent), encoding="utf-8")
    return target


def python_env() -> dict[str, Any]:
    """当前解释器信息，供 doctor / 错误提示使用。"""
    exe = Path(sys.executable)
    return {
        "executable": str(exe),
        "version": sys.version.split()[0],
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "prefix": sys.prefix,
    }
