# 变更记录

本文件记录「综合性办公文件处理技能」各版本的对外变更。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [v0.2.2] — 2026-10-30

### 新增

- **`vendor/` 内置依赖**：随仓分发 6 个纯 Python 包（pypdf、beautifulsoup4、soupsieve、
  markdownify、filetype、charset-normalizer），`core/vendor_boot.py` 在导入 `core` 时
  **追加**到 `sys.path` 末尾——本机真实安装优先，装不上才用内置副本，`OMNI_NO_VENDOR=1`
  可禁用。效果：只有 Python 的机器上 `pdf` / `html` / 编码探测开箱可用，
  `scripts/doctor.py` 在系统 Python 3.14 上从「缺 5 个必需依赖」降到「只缺 4 个原生包」。
- **模块硬依赖与可选依赖分离**：`BaseHandler.required_deps`（新增）+ `optional_deps`，
  `capabilities()` 暴露 `missing_required_deps`，`core.cli list` 打印
  `✗ 硬依赖缺失: ...`。此前 `html` 把 bs4 标成"可选依赖"，而 5 个 html 动作全缺它不可用。

### 修正

- **`run_batch` 递归扫到自己的产物**：`core.utils.iter_files()` 会走进上一次
  `transform` 生成的 `<名字>_out/` 目录，把产物当新输入（表现为 `UNSUPPORTED_FORMAT`
  噪音与产物套娃）。现在跳过 `_out` / `*_out` / `_smoke` / `__pycache__` / `.git` 等目录。
- **`scripts/smoke_test.py` 默认往技能目录里写**：只读安装（全局技能目录）下必炸，
  且 `--keep` 语义颠倒（不加 `--keep` 也打印"已保留"却从不清理）。现在默认用系统临时
  目录、不加 `--keep` 时自动清理，`--keep` 才保留。
- **[P0] PDF 模块整体不可用却仍报「依赖齐备」**：`pypdf` 是 `pdf` 模块 `probe/read/auto`
  的唯一解析器，但它既不在 `requirements.txt` 里、也不在 `core/runtime.py:20` 的
  `DEPENDENCIES` 探针表里，而 `doctor()` 的健康判定只认那张表 —— 结果是任意合法 PDF
  都返回 `DEPENDENCY_MISSING`，`scripts/doctor.py` 却打印「核心依赖齐备」。
  现在 `requirements.txt` / `requirements.lock.txt` 声明 `pypdf`，探针表新增
  `("pypdf", "pypdf", ..., True)`，缺失即 `healthy=False`。
- **`doctor --verbose` 必崩**：`dependencies` 在 verbose 下是 list、否则是 dict，
  而 `scripts/doctor.py` 两种假设各对一半，任何一次 `--verbose` 都抛
  `TypeError: list indices must be integers or slices, not str`。
  现在 `dependencies` 恒为汇总 dict，明细固定走新字段 `dependency_details`。
- **cp936 控制台 UnicodeEncodeError**：缺依赖时打印 `✗` 会崩在 `print`，
  恰好只在「最需要看到报告」的场景触发。`scripts/doctor.py` 启动即把
  stdout/stderr 切到 UTF-8 + `errors="replace"`。
- **文本编码静默损坏**：GBK 文件被 `utf-8(replace)` 解成 `����` 后仍报
  `ok=true / warnings=[]`。新增 `core.utils.decode_warning()`，`txt` 模块的
  probe/read/analyze/transform/edit 五处读取统一登记 U+FFFD 警告。
- **`options.target` 被静默忽略**：`pptx` / `docx` 的 `transform` 只读 `op`，
  `target:"pdf"` 返回 `ok=true` 却产出 `.md`。现在 `op` 缺省时回退到 `target`
  （`ctx.text("op") or ctx.text("target")`）。

## [v0.2.1] — 2026-10-13

### 修正

- **OCR 链在 rapidocr 3.x 下静默失败**：`recognize_rapidocr` 解析 `RapidOCROutput` 时
  对 numpy 数组使用 `x or []` / `if payload:` 真值判断，触发
  "The truth value of an array is ambiguous"，OCR 被降级为 warning 跳过
  （旧版 rapidocr-onnxruntime 1.x 返回 tuple 才侥幸未触发）。
  改为 `is None` 显式判空 + `len()` 判空，1.x/2.x/3.x 三种返回结构均正确解析。

### 环境

- 受管运行时解释器（`binaries/python/versions/3.13.12`，即新会话默认 `python`）
  补装全部技能依赖（requirements.txt + pypdf + rapidocr + onnxruntime）。
  此前依赖仅存在于 `envs/default` venv，导致新会话以默认解释器调用技能时 0/19 全缺。

## [v0.2] — 2026-10-12

### 新增

- **模块层 `handlers/` 扩至 7 类文件（25 → 34 个动作）**
  - `txt`（纯文本，零第三方依赖）：编码智能探测（BOM → charset-normalizer → utf-8 宽容）、
    行尾风格诊断（CRLF/LF/CR/混合）、行级统计与关键词分析、重复行 Top、疑似乱码检测；
    转 JSON/JSONL/Markdown、行尾与空白 normalize；就地查找替换（支持正则）、追加/前置、
    按模式删行、就地清理
  - `pdf`（PDF 读取，基于 pypdf，只读不写编辑）：页数/元数据/加密状态/PDF 版本探测、
    首页文本预览；按页文本提取（`page_range` 页码区间）与书签大纲解析；
    逐页字符分布、空页/纯图扫描件识别（占比 >50% 时提示走图像 OCR 路径）；
    导出 to_text/to_markdown/to_json、按页拆分 split（`split_pages` 控制每份页数）
