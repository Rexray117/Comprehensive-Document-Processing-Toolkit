# API 参考手册

> **本文件由 `scripts/gen_api_doc.py` 自动生成，请勿手工编辑。**
> 生成命令：`python scripts/gen_api_doc.py`
> 契约来源：各 handler 的 `@action(...)` 声明，与运行时 `cap <module>` 输出完全一致。

## 调用约定

### 三种入口，同一套契约

| 入口 | 形态 |
| --- | --- |
| Python | `from core.dispatcher import run` → `run({...})` |
| 命令行 | `python -m core.cli run -m <module> -a <action> -i <file> [-o <out>] [-p k=v ...]` |
| JSON 请求体 | `python -m core.cli ask '{"module":...,"action":...,"input":...,"options":{...}}'` |

### 请求字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `module` | 否 | 模块名；`auto`（默认）按文件真实类型自动路由 |
| `action` | 是 | 动作名，见下方各模块 |
| `input` | 是（除标注 `needs_input=false` 的动作） | 输入文件路径 |
| `output` | 否 | 目录或文件路径；省略时编辑类动作就地保存、导出类动作写入 `<名称>_out/` |
| `options` | 否 | 动作参数，键值与 `-p k=v` 等价 |

### 响应信封

```json
{
  "ok": true,
  "module": "pptx",
  "action": "read",
  "input": "/abs/path/deck.pptx",
  "output": null,
  "elapsed_ms": 18,
  "warnings": ["非致命问题提示"],
  "artifacts": [{"path": "/abs/out/deck.md", "kind": "markdown", "bytes": 4213, "note": "4.1 KB"}],
  "data": { },
  "error": null
}
```

失败时：`ok=false`、`data={}`、`error={"code","message","hint","details"}`。
错误码：`DEPENDENCY_MISSING` / `INPUT_INVALID` / `UNSUPPORTED_FORMAT` / `ACTION_FAILED` / `PARAM_INVALID` / `INTERNAL_ERROR`。

### 通用约定

- **跨模块统一动词**：`probe`（探测）/ `read`（读取）/ `analyze`（分析）/ `transform`（转换导出）/ `edit`（修改）。
- **`edit` 默认就地保存**，并在原文件旁生成 `<名称>_bak.<后缀>` 备份；`in_place=false` 则输出 `<名称>_edited.<后缀>`。
- **`transform` 默认输出到 `<输入同名>_out/` 目录**，避免污染源文件；用 `-o` 指定位置。
- 布尔参数用 `true` / `false`；`-p` 的值会自动尝试 JSON 解析，因此 `-p bullets=甲,乙` 会被 handler 按逗号拆成列表。

---


## 模块 `docx` —— DOCX 文档

**版本**：0.1.0  
**支持类型**：.docx, .dotx  
**可选依赖**：`mammoth` → mammoth, `lxml` → lxml  

处理 .docx 文档：按真实正文顺序读取段落/表格/图片/页眉页脚，导出 OMML 公式原始 XML，转 Markdown、跨 run 查找替换、插入表格与可编辑公式、批量清理空段。

### `probe`

ZIP 层快速探测：段落/表格/图片/公式/批注的数量与文档体量

| 参数 | 说明 |
| --- | --- |
| `deep` | true 时用 python-docx 补充节与样式信息（默认 false） |

### `read`

按正文真实顺序读取段落/表格/图片/公式/页眉页脚

| 参数 | 说明 |
| --- | --- |
| `include_tables` | 是否输出表格数据（默认 true） |
| `include_headers` | 是否输出页眉页脚（默认 true） |
| `include_formulas` | 是否输出 OMML 原始 XML（默认 true） |
| `include_images` | 是否输出图片清单（默认 true） |
| `include_empty` | 是否保留空段落（默认 false） |
| `max_formula_xml` | 单个公式 XML 最大字符数（默认 4000，超出截断） |

### `analyze`

文档分析：大纲层级、字数统计、关键词、样式分布、公式/图片、结构问题诊断

