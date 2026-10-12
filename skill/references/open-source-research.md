# 选型调研：开源方案与取舍依据

本文件记录开发本工具箱时对 GitHub 开源方案与公开资料的调研结论，以及最终取舍。
调研时间：2026-09。

## 一、Agent Skill 的组织范式（Anthropic Agent Skills）

Anthropic 开源了 Claude.ai 文档能力的底层实现（`github.com/anthropics/skills`，含 `docx` / `pptx` / `xlsx` / `pdf` 四个文档技能，另有大量 Apache-2.0 通用技能），其组织方式已成为事实标准（AgentSkills 规范，见 agentskills.io）：

```
skill-name/
├── SKILL.md        # 必需：元数据(name/description) + 核心指令
├── scripts/        # 可选：可确定性执行的脚本
├── references/     # 可选：按需加载的详细资料
└── assets/         # 可选：模板与资源
```

**对本项目的三条直接借鉴：**

1. **渐进式披露（Progressive Disclosure）三层结构**
   启动时只加载 `name` + `description`（几十 token）；命中后读完整 `SKILL.md`；再按需读 `references/`；
   稳定步骤交给 `scripts/`。本工具箱据此把「主文档」压缩为路由 + 速查，把参数手册拆到
   `references/api-reference.md`，把确定性工作沉淀为 `core/` 与 `scripts/`。

2. **`description` 是路由规则，不是简介**
   官方示例把「做什么 + 何时用 + 触发关键词」全写进 description。本技能的 frontmatter 即按此写，
   并显式声明**不适用场景**（从零创作 → tencent-docx/pptx；图像美化 → buddy-image-processing），
   避免误触发 —— 路由越精准，Agent 行为越稳定。

3. **"生成 → 验证 → 修复" 循环**
   官方 `docx/scripts/validate.py` 的做法是生成后校验，失败则解包 XML 修复再打包。
   本项目对应落地为 `scripts/doctor.py`（环境自检）+ `scripts/smoke_test.py`（46 个端到端用例），
   把「能不能跑」变成可自动验证的事实，而不是靠感觉。

**未采纳的部分**：官方 `pptx`/`xlsx` 技能基于 Node.js（pptxgenjs / SheetJS）+ pandoc + LibreOffice。
本项目选择 Python 生态，理由是：目标环境已固定使用 Python 虚拟环境；且 Python 的 `python-pptx` /
`python-docx` / `openpyxl` 能直接读写 OOXML 对象模型（保格式、读公式、改样式），
而后者的 JS 方案更偏向「从零生成」，对「修改已有文件且保留原格式」支持较弱。

## 二、各文件类型的技术栈选型

| 文件类型 | 候选方案 | 选择 | 理由 |
| --- | --- | --- | --- |
| PPTX | `python-pptx` / 直接操作 OOXML / LibreOffice 转换 | **python-pptx + ZIP 层直读** | `python-pptx` 提供对象模型；`probe` 走 ZIP 直读可零依赖、更快（只数字节与目录，不全量解析） |
| DOCX | `python-docx` / `mammoth` / `docx2python` / pandoc | **python-docx + lxml 直操作** | 需要「保留原格式的查找替换」与「真 OMML 公式读写」，只有直接操作 XML 才能做到；`mammoth` 仅作为 `to_html` 的可选增强 |
| XLSX | `openpyxl` / `XlsxWriter` / `pandas` / `pyexcel` | **openpyxl（结构层）+ pandas（分析层）** | openpyxl 能写公式、条件格式、图表；pandas 做描述统计/分组/相关性。XlsxWriter 只能写不能读，故不作主引擎 |
| HTML | `BeautifulSoup`+`lxml` / `lxml.html` / 正则 | **BeautifulSoup + lxml 解析器** | 容错性最好（真实网页大量不闭合标签）；lxml 作为后端兼顾速度 |
| 图片 | `Pillow` / `OpenCV` / `scikit-image` | **Pillow 为主，numpy 算指标，OpenCV 可选增强** | Pillow 覆盖全部读写与绘制需求；Laplacian 方差、色彩丰富度用 numpy 手写即可，避免为几个指标引入 opencv（~40MB） |

**整体方案为什么没选 `unstructured` / Apache Tika**：两者确实能用「一个调用吃下所有格式」，
但它们是重型依赖（Tika 需 JVM，unstructured 会拉入 detectron2/onnx 等一大串），
且返回的是「元素流」而非「可回写的对象模型」—— 满足不了本工具箱「修改并保格式」的核心诉求。
在**只需要提取文本做 RAG 入库**的场景下它们更合适，但那不是本工具箱的定位。