- **依赖**：新增 `pypdf`（PDF 模块必需，纯 Python 离线）；OCR 推荐路径补充
  `rapidocr`（2.x 新包名，兼容 Python 3.13+；原 `rapidocr-onnxruntime>=1.3.24`
  在 Python 3.12+ 无可用发行版）

### 修正

- SKILL.md frontmatter 补充 `agent_created: true`（Agent Skills 标准，供技能管理工具识别）

## [v0.1] — 2026-09-11

首个封版发布。此前为内部开发态（未加版本标记）。

### 新增

- **框架层 `core/`**
  - `BaseHandler` + `@action` 声明式动作契约，元类自动收集，新增动作无需改框架
  - `registry`：`pkgutil` 扫描 `handlers/` 自动发现模块，支持扩展名与文件类型双路由
  - `dispatcher`：`Request → Response` 统一调度，`module=auto` 按文件真实类型自动路由，
    `run()` 永不抛异常，始终返回统一信封
  - `result`：`Artifact` / `Response` / `Timer`
  - `errors`：分类型异常，`hint` 字段直接携带 pip 修复命令
  - `utils`：OOXML 容器嗅探 → 魔数 → 扩展名三级判定；中英混合分词；文本统计；编码探测
  - `textops`：跨 run 文本替换算法（PPTX 与 DOCX 共用同一实现）
  - `runtime`：依赖与外部工具探测、`doctor` 自检报告
  - `cli`：`list` / `cap` / `doctor` / `run` / `batch` / `ask` 六个子命令 + 简写形式
  - `version`：包名、版本、发布标签、MCP 协议版本的唯一来源
- **模块层 `handlers/`**（5 类文件 × 5 个统一动作 = 25 个动作）
  - `pptx`：ZIP 层快速探测；备注/表格/图表/图片读取；跨 run 查找替换保格式；
    增删/复制/移动幻灯片；导出内嵌媒体；LibreOffice 渲染 PDF/PNG
  - `docx`：按正文真实顺序遍历段落与表格；**真 OMML 公式读写**；页眉页脚替换；
    追加 Markdown；清理空段
  - `xlsx`：值与公式双读（含缓存缺失显式告警）；描述统计/相关性/分组聚合/离群点；
    透视表；写公式/设格式/冻结窗格/插图表
  - `html`：正文与结构提取；SEO 与无障碍审查；**一键内联 CSS/JS/图片为单文件自包含 HTML**；
    选择器级增删改
  - `image`：EXIF/GPS；OCR 多后端；清晰度/噪声/色彩指标；图片类型启发式判别；
    视觉桥接载荷；压缩/裁剪/水印/打码
- **能力层 `ocr/`**
  - 多后端可插拔链路：RapidOCR（默认）→ Tesseract → EasyOCR，自动按可用性坠落
  - 统一 `recognize()` 抽象，屏蔽各家 API 差异；无后端时抛可读异常由上层降级为 warning
- **集成层 `bridges/`**
  - `mcp_server`：MCP stdio 服务器，实现 `initialize` / `tools/list` / `tools/call` /
    `resources/list` / `resources/read` / `ping`；协议版本协商；兼容按行分隔与
    `Content-Length` 两种编码；**执行业务代码期间把 `sys.stdout` 重定向到 `stderr`**，
    从机制上杜绝协议流污染
  - `stdio_bridge`：单行 JSON 请求/响应桥接器，支持 `id` 配对、批量数组、常驻与 `--once` 模式
  - `spec`：从动作声明派生工具契约，支持通用 4 工具与细粒度 25 工具两种模式，
    可导出 MCP / OpenAI / LangChain 三种格式
- **工具层 `scripts/`**
  - `doctor`：环境与依赖自检
  - `smoke_test`：端到端冒烟测试，自动造 6 个样例文件并跑 46 个用例
  - `gen_api_doc`：从代码契约生成 API 手册，保证文档不漂移
  - `gen_tool_specs`：生成 harness 可直接注册的工具定义产物
  - `pack_release`：打包为可交付目录 + zip（非破坏性，覆盖前先改名备份）
- **文档**：`SKILL.md`、`README.md`、`bridges/INTEGRATION.md`、`references/`（架构 / API 手册 /
  扩展指南 / 选型调研）、`examples/requests.example.json`（20 条请求体示例）

### 已验证

- `scripts/smoke_test.py --ocr`：46 个用例，45 通过 / 1 警告（公式无缓存值的预期提示）/ 0 失败
- `bridges/mcp_server.py --selftest`：通用与细粒度两种模式各 13/13 通过
- 真实子进程 MCP 往返：`initialize` → `notifications/initialized` → `tools/list` →
  `tools/call`（analyze / transform）全链路可用，stderr 干净，退出码 0
- 跨 run 替换：三 run 跨边界命中，未参与匹配的加粗 run 格式完整保留
- HTML 自包含：外链样式表与脚本残留 0，图片以 base64 内联
- 图像 OCR：145 字符中文识别，平均置信度 0.83，类型判别 `document_scan` 置信度 0.85

### 已知边界

- `pptx` 的 `duplicate_slide` 对**组合形状内的图片**可能丢失图片关系，调用时会给出 warning
- 图像语义层（「图里画了什么」）刻意不在本地跑 VLM，需调用方补一步多模态调用
- `xlsx` 公式缓存值缺失时只能回退显示公式文本（源文件本身的限制，非工具缺陷）
- 渲染 PDF/PNG 依赖系统安装 LibreOffice；未安装时返回可读错误而非静默降级
