"""跨 run 文本替换算法（PPTX / DOCX 共用）。

问题背景
--------
Office 文档里一段肉眼连续的文本，在 XML 里往往被切成多个 ``run``
（因为局部加粗、变色、拼写检查标记等）。直接对 ``run.text`` 逐个做
``str.replace`` 会漏掉「跨 run」的匹配：

    run1 = "风险等级为"   run2 = "高"   run3 = "，建议停飞"
    查找 "为高，"  → 三个 run 单独都匹配不上

本模块的算法：
1. 把 ``runs`` 拼成一个字符串，记录每个字符属于哪个 run；
2. 在拼接串上做正则匹配；
3. 替换文本写回「匹配起点所在的 run」，被吞掉的字符从各自 run 中移除。

效果：跨 run 能命中，且未参与匹配的 run 格式（字体/颜色/字号）完整保留。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

Match = tuple[int, int, str]


def rewrite_runs(runs: list[Any], matches: Iterable[Match]) -> int:
    """按匹配区间重写 run 文本，返回替换次数。

    Args:
        runs: 具有 ``.text`` 属性的对象序列（python-docx / python-pptx 的 Run）。
        matches: ``(start, end, replacement)`` 序列，区间不得重叠，坐标为
            拼接后整串的下标。
    """
    matches = [m for m in matches if m[1] > m[0]]
    if not matches:
        return 0

    texts = [r.text or "" for r in runs]
    joined = "".join(texts)
    if not joined:
        return 0

    owner: list[int] = []
    for idx, text in enumerate(texts):
        owner.extend([idx] * len(text))

    starts = {start: (end, repl) for start, end, repl in matches}
    chunks: list[list[str]] = [[] for _ in texts]
    i, n = 0, len(joined)
    while i < n:
        hit = starts.get(i)
        if hit is not None:
            end, repl = hit
            chunks[owner[i]].append(repl)
            i = max(end, i + 1)
        else:
            chunks[owner[i]].append(joined[i])
            i += 1

    changed = 0
    for idx, run in enumerate(runs):
        new_text = "".join(chunks[idx])
        if new_text != texts[idx]:
            run.text = new_text
            changed += 1
    return len(matches)


def find_matches(text: str, pattern: re.Pattern[str], repl: str) -> list[Match]:
    """在整串上找匹配，返回 ``(start, end, replacement)`` 列表。"""
    out: list[Match] = []
    for m in pattern.finditer(text):
        if m.end() == m.start():
            continue
        replacement = m.expand(repl) if "\\" in repl else repl
        out.append((m.start(), m.end(), replacement))
    return out


def replace_in_runs(runs: list[Any], pattern: re.Pattern[str], repl: str) -> int:
    """对一组 run 做替换（自动处理跨 run 匹配）。"""
    runs = list(runs)
    if not runs:
        return 0
    joined = "".join(r.text or "" for r in runs)
    if not joined:
        return 0
    return rewrite_runs(runs, find_matches(joined, pattern, repl))


def replace_in_paragraph(paragraph: Any, pattern: re.Pattern[str], repl: str) -> int:
    """替换一个段落内的文本（python-docx 段落）。"""
    return replace_in_runs(list(getattr(paragraph, "runs", []) or []), pattern, repl)


def replace_in_text_frame(text_frame: Any, pattern: re.Pattern[str], repl: str) -> int:
    """替换一个文本框内的全部段落（python-pptx TextFrame）。"""
    total = 0
    for para in getattr(text_frame, "paragraphs", []) or []:
        total += replace_in_runs(list(getattr(para, "runs", []) or []), pattern, repl)
    return total


def build_pattern(find: str, *, regex: bool = False, ignore_case: bool = False) -> re.Pattern[str]:
    """统一的模式构造：字面量自动转义，可选正则与大小写不敏感。"""
    source = find if regex else re.escape(find)
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(source, flags)