## 三、OCR 方案横向对比（图像内容识别的关键选型）

| 引擎 | 中文印刷体准确率 | 模型体积 | 运行时依赖 | 完全离线 | 纯 pip 可装 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| **RapidOCR** | ~94%（复用 PP-OCR 模型转 ONNX） | 数十 MB | `onnxruntime` | ✅ | ✅ | **选为默认后端**：无需系统可执行文件，CPU 推理快，中文准 |
| Tesseract 5.x | 88.7% | ~210 MB | 系统级可执行文件 + 语言包 | ✅ | ❌（Python 绑定需另装二进制） | 保留为备选：参数最可控，适合已部署 Tesseract 的环境 |
| PaddleOCR | 94.5% | 45 MB（基础） | `paddlepaddle`（体积大） | ✅ | ⚠️（依赖链重） | 未内置：精度与 RapidOCR 同级，但依赖过重 |
| EasyOCR | 81.2% | 18 MB（基础） | `torch` | ✅ | ⚠️（拉 PyTorch） | 保留为末位备选 |
| 云 OCR（阿里/腾讯/百度） | >97% | — | 网络 + 付费 + 实名 | ❌ | — | 未采用：与「离线、可移植、零成本」的定位冲突 |

**落地策略**：不绑定单一引擎，而是抽象成 `ocr/engines.py` 的可插拔链路
（`rapidocr → tesseract → easyocr` 自动降级），并以 `DependencyError` +
`hint`（含安装命令）暴露不可用状态。上层的 `analyze` 动作把它降级为 **warning** 而非错误，
保证「没装 OCR」时其余统计与判别能力照常可用。

### 一个踩到的具体坑（值得记录）

RapidOCR 的输入加载层只接受 **路径 / bytes / ndarray**，直接传 PIL `Image` 对象会失败；
而 `pytesseract` 对路径与 PIL 对象都接受。为统一三家行为，`recognize()` 的处理是：
**预处理后先存为临时 PNG，再把路径传给后端**，`finally` 中清理。
这条经验已写入 `extension-guide.md`，新增 OCR 后端时必须遵守。

## 四、工程实践参考

调研中确认并采纳的几条通用实践：

| 实践 | 来源 | 本项目落地 |
| --- | --- | --- |
| Office 文档的**文档顺序遍历**应直接迭代 `w:body` 子元素 | `python-docx` 对象模型局限的普遍讨论 | `docx_handler._iter_blocks()` 按 `w:p` / `w:tbl` 真实顺序产出，避免「先全部段落、后全部表格」 |
| `.xlsx` 用 `data_only=True` 读公式单元格可能得到 `None` | `openpyxl` 官方文档明确说明（缓存值需文件曾被 Excel 打开保存） | 同时加载公式版与值版工作簿；缺失时回退公式文本并**显式 warning**，防止误判为空数据 |
| 大表格用 `read_only=True` 降低内存 | `openpyxl` 性能建议 | `analyze` / `transform` 一律 `read_only=True`；仅需写回时才用可写模式 |
| 跨 run 文本替换需按「字符归属 run」重建 | OOXML 文本被切分的固有问题 | `core/textops.py` 的 `rewrite_runs()`：拼接 → 匹配 → 按字符归属回填，未参与匹配的 run 格式完整保留 |
| 表格读取用 `pdfplumber`、`pandas.read_excel` 等成熟路径 | 多方实践文章 | `xlsx` 分析层直接用 pandas；结构层用 openpyxl，各司其职 |
| 用 `markdownify` / `html2text` 而非手写 HTML→MD | 社区共识 | `html_handler` 优先 `markdownify`，缺失时降级 `html2text`，再降级内置渲染器 |

## 五、参考资料

- `github.com/anthropics/skills` —— Agent Skills 官方参考实现（docx / pdf / pptx / xlsx）
- `platform.claude.com/docs/en/build-with-claude/skills-guide` —— Skills 的 API 集成与渐进式披露说明
- `python-pptx.readthedocs.io` —— PPTX 对象模型
- `openpyxl.readthedocs.io` —— 公式、图表、条件格式、`data_only` 语义
- `github.com/RapidAI/RapidOCR` —— RapidOCR 工程化落地说明
- agentskills.io —— AgentSkills 开放规范
