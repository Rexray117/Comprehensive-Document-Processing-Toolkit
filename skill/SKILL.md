---
name: omni-office-file-toolkit
description: >-
  综合性办公文件处理技能：统一处理 PPTX 演示文稿、DOCX 文档、XLSX 工作表、HTML 网页、
  PDF 文档、TXT 纯文本、PNG/JPG 等图片（含 OCR 图像内容识别）的读取、内容识别、转换与修改。
  触发场景：读取/解析/提取 .pptx/.docx/.xlsx/.html/.pdf/.txt/.png/.jpg 的内容；批量扫描目录并汇总；
  把 PPT/Word/Excel/PDF 转成 Markdown、纯文本、CSV、JSON；从图片里提取文字（OCR）、识别图片类型、
  检查清晰度；把外部 CSS/JS/图片内联成单文件自包含 HTML；对文档做跨 run 查找替换且保留原格式；
  导出或插入 DOCX 的真 OMML 公式；给 Excel 写公式/设格式/插图表；批量修改文档属性；
  交付前的文档质量审查（标题层级、缺失 alt、空页、数据缺失率）。
  用户提到「读取这个文件」「提取内容」「转换格式」「批量处理文档」「OCR」「识别图片文字」
  「提取表格」「转 Markdown」「读 PDF」「读文本文件」「文档体检」时使用。
  不用于：从零创作排版精良的交付级文档（用 tencent-docx / tencent-pptx / tencent-docs-sheet-generation），
  也不用于图片美颜/去水印/抠像（用 buddy-image-processing）。
version: 0.2.0
license: Apache-2.0
agent_created: true
---

# 综合性办公文件处理技能

> **显示名**：综合性办公文件处理技能 ｜ **标识符**：`omni-office-file-toolkit` ｜ **版本**：v0.2.2
>
> 前置说明：Agent Skills 标准要求 `name` 字段为 ASCII 小写连字符形式（用于跨平台标识与工具命名），
> 因此这里用 `omni-office-file-toolkit`；中文名「综合性办公文件处理技能」用于展示。
> 安装目录名与发行包名同样使用 ASCII（`omni-office-file-toolkit/`）：DSH 按目录名发现技能，
> 目录名与 `name` 不一致会导致 `doctor`/`verify` 之类的自检误判「未安装」。

一个插件式的文件处理整合包：**7 个处理模块 + 4 条集成路径**，既能整体调度，也能单模块独立运行。

---

## 一、核心设计（先读这段，能省很多试错）

### 1.1 五个统一动作，跨模块完全一致

记住 5 个词就能操作全部 5 类文件：

| 动作 | 语义 | 典型用途 |
| --- | --- | --- |
| `probe` | 极速探测（多数走 ZIP 层直读，不加载完整解析器） | 先看「这是什么文件、多大、多少页/表/图」 |
| `read` | 结构化读取 | 取标题层级、段落、表格、公式、图片、链接、单元格 |
| `analyze` | 内容识别与洞察 | 关键词、大纲、统计、质量诊断、**图片 OCR 与类型判别** |
| `transform` | 转换导出（产出新文件） | 转 Markdown / CSV / JSON / 单文件 HTML / 导出图片 |
| `edit` | 修改（默认就地保存并自动备份） | 跨 run 查找替换、写单元格公式、增删页、打码 |

### 1.2 统一返回信封

所有调用返回同一种结构，集成方不用为每个模块写适配：

```json
{ "ok": true, "module": "pptx", "action": "read", "elapsed_ms": 18,
  "warnings": [], "artifacts": [{"path": "...", "kind": "markdown", "bytes": 1234}],
  "data": { }, "error": null }
```

失败时 `ok=false`，且 `error.hint` **直接带着修复命令**（如缺依赖时的 pip 安装命令），可据此自愈。

### 1.3 四个必须知道的约定

1. **`edit` 默认就地保存**，并在原文件旁生成 `<名称>_bak.<后缀>` 备份。要保留原文件就传
   `in_place=false`（输出 `<名称>_edited.<后缀>`）或用 `output` 指定路径。
2. **`transform` 默认输出到 `<文件名>_out/`**，不污染源文件；产物路径在 `artifacts` 字段里。
3. **Excel 公式单元格可能读到 `null`** —— 该文件若从未被 Excel/WPS 打开保存过，公式没有缓存值。
   工具会给出 warning 并回退显示公式文本，**不要当成空数据**。
4. **图片 OCR 需要额外依赖**。默认 `analyze` 会尝试 OCR；后端不可用时**降级为 warning 并跳过**
   （不是报错），统计与判别照常输出。要启用：`pip install -r requirements-ocr.txt`。

---

## 二、集成方式（四条路径，挑一条）