| 参数 | 说明 |
| --- | --- |
| `top_n` | 关键词数量（默认 20） |
| `max_heading_level` | 视为过深的标题层级（默认 4） |

### `transform`  _（产出文件）_

导出 Markdown / 纯文本 / 图片 / OMML 公式 / HTML / 原始 XML

| 参数 | 说明 |
| --- | --- |
| `op` | to_markdown | to_text | export_images | extract_omml | to_html | dump_xml |
| `formula_as` | to_markdown 时公式写法：text（默认，纯文本）或 omml（嵌入原始 XML 注释） |

### `edit`  _（会修改输入文件（自动备份））_

修改文档：查找替换（保格式）/追加段落标题/插表格/插入 OMML 公式/清理空段/写属性

| 参数 | 说明 |
| --- | --- |
| `op` | replace_text | add_paragraph | append_heading | insert_table | insert_formula | append_markdown | remove_empty_paragraphs | set_core_properties |
| `find` | replace_text：查找内容 |
| `replace` | replace_text：替换内容 |
| `regex` | replace_text：正则模式（默认 false） |
| `text` | add_paragraph / append_heading：文本内容 |
| `level` | append_heading：标题层级（默认 1） |
| `style` | add_paragraph：段落样式名 |
| `rows` | insert_table：二维数组或 CSV 行文本 |
| `formula` | insert_formula：OMML 片段（含 <m:oMath>） |
| `in_place` | 是否就地保存（默认 true） |

---

## 模块 `html` —— HTML 网页

**版本**：0.1.0  
**支持类型**：.html, .htm, .xhtml  
**可选依赖**：`bs4` → beautifulsoup4, `lxml` → lxml, `markdownify` → markdownify  

处理 .html 网页：提取正文/标题层级/链接/图片/表格/结构化数据，做 SEO 与无障碍审查，转 Markdown/纯文本，把外链资源内联为单文件自包含 HTML，并支持选择器级增删改。

### `probe`

快速概览：标题、meta、各类元素数量、内联资源占比

_无参数。_

### `read`

结构化读取：元信息、标题大纲、正文、链接、图片、表格、列表、结构化数据

| 参数 | 说明 |
| --- | --- |
| `include_text` | 是否输出正文纯文本（默认 true） |
| `main_only` | 只取 article/main 主体内容（默认 false） |
| `include_links` | 输出链接清单（默认 true） |
| `include_images` | 输出图片清单（默认 true） |
| `include_tables` | 输出表格数据（默认 true） |
| `max_text_chars` | 正文最大字符数（默认 20000） |

### `analyze`

质量审查：SEO、无障碍、可读性、外链分布、配色与字号统计、关键词

| 参数 | 说明 |
| --- | --- |
| `top_n` | 关键词数量（默认 20） |
| `main_only` | 只分析主体内容（默认 true） |

### `transform`  _（产出文件）_

转换与打包：Markdown / 纯文本 / 自包含单文件 / 抽取 CSS·JS / 净化 / 格式化

| 参数 | 说明 |
| --- | --- |
| `op` | to_markdown | to_text | inline_assets | extract_css | extract_js | extract_tables | sanitize | prettify | minify | rewrite_links |
| `base` | rewrite_links：把相对链接改为该基地址 |
| `keep_scripts` | sanitize 时是否保留 <script>（默认 false） |
| `embed_base64` | inline_assets 是否把图片转为 base64 data URI（默认 true） |

### `edit`  _（会修改输入文件（自动备份））_

修改页面：替换文本/改标题/写 meta/选择器增删改节点/注入 CSS

