# 工具索引

> 由 `scripts/gen_tool_specs.py` 自动生成 —— 综合性办公文件处理技能 v0.1
> 契约来源：各 handler 的 `@action(...)` 声明，与运行时 `python -m core.cli cap <module>` 一致。

## 模式一：通用工具（默认，推荐）

共 4 个工具，覆盖全部能力，上下文占用最小。

| 工具名 | 说明 | 必填参数 |
| --- | --- | --- |
| `file_dispatch` | 综合性办公文件处理技能：统一处理 PPTX、DOCX、XLSX、HTML 与图片（含 OCR 图像内容识别）的读取、分析、转换与修改。 | `action`, `input` |
| `file_capabilities` | 查询本技能支持的模块与动作契约（含每个动作的全部参数说明）。 | — |
| `file_doctor` | 环境自检：Python 版本、依赖是否齐备（缺失时直接给出 pip 安装命令）、OCR 后端是否可用、可用模块列表。 | — |
| `file_batch` | 对目录批量执行同一动作，例如「把 ./docs 下所有 pptx 和 docx 都体检一遍」。 | `action`, `target` |

## 模式二：细粒度工具

共 34 个工具（每模块每动作一个），参数已按类型展开，适合弱模型或需要强约束的场景。

| 工具名 | 说明 | 参数 |
| --- | --- | --- |
| `file_docx_probe` | [DOCX 文档] ZIP 层快速探测：段落/表格/图片/公式/批注的数量与文档体量 | `deep` |
| `file_docx_read` | [DOCX 文档] 按正文真实顺序读取段落/表格/图片/公式/页眉页脚 | `include_tables`, `include_headers`, `include_formulas`, `include_images`, `include_empty`, `max_formula_xml` |
| `file_docx_analyze` | [DOCX 文档] 文档分析：大纲层级、字数统计、关键词、样式分布、公式/图片、结构问题诊断 | `top_n`, `max_heading_level` |
| `file_docx_transform` | [DOCX 文档] 导出 Markdown / 纯文本 / 图片 / OMML 公式 / HTML / 原始 XML（产出文件） | `op`, `formula_as` |
| `file_docx_edit` | [DOCX 文档] 修改文档：查找替换（保格式）/追加段落标题/插表格/插入 OMML 公式/清理空段/写属性（会修改输入文件并自动备份） | `op`, `find`, `replace`, `regex`, `text`, `level`, `style`, `rows`, `formula`, `in_place` |
| `file_html_probe` | [HTML 网页] 快速概览：标题、meta、各类元素数量、内联资源占比 | — |
| `file_html_read` | [HTML 网页] 结构化读取：元信息、标题大纲、正文、链接、图片、表格、列表、结构化数据 | `include_text`, `main_only`, `include_links`, `include_images`, `include_tables`, `max_text_chars` |
| `file_html_analyze` | [HTML 网页] 质量审查：SEO、无障碍、可读性、外链分布、配色与字号统计、关键词 | `top_n`, `main_only` |
| `file_html_transform` | [HTML 网页] 转换与打包：Markdown / 纯文本 / 自包含单文件 / 抽取 CSS·JS / 净化 / 格式化（产出文件） | `op`, `base`, `keep_scripts`, `embed_base64` |
| `file_html_edit` | [HTML 网页] 修改页面：替换文本/改标题/写 meta/选择器增删改节点/注入 CSS（会修改输入文件并自动备份） | `op`, `find`, `replace`, `target`, `selector`, `text`, `position`, `name`, `content`, `in_place` |
| `file_image_probe` | [图片（PNG/JPG 等）] 快速探测：格式、尺寸、模式、DPI、透明度、动画、体积 | `exif` |
| `file_image_read` | [图片（PNG/JPG 等）] 读取元数据：EXIF（含 GPS）、ICC、调色板、质量指标 | `include_exif`, `include_palette`, `palette_size`, `include_metrics` |
| `file_image_analyze` | [图片（PNG/JPG 等）] 内容识别：OCR 文字 + 版面还原 + 类型判别 + 视觉桥接载荷 | `ocr`, `ocr_langs`, `ocr_backend`, `ocr_min_confidence`, `binarize`, `vision_payload`, `palette_size` |
| `file_image_transform` | [图片（PNG/JPG 等）] 转换与导出：格式转换、缩放、裁剪、旋转、压缩、水印、转 PDF、base64（产出文件） | `op`, `format`, `width`, `height`, `percent`, `keep_aspect`, `box`, `angle`, `quality`, `text`, `max_side` |
| `file_image_edit` | [图片（PNG/JPG 等）] 就地编辑：打码遮盖、模糊/马赛克区域、文字标注、画框、加边框、拼网格（会修改输入文件并自动备份） | `op`, `box`, `text`, `color`, `width`, `radius`, `block`, `opacity` |
| `file_pdf_probe` | [PDF 文档] 快速概览：页数、元数据、加密状态、PDF 版本、书签数、首页文本预览 | `preview_chars` |
| `file_pdf_read` | [PDF 文档] 按页结构化读取：每页文本与字符数、元数据、书签大纲、文本统计 | `page_range`, `max_chars`, `include_outline` |
| `file_pdf_analyze` | [PDF 文档] 质量诊断：关键词、逐页字符分布、空页/纯图页识别、加密与元数据完整性检查 | `top_n` |
| `file_pdf_transform` | [PDF 文档] 转换导出：to_text（全文 txt）/ to_markdown（分页 md）/ to_json（逐页结构化）/ split（按页拆出新 PDF）（产出文件） | `op`, `page_range`, `split_pages` |
| `file_pptx_probe` | [PPTX 演示文稿] ZIP 层快速探测：页数、媒体、备注、尺寸，不依赖 python-pptx | `deep` |
| `file_pptx_read` | [PPTX 演示文稿] 结构化读取全部幻灯片（标题/形状/表格/图表/备注/图片） | `include_notes`, `include_tables`, `include_charts`, `include_empty`, `slide_range`, `include_runs` |
| `file_pptx_analyze` | [PPTX 演示文稿] 内容识别：大纲、字数、关键词、字体/配色分布、空页检测、图片 OCR | `top_n`, `ocr`, `ocr_langs`, `ocr_backend`, `min_chars_per_slide` |
| `file_pptx_transform` | [PPTX 演示文稿] 导出为 Markdown / 纯文本 / 大纲 JSON / 图片 / 媒体 / PDF（产出文件） | `op`, `render_format`, `layout_placeholder` |
| `file_pptx_edit` | [PPTX 演示文稿] 修改演示文稿：查找替换（保格式）/改标题/增删复制页/加文本框/写属性（会修改输入文件并自动备份） | `op`, `find`, `replace`, `regex`, `slide`, `text`, `title`, `bullets`, `layout_index`, `in_place` |
| `file_txt_probe` | [纯文本文件] 快速概览：编码、行尾风格、行数、空行占比、最长行、开头预览 | `preview_chars` |
| `file_txt_read` | [纯文本文件] 结构化读取：全文或带行号片段、文本统计、行尾与编码信息 | `max_chars`, `full`, `line_numbers`, `offset`, `limit` |
| `file_txt_analyze` | [纯文本文件] 内容分析：词频关键词、行级分布、重复行 Top、疑似乱码与格式问题诊断 | `top_n`, `duplicate_min` |
| `file_txt_transform` | [纯文本文件] 转换导出：to_json（行数组）/ to_jsonl / to_markdown（段落化）/ normalize（规范行尾与空白）（产出文件） | `op`, `encoding`, `crlf` |
| `file_txt_edit` | [纯文本文件] 修改文本：replace（查找替换）/ append / prepend / delete_lines（按模式删行）/ normalize（就地清理）（会修改输入文件并自动备份） | `op`, `find`, `replace`, `regex`, `ignore_case`, `text`, `in_place` |
| `file_xlsx_probe` | [XLSX 工作表] 工作簿概览：工作表、尺寸、公式/图表/合并/冻结、命名区域 | `deep` |
| `file_xlsx_read` | [XLSX 工作表] 读取表格数据：值 + 公式 + 表头 + 记录 + 合并单元格 | `sheet`, `header_row`, `max_rows`, `max_cols`, `mode`, `include_formulas` |
| `file_xlsx_analyze` | [XLSX 工作表] 数据分析：描述统计、缺失率、唯一值、重复行、相关性、分组聚合、离群点 | `sheet`, `header_row`, `by`, `agg`, `top_n`, `corr`, `sample_rows` |
| `file_xlsx_transform` | [XLSX 工作表] 导出与转换：CSV / JSON / Markdown / HTML / 筛选 / 排序 / 合并 / 透视（产出文件） | `op`, `sheet`, `header_row`, `query`, `columns`, `ascending`, `index`, `pivot_columns`, `values`, `aggfunc` |
| `file_xlsx_edit` | [XLSX 工作表] 修改工作簿：写单元格/公式、追加行、增删改工作表、设置格式、图表、冻结窗格（会修改输入文件并自动备份） | `op`, `sheet`, `cell`, `value`, `formula`, `range`, `rows`, `name`, `in_place` |

## 切换方式

```bash
# MCP：默认通用模式
python bridges/mcp_server.py
# MCP：细粒度模式
python bridges/mcp_server.py --granular
# 导出其它格式
python bridges/stdio_bridge.py --once <<< '{"cmd":"tools","format":"openai"}'
```
