# omni-file-toolkit

综合性文件处理工具箱（大型整合包）—— **5 个处理模块 + 1 套统一调度接口**，
支持对 PPTX / HTML / 图片（含 OCR 内容识别）/ DOCX / XLSX 的读取、内容识别、处理与修改。

各模块既可整体调度，也可独立运行；框架零耦合，新增文件类型无需改动任何框架代码。

## 目录结构

```
omni-file-toolkit/
├── SKILL.md                  ← Agent 入口（渐进式披露第一层）
├── README.md                 ← 本文件
├── requirements.txt          ← 核心依赖（纯 pip 可装）
├── requirements-ocr.txt      ← OCR 可选依赖（图像文字识别）
├── requirements-extras.txt   ← 增强可选依赖（缺失自动降级）
├── install.py                ← 安装到技能目录（复制 / 联接 / 建虚拟环境）
│
├── core/                     ← 框架层（不含任何业务逻辑）
│   ├── base.py               抽象基类 + @action 声明 + ActionContext
│   ├── registry.py           模块自动发现与路由
│   ├── dispatcher.py         统一调度（Request → Response，永不抛异常）
│   ├── cli.py                统一命令行
│   ├── result.py             Artifact / Response 信封 / Timer
│   ├── errors.py             分类型异常 + pip 修复命令生成
│   ├── utils.py              类型嗅探、路径、文本统计、中英分词
│   ├── textops.py            跨 run 文本替换算法（PPTX/DOCX 共用）
│   └── runtime.py            依赖与外部工具探测、doctor 报告
│
├── handlers/                 ← 模块层（每个文件一个文件类型，均可独立运行）
│   ├── pptx_handler.py
│   ├── docx_handler.py
│   ├── xlsx_handler.py
│   ├── html_handler.py
│   └── image_handler.py
│
├── ocr/                      ← 能力层：多后端可插拔 OCR
│   └── engines.py
│
├── scripts/                  ← 确定性工具
│   ├── doctor.py             环境自检
│   ├── smoke_test.py         端到端冒烟测试（46 个用例）
│   └── gen_api_doc.py        从代码契约生成 API 手册
│
├── references/               ← 按需加载的详细文档
│   ├── architecture.md       分层架构与设计取舍
│   ├── api-reference.md      自动生成的完整 API 手册
│   ├── extension-guide.md    扩展新模块 / 动作 / OCR 后端
│   └── open-source-research.md  选型调研与开源方案对比
│
└── examples/
    └── requests.example.json 20 个请求体示例
```

## 快速开始

```bash
PY="D:/Job/job in awit/2026 10/20261012 1401 综合性文档技能/omni-office-file-toolkit-bundle/venv/Scripts/python.exe"
cd omni-file-toolkit

"$PY" scripts/doctor.py          # ① 环境自检
"$PY" scripts/smoke_test.py      # ② 端到端自测（自动造样例 + 46 个用例）
"$PY" -m core.cli list           # ③ 列出模块与动作
"$PY" -m core.cli cap docx       # ④ 查看某模块的完整参数契约
```

## 用法示例

```bash
# 单次调用（module=auto 自动识别文件类型）
"$PY" -m core.cli read report.docx --json
"$PY" -m core.cli run -a analyze -i shot.png -p ocr=true

# 批量处理目录
"$PY" -m core.cli batch -a probe -i ./docs --kind pptx,docx,xlsx --json

# JSON 请求体
"$PY" -m core.cli ask '{"module":"xlsx","action":"analyze","input":"book.xlsx","options":{"by":"区域"}}'
```

```python
import sys; sys.path.insert(0, "path/to/omni-file-toolkit")
from core.dispatcher import run

result = run({"module": "auto", "action": "analyze", "input": "deck.pptx"})
print(result["ok"], result["data"].get("keywords", [])[:5])
```

## 统一动作语义

| 动作 | 语义 | 说明 |
| --- | --- | --- |
| `probe` | 探测 | 多数模块走 ZIP 层直读，零依赖也能跑 |
| `read` | 读取 | 结构化输出（标题/段落/表格/公式/图片/单元格） |
| `analyze` | 分析 | 关键词、统计、质量诊断、图片 OCR 与类型判别 |
| `transform` | 转换导出 | 产出新文件（Markdown / CSV / JSON / 单文件 HTML / 图片） |
| `edit` | 修改 | 默认就地保存 + 自动 `.bak` 备份 |

## 模块能力

| 模块 | 扩展名 | 亮点 |
| --- | --- | --- |
| `pptx` | `.pptx` `.ppsx` `.potx` | 备注/表格/图表/图片读取；**跨 run 查找替换保格式**；增删复制移动幻灯片；导出内嵌媒体；LibreOffice 渲染 |
| `docx` | `.docx` `.dotx` | **按正文真实顺序**遍历；**真 OMML 公式读写**；页眉页脚替换；追加 Markdown；清理空段 |
| `xlsx` | `.xlsx` `.xlsm` `.xltx` | 值与公式双读（含缓存缺失告警）；描述统计/相关性/分组/离群点；透视表；写公式/格式/图表 |
| `html` | `.html` `.htm` `.xhtml` | SEO 与无障碍审查；**一键内联成单文件自包含 HTML**；选择器级增删改 |
| `image` | `.png` `.jpg` `.gif` `.bmp` `.webp` `.tiff` | EXIF/GPS；**OCR 多后端**；清晰度/噪声/色彩指标；类型判别；**视觉桥接载荷**；压缩/水印/打码 |

## 文档导航

- 架构与设计取舍 → `references/architecture.md`
- 完整 API 手册 → `references/api-reference.md`（由代码自动生成）
- 如何扩展 → `references/extension-guide.md`
- 选型依据与技术调研 → `references/open-source-research.md`

## 许可

Apache-2.0