| 参数 | 说明 |
| --- | --- |
| `op` | replace_text | set_title | add_meta | inject_html | remove_elements | set_attribute | add_stylesheet | set_lang |
| `find` | replace_text：查找内容（或 CSS 选择器，配合 target=selector） |
| `replace` | replace_text：替换内容 |
| `target` | replace_text：限定在某个选择器内替换 |
| `selector` | remove_elements / inject_html / set_attribute：CSS 选择器 |
| `text` | set_title / inject_html：文本或 HTML 片段 |
| `position` | inject_html：append（默认）| prepend | before | after | body_end | head_end |
| `name` | add_meta / set_attribute：属性名 |
| `content` | add_meta / set_attribute：属性值 |
| `in_place` | 是否就地保存（默认 true） |

---

## 模块 `image` —— 图片（PNG/JPG 等）

**版本**：0.1.0  
**支持类型**：.png, .jpg, .jpeg, .gif, .bmp, .webp, .tif, .tiff  
**可选依赖**：`PIL` → Pillow, `rapidocr_onnxruntime` → rapidocr-onnxruntime  

处理位图图片：读取元数据与 EXIF、OCR 识别图中文字、评估清晰度与配色、启发式判别图片类型并生成多模态视觉分析载荷，支持转换/压缩/裁剪/加水印/打码。

### `probe`

快速探测：格式、尺寸、模式、DPI、透明度、动画、体积

| 参数 | 说明 |
| --- | --- |
| `exif` | 是否附带 EXIF 概要（默认 false） |

### `read`

读取元数据：EXIF（含 GPS）、ICC、调色板、质量指标

| 参数 | 说明 |
| --- | --- |
| `include_exif` | 是否输出完整 EXIF（默认 true） |
| `include_palette` | 是否输出主色板（默认 true） |
| `palette_size` | 主色数量（默认 8） |
| `include_metrics` | 是否输出质量指标（默认 true） |

### `analyze`

内容识别：OCR 文字 + 版面还原 + 类型判别 + 视觉桥接载荷

| 参数 | 说明 |
| --- | --- |
| `ocr` | 是否做 OCR（默认 true；设为 false 则只做统计与判别） |
| `ocr_langs` | OCR 语言，默认 chi_sim+eng |
| `ocr_backend` | auto / rapidocr / tesseract / easyocr |
| `ocr_min_confidence` | 过滤低于该置信度的文本块（默认 0.4） |
| `binarize` | OCR 前是否二值化（默认 false，扫描件可开启） |
| `vision_payload` | 是否输出多模态视觉分析载荷（默认 false，会显著增大响应体） |
| `palette_size` | 主色数量（默认 8） |

### `transform`  _（产出文件）_

转换与导出：格式转换、缩放、裁剪、旋转、压缩、水印、转 PDF、base64

| 参数 | 说明 |
| --- | --- |
| `op` | convert | resize | thumbnail | crop | rotate | flip | grayscale | compress | watermark | to_pdf | to_base64 | auto_orient | strip_exif | sprite |
| `format` | convert：目标格式 png/jpg/webp/bmp/tiff |
| `width` | resize：目标宽度（像素） |
| `height` | resize：目标高度（像素） |
| `percent` | resize/thumbnail：按百分比缩放 |
| `keep_aspect` | resize：是否保持比例（默认 true） |
| `box` | crop/redact：left,top,right,bottom |
| `angle` | rotate：旋转角度（度，逆时针） |
| `quality` | compress：JPEG 质量 1-100（默认 80） |
| `text` | watermark：水印文字 |
| `max_side` | thumbnail：最长边像素（默认 512） |

### `edit`  _（会修改输入文件（自动备份））_

就地编辑：打码遮盖、模糊/马赛克区域、文字标注、画框、加边框、拼网格

| 参数 | 说明 |
| --- | --- |
| `op` | redact | blur_region | pixelate_region | annotate_text | draw_box | add_border | draw_grid | watermark |
| `box` | 目标区域 left,top,right,bottom（redact/blur/pixelate/draw_box） |
| `text` | annotate_text：标注文字；watermark：水印文字 |
| `color` | draw_box / annotate_text：颜色，如 #FF0000 或 red |
| `width` | draw_box / add_border：线宽（默认 3） |
| `radius` | blur_region：模糊半径（默认 12） |
| `block` | pixelate_region：马赛克块大小（默认 12） |
| `opacity` | redact：遮盖不透明度（默认 100） |

