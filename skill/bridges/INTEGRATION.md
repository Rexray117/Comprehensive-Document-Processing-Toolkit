# 集成指南

本技能对外提供 **四条集成路径**，按你的 harness 能力挑一条即可。四条路径共用同一套底层调度，
行为完全一致，可混用。

| 路径 | 适用场景 | 集成成本 | 入口 |
| --- | --- | --- | --- |
| **A. MCP** | 宿主支持 MCP（Claude Desktop / Claude Code / 各类支持 MCP 的编排框架） | 最低：加一段 JSON | `bridges/mcp_server.py` |
| **B. stdio 桥接** | 自定义 harness，能起子进程、读写管道 | 低：实现单行 JSON 协议 | `bridges/stdio_bridge.py` |
| **C. 进程内调用** | 与你自己的 Python 服务同一个进程 | 低：一次 import | `core.dispatcher.run` |
| **D. Function Calling** | 只用模型 API，自己管工具循环 | 中：注册工具 + 写执行循环 | `harness/*.json` |

---

## 路径 A：MCP（推荐）

### 1. 安装依赖

```bash
# 用你打算给 harness 用的那个 Python 解释器
python -m pip install -r requirements.txt
# 可选：图像文字识别（OCR）
python -m pip install -r requirements-ocr.txt
```

### 2. 生成配置并粘贴

```bash
python bridges/mcp_server.py --print-config
```

输出形如：

```json
{
  "mcpServers": {
    "omni-office-file-toolkit": {
      "command": "<绝对路径>/python.exe",
      "args": ["<技能目录>/bridges/mcp_server.py"],
      "env": {}
    }
  }
}
```

把它合并进宿主的 MCP 配置即可，例如 Claude Desktop 的
`claude_desktop_config.json`。重启宿主后，工具列表里会出现 `file_dispatch` 等工具。

> **注意**：`command` 必须是**装了依赖**的那个 Python 的绝对路径。若宿主在沙箱/容器里运行，
> 请把 `command` 指向沙箱内的解释器，并在沙箱内提前装好依赖（MCP 运行期不允许联网安装）。

### 3. 提供的工具

默认 **通用模式**（4 个工具，上下文占用最小）：

| 工具 | 作用 |
| --- | --- |
| `file_dispatch` | 统一执行入口：`{action, input, module?, output?, options?}` |
| `file_capabilities` | 查询模块与动作契约（**不确定参数名时先调它**） |
| `file_doctor` | 环境自检，缺依赖时直接给出安装命令 |
| `file_batch` | 对目录批量执行同一动作 |

切成 **细粒度模式**（每模块每动作一个工具，共 25 个，参数已按类型展开）：

```json
{ "args": ["<技能目录>/bridges/mcp_server.py", "--granular"] }
```

### 4. 服务器能力

- 实现方法：`initialize`、`tools/list`、`tools/call`、`resources/list`、`resources/read`、`ping`
- 协议版本协商：客户端版本受支持则顺着客户端，否则回落到本服务最新支持版本
- 传输编码：按行分隔的 JSON-RPC 2.0（同时兼容旧式 `Content-Length` 头帧）
- **资源**：`SKILL.md`、`README.md`、`MANIFEST.json`、本文件、`references/*.md` 全部以 MCP 资源形式暴露，
  宿主可直接读取，无需额外塞进系统提示
- 自检：`python bridges/mcp_server.py --selftest`（13 项断言，含真实文件处理）

### 5. stdout 纯净性保证

stdio 协议服务器最常见的崩溃原因，是业务代码里某处不小心 `print` 了一行，导致客户端收到非法消息。
本服务器在执行任何业务代码期间会把 `sys.stdout` 临时重定向到 `stderr`，**从机制上杜绝**这类污染。
调试日志请设 `OMNI_MCP_DEBUG=1`，日志走 `stderr`。

---

## 路径 B：stdio 桥接（自定义 harness）

常驻进程，stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应。无需实现 MCP。

```bash
python bridges/stdio_bridge.py            # 常驻
python bridges/stdio_bridge.py --once     # 读完 stdin 即退出
```

### 协议

请求可带 `id`（任意 JSON 标量），响应原样回带，便于配对。

| 请求 | 说明 |
| --- | --- |
| `{"id":1,"cmd":"ping"}` | 存活探测 |
| `{"id":2,"cmd":"info"}` | 包名与版本 |
| `{"id":3,"cmd":"capabilities","module":"pptx"}` | 动作契约（省略 `module` 返回全部） |
| `{"id":4,"cmd":"tools","mode":"generic","format":"openai"}` | 工具定义（`format=openai` 转 function-calling 结构） |
| `{"id":5,"cmd":"doctor"}` | 环境自检 |
| `{"id":6,"cmd":"resources"}` | 可读资源清单 |
| `{"id":7,"cmd":"read_resource","path":"SKILL.md"}` | 读取资源 |
| `{"id":8,"cmd":"batch","action":"probe","target":"./docs","kinds":["pptx","docx"]}` | 批量 |
| `{"id":9,"action":"read","input":"deck.pptx"}` | 直接分发（有 `action` 即视为 dispatch） |
| `{"id":10,"cmd":"shutdown"}` | 退出 |
| `[{...},{...}]` | 顶层数组 = 批量请求 |

### 最小客户端（Python）

```python
import json, subprocess

proc = subprocess.Popen(
    [PYTHON, "bridges/stdio_bridge.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    text=True, encoding="utf-8", bufsize=1,
)

def call(payload: dict) -> dict:
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())

print(call({"id": 1, "action": "probe", "input": "deck.pptx"})["response"]["data"]["slide_count"])
```

