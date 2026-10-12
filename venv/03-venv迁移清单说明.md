# 03 · venv 迁移清单说明

> 目的：venv 626MB 不便整体拷贝。本套清单把 venv「降维」成三个轻量文件 + 一个校验脚本，
> 迁移到新电脑时**只带走 skill/ + requirements/（约 1MB）**，即可在新机精确重建环境。

## 一、清单文件一览（全部在 `requirements/` 下）

| 文件 | 体积 | 内容 | 用途 |
| --- | --- | --- | --- |
| `venv_manifest.json` | ~40KB | Python/平台信息 + 117 包全量数组（版本/角色/体积/依赖） | 机读主清单，程序化处理 |
| `venv_packages.csv` | ~30KB | 包级清单（Excel 可直接打开） | 人工审阅：哪些包该装、多大、干嘛用 |
| `venv_files.csv` | ~800KB | **8998 个文件的完整快照**：相对路径/字节/所属包 | 文件级核对与补缺 |
| `pip-freeze-snapshot.txt` | 3KB | pip 精确版本列表 | **重建环境的主命令依据** |
| `check_venv.py` | 4KB | 比对器：核对本机 venv vs 清单，输出补足命令 | 新机校验 |

## 二、清单统计（2026-10-12 快照）

| 角色 | 包数 | 体积 | 说明 |
| --- | --- | --- | --- |
| direct_required | 12 | 103.2MB | 7 模块 + pypdf 的直接依赖，**必装** |
| direct_optional | 11 | 316.1MB | OCR 链等可选能力，缺失只降级不报错（本机 OCR 已实测可用，建议补足） |
| transitive | 94 | 289.2MB | 传递依赖，pip 自动解决 |

体积 Top：`opencv-python-headless` 112MB、`opencv-python` 112MB、`patchright` 105MB、
`playwright` 104MB、`numpy` 40MB、`onnxruntime` 40MB、`pandas` 34MB、`rapidocr` 31MB。

## 三、⚠️ 与技能无关的"搭便车"包（约 320MB，可跳过）

共享 default venv 里混入了其他工具的依赖，**本技能完全用不到**，新机重建时可排除以省体积：

| 包 | 体积 | 实际来源 |
| --- | --- | --- |
| patchright + playwright | 208MB | 浏览器自动化工具（super-auto-web-toolpack 类技能的依赖） |
| opencv-python | 112MB | 与 headless 版重复（rapidocr 用 headless 即可） |

排除后核心体积约 **300MB**；若连 OCR 也不要，可再省 200MB+。

## 四、新机迁移流程（三选一）

```bash
# 前提：新机装好 Python ≥3.13，把整个 bundle 的 skill/ + requirements/ 拷过去

# ── 方案 A：标准重建（推荐，最省事）──────────────────
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements/pip-freeze-snapshot.txt
python requirements/check_venv.py        # 校验

# ── 方案 B：精简重建（排除搭便车包，省 ~320MB）────────
# 手工编辑 pip-freeze-snapshot.txt，删除以下行后按方案 A 执行：
#   patchright / playwright / opencv-python（保留 headless）

# ── 方案 C：最小重建（只要核心 12 包，~100MB）─────────
python -m venv venv
venv\Scripts\python.exe -m pip install python-pptx python-docx openpyxl pandas numpy \
    Pillow beautifulsoup4 lxml markdownify filetype charset-normalizer pypdf
# 注意：此方案无 OCR、无图片分析增强，image 模块文字识别不可用

# ── 重建后校验 ──────────────────────────────────────
venv\Scripts\python.exe verify_bundle.py     # doctor + txt/pdf 端到端
python requirements/check_venv.py            # 文件级比对（方案 B/C 会报"缺失"，属预期）
```

## 五、check_venv.py 输出解读

- **缺失 0 / 体积不符 0** → 环境与源机逐字节一致；
- **缺失 N 个文件，涉及 M 个包** → 按报告给出的 `pip install --force-reinstall <包名>` 补足
  （包名已按 `venv_files.csv` 的 owner 字段聚合）；
- **多出若干 `__pycache__/*.pyc`** → 无害，Python 自动生成的字节码缓存；
- **方案 B/C 下报缺失** → 属预期（清单含全量，精简安装本来就不装那些），看缺失 owner
  是否全在"搭便车"名单内即可。

## 六、清单再生成（环境变更后）

```bash
# 清单生成器在 bundle 内自包含，改完依赖重跑一次即可
venv\Scripts\python.exe scripts\gen_venv_manifest.py
```