---

## 模块 `pdf` —— PDF 文档

**版本**：0.1.0  
**支持类型**：.pdf  
**可选依赖**：`pypdf` → pypdf  

读取 .pdf：元数据与加密探测、按页文本提取（支持页码区间）、书签大纲、关键词与空页诊断，导出文本/Markdown/JSON、按页拆分。只读模块，不做写编辑。

### `probe`

快速概览：页数、元数据、加密状态、PDF 版本、书签数、首页文本预览

| 参数 | 说明 |
| --- | --- |
| `preview_chars` | 首页文本预览字符数（默认 300） |

### `read`

按页结构化读取：每页文本与字符数、元数据、书签大纲、文本统计

| 参数 | 说明 |
| --- | --- |
| `page_range` | 页码区间，如 1-5 或 3 或 all（默认 all，1 基） |
| `max_chars` | 文本输出上限（默认 30000） |
| `include_outline` | 是否输出书签大纲（默认 true） |

### `analyze`

质量诊断：关键词、逐页字符分布、空页/纯图页识别、加密与元数据完整性检查

| 参数 | 说明 |
| --- | --- |
| `top_n` | 关键词数量（默认 20） |

### `transform`  _（产出文件）_

转换导出：to_text（全文 txt）/ to_markdown（分页 md）/ to_json（逐页结构化）/ split（按页拆出新 PDF）

| 参数 | 说明 |
| --- | --- |
| `op` | to_text | to_markdown | to_json | split |
| `page_range` | 页码区间（默认 all，1 基） |
| `split_pages` | split：每份页数（默认 1） |

---

## 模块 `pptx` —— PPTX 演示文稿

**版本**：0.1.0  
**支持类型**：.pptx, .ppsx, .potx  
**可选依赖**：`pptx` → python-pptx, `fitz` → pymupdf  

处理 .pptx 演示文稿：读取幻灯片文本/表格/图表/备注/图片、统计大纲与关键词、导出 Markdown 或图片、跨 run 查找替换保格式、增删与复制幻灯片。

### `probe`

ZIP 层快速探测：页数、媒体、备注、尺寸，不依赖 python-pptx

| 参数 | 说明 |
| --- | --- |
| `deep` | true 时额外用 python-pptx 解析版式与占位符（默认 false） |

### `read`

结构化读取全部幻灯片（标题/形状/表格/图表/备注/图片）

| 参数 | 说明 |
| --- | --- |
| `include_notes` | 是否读备注页（默认 true） |
| `include_tables` | 是否读表格内容（默认 true） |
| `include_charts` | 是否读图表数据（默认 true） |
| `include_empty` | 是否保留空文本框（默认 false） |
| `slide_range` | 指定页码，如 '1-5' 或 '2,4,7'（默认全部） |
| `include_runs` | 是否细化到 run 级字体信息（默认 false） |

### `analyze`

内容识别：大纲、字数、关键词、字体/配色分布、空页检测、图片 OCR

| 参数 | 说明 |
| --- | --- |
| `top_n` | 关键词数量（默认 20） |
| `ocr` | 是否对幻灯片内图片做 OCR（默认 false） |
| `ocr_langs` | OCR 语言，默认 chi_sim+eng |
| `ocr_backend` | auto / rapidocr / tesseract / easyocr |
| `min_chars_per_slide` | 低于该字数视为稀疏页（默认 10） |

### `transform`  _（产出文件）_

导出为 Markdown / 纯文本 / 大纲 JSON / 图片 / 媒体 / PDF

| 参数 | 说明 |
| --- | --- |
| `op` | to_markdown | to_text | to_outline_json | export_images | export_media | render |
| `render_format` | render 时的目标格式：pdf（默认）或 png |
| `layout_placeholder` | 是否导出版式占位提示（to_markdown 时，默认 false） |