### 最小客户端（Node.js）

```js
const { spawn } = require("child_process");
const readline = require("readline");

const proc = spawn(PYTHON, ["bridges/stdio_bridge.py"], { stdio: ["pipe", "pipe", "inherit"] });
const rl = readline.createInterface({ input: proc.stdout });
const pending = new Map();
let seq = 0;

rl.on("line", (line) => {
  const msg = JSON.parse(line);
  const resolve = pending.get(msg.id);
  if (resolve) { pending.delete(msg.id); resolve(msg); }
});

function call(payload) {
  const id = ++seq;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    proc.stdin.write(JSON.stringify({ id, ...payload }) + "\n");
  });
}

call({ action: "read", input: "report.docx" }).then((r) => console.log(r.response.data));
```

---

## 路径 C：进程内调用

```python
import sys
sys.path.insert(0, "<技能目录>")
from core.dispatcher import run, run_batch

envelope = run({"module": "auto", "action": "analyze", "input": "deck.pptx"})
if envelope["ok"]:
    print(envelope["data"]["keywords"][:5])

report = run_batch("./docs", "probe", kinds=["pptx", "docx", "xlsx"])
print(report["succeeded"], "/", report["total"])
```

`run()` **永不抛异常**，始终返回统一信封；调用方只需判断 `envelope["ok"]`。

---

## 路径 D：Function Calling / LangChain

静态工具定义已预生成，直接加载即可，无需运行时解析：

| 文件 | 用途 |
| --- | --- |
| `harness/openai_tools.generic.json` | OpenAI / 兼容 Chat Completions 的 `tools` 参数 |
| `harness/openai_tools.granular.json` | 同上，细粒度版 |
| `harness/langchain_tools.generic.json` | LangChain / LlamaIndex 常见结构 |
| `harness/TOOL_INDEX.md` | 工具索引（人类可读） |
| `bridges/tool_specs.json` | MCP 形态原始契约 |

### 参考执行循环

```python
import json, subprocess

TOOLS = json.load(open("harness/openai_tools.generic.json", encoding="utf-8"))["tools"]

def execute_tool(name: str, arguments: dict):
    """把模型给的 tool_call 转成本技能的请求。"""
    if name == "file_dispatch":
        req = arguments
    elif name == "file_capabilities":
        req = {"cmd": "capabilities", **arguments}
    elif name == "file_doctor":
        req = {"cmd": "doctor", **arguments}
    elif name == "file_batch":
        req = {"cmd": "batch", **arguments}
    else:
        raise ValueError(f"未知工具: {name}")

    out = subprocess.run(
        [PYTHON, "bridges/stdio_bridge.py", "--once"],
        input=json.dumps(req, ensure_ascii=False) + "\n",
        capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(out.stdout)["response"]

# responses = client.chat.completions.create(model=..., messages=..., tools=TOOLS)
# for call in responses.choices[0].message.tool_calls:
#     args = json.loads(call.function.arguments)
#     result = execute_tool(call.function.name, args)
#     messages.append({"role": "tool", "tool_call_id": call.id,
#                      "content": json.dumps(result, ensure_ascii=False)})
```

> 若要长期运行，建议保持一个常驻的 `stdio_bridge.py` 进程，而不是每次 `--once` 重启 ——
> 后者每次都要重新导入 `pandas` 等重依赖，首次调用会慢几秒。

---

## 安全边界（集成前请知悉）

| 项 | 说明 |
| --- | --- |
| 文件访问 | **只读写调用方显式传入的路径**。不做目录遍历扫描，除非显式调用 `file_batch` 并给定目录 |
| 网络访问 | **无**。全部依赖与模型均为本地/离线，不发起任何出站请求（`rapidocr-onnxruntime` 首次使用会读本地模型文件） |
| 写文件 | `edit` 动作会写文件，**默认就地保存并先生成 `<名称>_bak.<后缀>` 备份**；`transform` 默认写入 `<文件名>_out/`，不触碰原文件 |
| 子进程 | 仅 `soffice`（LibreOffice 渲染）会按需启动，且需系统已安装；未安装时相关动作返回可读错误而非静默失败 |
| 敏感信息 | 图片 `read` 会返回 EXIF，**含 GPS 时会在返回体中显式提示隐私风险** |
| 无破坏性删除 | 本技能任何动作都不删除原文件（清理空段只影响文档内部结构且有备份） |

---

## 排错清单

| 现象 | 原因与处理 |
| --- | --- |
| 宿主启不起服务器 | `command` 指向的 Python 没装依赖 → 用该解释器跑 `python scripts/doctor.py` 看缺什么 |
| 工具列表为空 | 宿主没读到 MCP 配置，或进程启动即失败 → 直接手跑 `python bridges/mcp_server.py --selftest` 验证 |
| 客户端报「非法消息」 | stdout 被污染。已做重定向兜底，若仍出现请设 `OMNI_MCP_DEBUG=1` 看 stderr |
| OCR 相关动作无文字输出 | 未装 OCR 后端。会降级为 warning 而非报错 → `pip install -r requirements-ocr.txt` |
| Excel 公式列读到 `null` | 该文件从未被 Excel/WPS 打开保存过，公式无缓存值。返回体已含 warning，并回退显示公式文本 |
| 渲染 PDF/PNG 失败 | 未安装 LibreOffice → 装 `soffice`，或改用 `op=export_images` 直接抽图 |
| 首次调用很慢 | `pandas` / `numpy` / OCR 模型加载所致。保持桥接进程常驻，或接受一次性冷启动成本 |