| 路径 | 适用 | 入口 |
| --- | --- | --- |
| **A. MCP** | 宿主支持 MCP（Claude Desktop / Code / 各类 MCP 框架） | `bridges/mcp_server.py` |
| **B. stdio 桥接** | 自定义 harness，能起子进程 | `bridges/stdio_bridge.py` |
| **C. 进程内调用** | 与你自己的 Python 服务同进程 | `core.dispatcher.run` |
| **D. Function Calling** | 只用模型 API，自己管工具循环 | `harness/*.json` |

详细配置、协议说明与排错清单见 **`bridges/INTEGRATION.md`**。

### 最快上手（路径 A）

```bash
# ① 装依赖（可选！核心依赖已随包内置于 vendor/，见下方「自带依赖」）
python -m pip install -r requirements.txt

# ② 自检：缺什么、怎么装，一目了然
python scripts/doctor.py

# ③ 生成可直接粘贴的 MCP 配置
python bridges/mcp_server.py --print-config

# ④ 协议自检（13 项断言，含真实文件处理）
python bridges/mcp_server.py --selftest
```

### 自带依赖（vendor/）：不装 pip 包也能跑

仓库内置了 6 个**纯 Python、跨平台、无编译产物**的依赖副本，位于 `vendor/`：

| 包 | 覆盖的能力 | 许可证 |
| --- | --- | --- |
| `pypdf` | `pdf` 模块全部动作（probe / read / analyze / transform） | BSD-3-Clause |
| `beautifulsoup4` + `soupsieve` | `html` 模块的 DOM 解析 | MIT |
| `markdownify` | HTML → Markdown | MIT |
| `filetype` | 魔数嗅探（真实文件类型判定） | MIT |
| `charset-normalizer` | 文本编码自动探测（GBK/Big5 等） | MIT |

工作机制见 `core/vendor_boot.py`：导入 `core` 包时把 `vendor/` **追加**到 `sys.path` 末尾，
因此「机器上已 pip 安装的真实包优先，装不上时才用内置副本」。设置环境变量
`OMNI_NO_VENDOR=1` 可以完全禁用内置副本，用来排查到底是哪个包在起作用。

`requirements.txt` 里的其余依赖（python-pptx / python-docx / openpyxl / pandas / numpy /
Pillow / lxml）**必须**用 pip 安装——它们是带原生扩展或体积较大的包，不适合随包分发。

每个模块的能力自描述里有两组依赖字段，含义不同，别混：

- `required_deps`：缺了**整个模块的动作都会失败**（如 `pdf` 缺 `pypdf`）
- `optional_deps`：缺了只是**降级**（如 `image` 缺 `rapidocr` 则 OCR 跳过）

`python -m core.cli list` 会在硬依赖缺失时直接打印 `✗ 硬依赖缺失: ...`。

### 路径 B / C / D 速览

```bash
# B. 常驻桥接：stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应
python bridges/stdio_bridge.py
echo '{"action":"probe","input":"deck.pptx"}' | python bridges/stdio_bridge.py --once
```

```python
# C. 进程内调用（永不抛异常，始终返回信封）
import sys; sys.path.insert(0, "<技能目录>")
from core.dispatcher import run, run_batch

env = run({"module": "auto", "action": "analyze", "input": "deck.pptx"})
if env["ok"]:
    print(env["data"]["keywords"][:5])

print(run_batch("./docs", "probe", kinds=["pptx", "docx", "xlsx"])["succeeded"])
```

MCP 默认暴露 **4 个工具**（`file_dispatch` / `file_capabilities` / `file_doctor` / `file_batch`）。
加 `--granular` 可展开为 25 个细粒度工具（参数已按类型展开，适合弱模型）。

---

## 三、模块能力速查

| 模块 | 扩展名 | 亮点能力 |
| --- | --- | --- |
| `pptx` | `.pptx` `.ppsx` `.potx` | 备注页/表格/图表/图片读取；大纲与字体配色统计；**跨 run 查找替换保格式**；复制/增删/移动幻灯片；导出内嵌图片；LibreOffice 渲染 PDF/PNG |
| `docx` | `.docx` `.dotx` | **按正文真实顺序**遍历段落与表格（不错位）；**导出/插入真 OMML 公式**（可编辑，非图片；导出用 `transform op=extract_omml`，插入用 `edit op=insert_formula` 且 `formula` 必须是 `<m:oMath>` 片段而非 LaTeX）；页眉页脚替换；追加 Markdown；清理空段 |
| `xlsx` | `.xlsx` `.xlsm` `.xltx` | 值与公式双读（含缓存缺失显式告警）；描述统计/相关性/分组聚合/离群点；透视表；**写公式、设格式、冻结窗格、插图表** |
| `html` | `.html` `.htm` `.xhtml` | 正文/标题/链接/表格/JSON-LD 提取；**SEO 与无障碍审查**；**一键内联 CSS/JS/图片成单文件自包含 HTML**；选择器级增删改 |
| `pdf` | `.pdf` | 页数/元数据/加密状态探测；**按页文本提取（页码区间）**；书签大纲；空页/纯图扫描件识别；导出文本/Markdown/JSON；按页拆分（只读模块，不做写编辑） |
| `txt` | `.txt` | 编码智能探测；行尾风格诊断（CRLF/LF/混合）；统计与关键词；重复行检测；转 JSON/JSONL/Markdown；就地查找替换、按模式删行、格式清理（零第三方依赖） |
| `image` | `.png` `.jpg` `.gif` `.bmp` `.webp` `.tiff` | EXIF/GPS 读取；**OCR 多后端可插拔**；清晰度/亮度/噪声/色彩丰富度；图片类型启发式判别；**视觉桥接载荷**；压缩/裁剪/水印/打码 |