### `edit`  _（会修改输入文件（自动备份））_

修改演示文稿：查找替换（保格式）/改标题/增删复制页/加文本框/写属性

| 参数 | 说明 |
| --- | --- |
| `op` | replace_text | set_title | set_notes | add_slide | delete_slide | duplicate_slide | append_bullets | add_textbox | move_slide | set_core_properties |
| `find` | replace_text：查找内容 |
| `replace` | replace_text：替换内容 |
| `regex` | replace_text：是否按正则（默认 false） |
| `slide` | 目标页码（1 基） |
| `text` | set_title / set_notes：新文本 |
| `title` | add_slide：标题 |
| `bullets` | add_slide / append_bullets：要点列表 |
| `layout_index` | add_slide：版式索引（默认 1） |
| `in_place` | 是否就地保存（默认 true；false 则另存为 <名称>_edited.pptx） |

---

## 模块 `txt` —— 纯文本文件

**版本**：0.1.0  
**支持类型**：.txt  

处理 .txt 纯文本：编码智能探测、行尾诊断、统计与关键词分析，转 JSON/JSONL/Markdown，支持就地查找替换、行过滤与格式清理（零第三方依赖）。

### `probe`

快速概览：编码、行尾风格、行数、空行占比、最长行、开头预览

| 参数 | 说明 |
| --- | --- |
| `preview_chars` | 开头预览字符数（默认 300） |

### `read`

结构化读取：全文或带行号片段、文本统计、行尾与编码信息

| 参数 | 说明 |
| --- | --- |
| `max_chars` | 输出正文最大字符数（默认 20000，full=true 时忽略） |
| `full` | 是否返回全文不截断（默认 false） |
| `line_numbers` | 是否输出带行号的行列表（默认 false） |
| `offset` | 起始行号（0 基，默认 0） |
| `limit` | 最多返回的行数（默认 500） |

### `analyze`

内容分析：词频关键词、行级分布、重复行 Top、疑似乱码与格式问题诊断

| 参数 | 说明 |
| --- | --- |
| `top_n` | 关键词数量（默认 20） |
| `duplicate_min` | 重复行计入的最小长度（默认 4） |

### `transform`  _（产出文件）_

转换导出：to_json（行数组）/ to_jsonl / to_markdown（段落化）/ normalize（规范行尾与空白）

| 参数 | 说明 |
| --- | --- |
| `op` | to_json | to_jsonl | to_markdown | normalize |
| `encoding` | 输出编码（默认 utf-8） |
| `crlf` | normalize 输出行尾：true=CRLF（Windows），false=LF（Unix，默认） |

### `edit`  _（会修改输入文件（自动备份））_

修改文本：replace（查找替换）/ append / prepend / delete_lines（按模式删行）/ normalize（就地清理）

| 参数 | 说明 |
| --- | --- |
| `op` | replace | append | prepend | delete_lines | normalize |
| `find` | replace / delete_lines：查找内容或正则 |
| `replace` | replace：替换内容（默认空串即删除） |
| `regex` | find 是否按正则解释（默认 false） |
| `ignore_case` | 是否忽略大小写（默认 false） |
| `text` | append / prepend：追加或前置的文本 |
| `in_place` | 是否就地保存（默认 true） |

---

## 模块 `xlsx` —— XLSX 工作表

**版本**：0.1.0  
**支持类型**：.xlsx, .xlsm, .xltx  
**可选依赖**：`pandas` → pandas, `xlsxwriter` → XlsxWriter  

处理 .xlsx 工作簿：读取值/公式/合并单元格/数字格式，做描述统计与分组聚合，导出 CSV/JSON/Markdown/HTML，写入单元格与公式、追加行、设置格式、插入图表。

### `probe`

工作簿概览：工作表、尺寸、公式/图表/合并/冻结、命名区域

| 参数 | 说明 |
| --- | --- |
| `deep` | true 时统计每张表的公式与合并单元格数量（默认 true） |

### `read`

