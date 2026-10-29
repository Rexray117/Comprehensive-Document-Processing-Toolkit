# 扩展指南

本工具箱的设计目标是「加能力不改框架」。以下是四类常见扩展的完整步骤。

## 一、新增一个文件类型模块

以 Markdown 为例。

**第 1 步：新建 `handlers/markdown_handler.py`**

```python
"""Markdown 文档处理模块。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:      # 支持 python handlers/markdown_handler.py 直接运行
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action
from core.errors import ParamError
from core.utils import slugify, text_stats, top_keywords


class MarkdownHandler(BaseHandler):
    module = "markdown"                      # 必填：模块名，也是 --module 取值
    display_name = "Markdown 文档"
    description = "处理 .md 文件：解析 frontmatter、标题大纲、代码块、表格、链接。"
    extensions = (".md", ".markdown")        # 必填：用于扩展名路由
    kinds = ("md",)                          # 必填：sniff 产出的 kind
    version = "1.0.0"
    optional_deps = {"yaml": "PyYAML"}

    @action(
        "read",
        summary="读取 Markdown 结构与内容",
        params={"include_frontmatter": "是否解析 YAML frontmatter（默认 true）"},
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        from core.utils import read_text_smart

        text, encoding = read_text_smart(ctx.input_path())
        return {"encoding": encoding, "text": text, "stats": text_stats(text)}

    @action("analyze", summary="大纲、关键词、链接统计", params={"top_n": "关键词数量（默认 20）"})
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        from core.utils import read_text_smart

        text, _ = read_text_smart(ctx.input_path())
        return {"keywords": top_keywords(text, top_n=ctx.bounded_int("top_n", 20, low=1, high=200))}

    @action(
        "transform",
        summary="转纯文本或 HTML",
        params={"op": "to_text | to_html", "output": "输出目录"},
        produces_files=True,
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        from core.utils import read_text_smart

        text, _ = read_text_smart(ctx.input_path())
        op = ctx.text("op", "to_text")
        target = ctx.out_file(f"{slugify(ctx.input_path().stem)}.{op.split('_')[-1]}")
        target.write_text(text, encoding="utf-8")
        ctx.emit(target, "text")
        return {"op": op, "output": str(target)}

    @action("edit", summary="追加内容", params={"text": "要追加的文本"},
            modifies_input=True)
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        from core.utils import read_text_smart

        text = str(ctx.require("text"))
        target = ctx.input_path()
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n" + text)
        return {"appended_chars": len(text), "saved_to": str(target)}
```

**第 2 步（可选）：让 `sniff()` 认识这个扩展名**

若扩展名不在 `core/utils.py` 的 `EXT_TO_KIND` 里，补一行即可：

```python
EXT_TO_KIND = {
    ...
    ".md": "md", ".markdown": "md",
}
```

**第 3 步：验证**

```bash
PY="C:/Users/ASUS/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" -m core.cli list                 # 应出现 markdown 模块
"$PY" -m core.cli cap markdown         # 检查动作契约
"$PY" -m core.cli read README.md       # 实际跑一遍
```

就这样 —— **`registry` 扫描目录自动注册，无需改 dispatcher / cli / base 中任何一行。**

## 二、给已有模块加动作

```python
@action(
    "extract",                                  # 动作名（自定义动词也允许）
    summary="抽取引用文献",
    params={"style": "引用风格，apa | gb7714"},
    aliases=("refs",),                          # 别名，兼容不同调用习惯
    tags=("transform",),
)
def extract(self, ctx: ActionContext) -> dict[str, Any]:
    ...
```

要点：

- 方法签名固定为 `(self, ctx: ActionContext) -> dict | None`，返回值进 `Response.data`；
- 需要产出文件时用 `ctx.emit(path, kind)` 登记，调用方才能在 `artifacts` 里看到；
- 非致命问题用 `ctx.warn(msg)`，不要抛异常；
- 参数读取统一走 `ctx.get / flag / number / integer / text / list_of / bounded_int / require`，
  它们自带类型转换与越界校验，会自动抛 `ParamError`。

## 三、新增 OCR 后端

在 `ocr/engines.py` 中三步：

```python
# 1) 可用性探测
def _myocr_available() -> bool:
    try:
        __import__("myocr")
        return True
    except ImportError:
        return False

# 2) 识别实现：统一返回 _pack(...) 结果
def recognize_myocr(img, langs_list: list[str]) -> OCRResult:
    myocr = require("myocr", "myocr-python")
    raw = myocr.recognize(str(img))
    blocks = [
        OCRBlock(text=item["text"], confidence=float(item["score"]),
                 box=[[float(x), float(y)] for x, y in item["box"]])
        for item in raw
    ]
    return _pack("myocr", blocks, langs_list, "")

# 3) 登记到优先级链与可用性报告
_ORDER = ("myocr", "rapidocr", "tesseract", "easyocr")
```

同时更新 `available_backends()` 里的 `rows`，以及 `core/runtime.py` 的 `DEPENDENCIES`（让 `doctor` 能体检到）。

> **注意**：`recognize()` 会把预处理后的图像存成临时 PNG 再把**路径**传给各后端。
> 这是刻意的统一约定 —— RapidOCR 的输入加载层不接受 PIL 对象，走文件路径是三家后端共同最稳的路径。
> 新后端请同样接受「路径字符串」，不要依赖 PIL 对象。

## 四、新增导出格式

以给 `docx` 加 `to_rst`（reStructuredText）为例：

1. 在 `_save_target` 同级的 `transform` 方法里加一个分支：

```python
if op in ("to_rst", "rst"):
    data = self.read(ctx)
    body = self._to_rst(data)
    target = ctx.out_file(f"{stem}.rst")
    target.write_text(body, encoding="utf-8")
    ctx.emit(target, "text")
    return {"op": op, "chars": len(body), "output": str(target)}
```

2. 同步更新该动作 `params` 里 `op` 的说明字符串（它会被 `cap` 与自动生成的 API 手册读取）：

```python
"op": "to_markdown | to_text | to_rst | export_images | extract_omml | to_html | dump_xml",
```

3. 重新生成 API 手册：`python scripts/gen_api_doc.py`
4. 在 `scripts/smoke_test.py` 的 `build_cases()` 里补一个用例，保证回归覆盖。

## 五、扩展后的验收清单

- [ ] `python scripts/doctor.py` → 无新增缺失必需依赖
- [ ] `python -m core.cli list` → 新模块/新动作出现
- [ ] `python -m core.cli cap <module>` → 参数表完整可读
- [ ] `python handlers/xxx_handler.py <action> <file>` → 独立运行也能跑通
- [ ] `python scripts/smoke_test.py` → 全绿
- [ ] `python scripts/gen_api_doc.py` → 手册已同步