完整参数表：

```bash
python -m core.cli cap <module>        # 运行时查询（推荐给 Agent）
python -m core.cli list                # 列出全部模块与动作
```

或直接读 `references/api-reference.md`（由代码契约自动生成，不会与实现漂移）。

---

## 四、图像内容识别的四层结构

「识别图片内容」在本技能里是递进的四层，按需选择深度：

1. **元数据层** — `probe` + `options.exif=true`：格式、尺寸、DPI、EXIF（含 GPS，会提示隐私风险）
2. **统计层** — `read`：清晰度（Laplacian 方差）、亮度、对比度、噪声、主色板
3. **文字层** — `analyze` + `options.ocr=true`：OCR 文本 + 行级版面还原 + 置信度过滤
4. **语义层** — `analyze` + `options.vision_payload=true`：载荷在
   **`data.vision_bridge`** 里（不是 `data.vision_payload`，那个键名只是开关），
   结构为 `{mime, data_uri, prompt, local_signals_to_include}`；把它交给**你自己的多模态能力**
   即可得到「图里画了什么」的语义描述。

**为什么语义层做成桥接而不在本地跑 VLM**：本地 VLM 需要数 GB 权重与 GPU，与本技能
「纯 pip、离线、轻量」的定位冲突。把语义层显式交给调用方，既避免重型依赖，
也让识别质量随调用方模型升级而自动提升。

---

## 五、与其它技能的分工

- 需要**从零生成排版精良的交付级** Word/PPT/Excel → 用 `tencent-docx` / `tencent-pptx` / `tencent-docs-sheet-generation`
- 需要**读/析/改/批处理/打包**已有文件 → 用本技能
- 需要图片**美颜、去水印、抠像、修复** → 用 `buddy-image-processing`
- 本技能负责图片的**结构化信息与文字**，不负责视觉美化

---

## 六、扩展新模块（3 步，无需改框架）

```python
# handlers/markdown_handler.py
from core.base import BaseHandler, ActionContext, action
from core.version import VERSION

class MarkdownHandler(BaseHandler):
    module = "markdown"
    version = VERSION
    display_name = "Markdown 文档"
    description = "处理 .md 文件……"
    extensions = (".md",)
    kinds = ("md",)

    @action("read", summary="读取 Markdown 结构", params={"include_frontmatter": "是否解析 frontmatter"})
    def read(self, ctx: ActionContext) -> dict:
        return {"text": ctx.input_path().read_text(encoding="utf-8")}
```

丢进 `handlers/` 即被自动发现（`core.registry` 扫描目录）。

扩展后请依次跑：`scripts/doctor.py` → `scripts/smoke_test.py` → `scripts/gen_api_doc.py` → `scripts/gen_tool_specs.py`。
详见 `references/extension-guide.md`。

---

## 七、目录导航

```
SKILL.md                    ← 本文件（Agent 入口）
MANIFEST.json               ← 机器可读清单（模块/动作/依赖/集成入口）
VERSION / CHANGELOG.md      ← 版本与变更记录
requirements*.txt           ← 依赖（核心 / OCR / 增强）

core/        框架层：抽象基类、注册、调度、信封、错误、CLI、文本工具
handlers/    模块层：pptx / docx / xlsx / html / pdf / txt / image（各自可独立运行）
ocr/         能力层：多后端可插拔 OCR
bridges/     集成层：MCP 服务器 / stdio 桥接 / 工具契约生成 + INTEGRATION.md
harness/     集成产物：OpenAI / LangChain 工具定义、工具索引
scripts/     确定性工具：doctor / smoke_test / gen_api_doc / gen_tool_specs / pack_release
references/  详细文档：架构 / API 手册 / 扩展指南 / 选型调研
examples/    请求体示例（20 条）
```

| 想了解 | 读这个 |
| --- | --- |
| 分层架构与设计取舍 | `references/architecture.md` |
| 全部动作与参数 | `references/api-reference.md`（自动生成） |
| 怎么扩展 | `references/extension-guide.md` |
| 怎么集成 | `bridges/INTEGRATION.md` |
| 选型依据与技术调研 | `references/open-source-research.md` |
| 变更历史 | `CHANGELOG.md` |