读取表格数据：值 + 公式 + 表头 + 记录 + 合并单元格

| 参数 | 说明 |
| --- | --- |
| `sheet` | 工作表名或索引（默认第一张）；all 表示全部 |
| `header_row` | 表头所在行（默认 1；0 表示无表头） |
| `max_rows` | 每张表最多返回的数据行数（默认 500） |
| `max_cols` | 每张表最多返回的列数（默认 100） |
| `mode` | values（默认，优先缓存值）| formulas | both |
| `include_formulas` | 是否附带公式明细（默认 true） |

### `analyze`

数据分析：描述统计、缺失率、唯一值、重复行、相关性、分组聚合、离群点

| 参数 | 说明 |
| --- | --- |
| `sheet` | 工作表名或索引（默认第一张） |
| `header_row` | 表头所在行（默认 1） |
| `by` | 分组聚合的分组列名（可选） |
| `agg` | 分组聚合方式，如 sum,mean,count（默认 sum,mean,count） |
| `top_n` | 每列 Top 值个数（默认 5） |
| `corr` | 是否输出数值列相关性（默认 true） |
| `sample_rows` | 预览行数（默认 8） |

### `transform`  _（产出文件）_

导出与转换：CSV / JSON / Markdown / HTML / 筛选 / 排序 / 合并 / 透视

| 参数 | 说明 |
| --- | --- |
| `op` | to_csv | to_json | to_markdown | to_html | filter | sort | merge_sheets | pivot |
| `sheet` | 工作表名或索引（默认第一张） |
| `header_row` | 表头行（默认 1） |
| `query` | filter：pandas query 表达式，如 '数值 > 10 and 区域 == "华东"' |
| `columns` | sort/select：列名列表 |
| `ascending` | sort：是否升序（默认 true） |
| `index` | pivot：行索引列 |
| `pivot_columns` | pivot：列方向字段 |
| `values` | pivot：聚合值字段 |
| `aggfunc` | pivot：聚合函数（默认 sum） |

### `edit`  _（会修改输入文件（自动备份））_

修改工作簿：写单元格/公式、追加行、增删改工作表、设置格式、图表、冻结窗格

| 参数 | 说明 |
| --- | --- |
| `op` | set_cell | write_range | append_rows | set_formula | add_sheet | delete_sheet | rename_sheet | format_range | autofit | freeze_panes | merge_cells | insert_rows | delete_rows | replace_text | add_chart | set_column_width |
| `sheet` | 目标工作表（默认第一张） |
| `cell` | set_cell / set_formula：单元格坐标，如 B2 |
| `value` | set_cell：写入值 |
| `formula` | set_formula：公式，如 =SUM(B2:B10) |
| `range` | write_range / format_range 的区间，如 A1:C10 |
| `rows` | write_range / append_rows：二维数组 |
| `name` | add_sheet / rename_sheet 的工作表名 |
| `in_place` | 是否就地保存（默认 true） |

---


---

## 附：模块索引

| 模块 | 展示名 | 支持扩展名 | 动作 |
| --- | --- | --- | --- |

| 模块 | 展示名 | 支持扩展名 | 动作 |
| --- | --- | --- | --- |
| `docx` | DOCX 文档 | .docx, .dotx | probe, read, analyze, transform, edit |
| `html` | HTML 网页 | .html, .htm, .xhtml | probe, read, analyze, transform, edit |
| `image` | 图片（PNG/JPG 等） | .png, .jpg, .jpeg, .gif, .bmp, .webp, .tif, .tiff | probe, read, analyze, transform, edit |
| `pdf` | PDF 文档 | .pdf | probe, read, analyze, transform |
| `pptx` | PPTX 演示文稿 | .pptx, .ppsx, .potx | probe, read, analyze, transform, edit |
| `txt` | 纯文本文件 | .txt | probe, read, analyze, transform, edit |
| `xlsx` | XLSX 工作表 | .xlsx, .xlsm, .xltx | probe, read, analyze, transform, edit |
