# Comprehensive Document Processing Toolkit

[![Skill](https://img.shields.io/badge/skill-omni--office--file--toolkit%20v0.2.1-4a7dff)](./skill/SKILL.md)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](./requirements)
[![license](https://img.shields.io/badge/license-Apache--2.0-green)](./skill/LICENSE)

> 面向 AI Agent 的综合性文档处理工具包：**skill 文件 + 环境依赖 + 可脚本调用工具链**。
> 核心技能 `omni-office-file-toolkit` v0.2.1 统一处理 PPTX / DOCX / XLSX / HTML / PDF / TXT / 图片。

## 能力清单

- **读取与提取**：解析 Office / PDF / HTML / 文本 / 图片内容，批量扫描目录并汇总
- **格式转换**：PPT/Word/Excel/PDF → Markdown、纯文本、CSV、JSON
- **编辑与改写**：跨 run 查找替换且保留原格式；插入/导出 DOCX 真 OMML 公式；Excel 写公式、设格式、插图表；批量修改文档属性
- **图片与 OCR**：图像内容识别（OCR）、类型判定、清晰度检查
- **HTML 工程**：外部 CSS/JS/图片内联成单文件自包含网页
- **交付审查**：标题层级、缺失 alt、空页、数据缺失率等文档体检
- **对外集成**：MCP 服务桥、OpenAI / LangChain 工具规格导出（`skill/bridges/`、`skill/harness/`）

## 目录结构

```
skill/                         # 技能本体（可整目录拷进 agent 技能根）
  SKILL.md  README.md  VERSION MANIFEST.json  CHANGELOG.md  LICENSE
  core/  handlers/  ocr/  bridges/  harness/
  references/  examples/  scripts/
requirements/                  # 依赖清单（基础 / extras / OCR 三组，含 lock 与 pip freeze 快照）
docs/                          # 00 总览 · 01 环境依赖 · 02 工具链路
```

## 环境要求

- Python 3.10+
- 依赖按需三组：`requirements.txt`（基础）、`requirements-extras.txt`、`requirements-ocr.txt`（OCR 额外模型依赖）

## 安装

```bash
# 1) 依赖（建议虚拟环境）
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements/requirements.txt

# 2) 作为技能安装到 agent 技能根
git clone https://github.com/Rexray117/Comprehensive-Document-Processing-Toolkit.git /tmp/cdpt
cp -r /tmp/cdpt/skill ~/.dsh/skills/omni-office-file-toolkit
```

## 自检

```bash
python skill/scripts/doctor.py            # 摘要：能不能跑、缺什么、装什么
python skill/scripts/doctor.py --verbose  # 依赖与外部工具明细
python skill/scripts/doctor.py --json     # 机器可读
```

## 文档

- `docs/00-总览与目录导览.md`
- `docs/01-环境依赖说明.md`
- `docs/02-工具链路说明.md`

## 版本

当前 `skill/VERSION` = **0.2.1**（本次同步：`core/version.py`、`ocr/engines.py`、`references/extension-guide.md`、`README`/`CHANGELOG` 等）

## License

Apache-2.0（见 `skill/LICENSE`）
