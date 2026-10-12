#!/usr/bin/env python
"""打包发布：把开发态源码封装成可直接集成给 Agent / harness 的技能包。

产出
----
1. 一个**独立目录** ``<工作区>/综合性办公文件处理技能/``，内含：
   - ``SKILL.md``（Agent 入口）、``MANIFEST.json``（机器可读清单）
   - ``VERSION``、``CHANGELOG.md``、``LICENSE``
   - ``requirements.txt`` / ``requirements-ocr.txt`` / ``requirements-extras.txt``
     / ``requirements.lock.txt``（按构建时实际安装版本锁定）
   - ``core/`` ``handlers/`` ``ocr/`` ``bridges/`` ``harness/`` ``scripts/``
     ``references/`` ``examples/``
2. 一个发布压缩包 ``<工作区>/综合性办公文件处理技能-v0.1.zip``

安全约定
--------
**不做破坏性删除**。目标目录若已存在，一律改名为 ``<名称>.old-<时间戳>`` 后保留，
用户可自行确认再清理。

用法
----
    python scripts/pack_release.py                 # 打包 + 验证
    python scripts/pack_release.py --skip-verify   # 只打包
    python scripts/pack_release.py --no-zip        # 不生成压缩包
    python scripts/pack_release.py --dest DIR      # 自定义目标目录
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import version  # noqa: E402

DEST_NAME = version.NAME  # 「综合性办公文件处理技能」
EXCLUDE_DIRS = {"_smoke", "__pycache__", ".venv", ".git", ".idea", ".vscode", "dist", ".pytest_cache"}
EXCLUDE_FILES = {".DS_Store", ".gitignore", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".bak", ".log", ".old"}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def log(message: str) -> None:
    print(message, flush=True)


def run(cmd: list[str], *, cwd: Path, capture: bool = True,
        env_extra: Optional[dict[str, str]] = None) -> tuple[int, str]:
    """执行子进程。显式指定 utf-8 + replace，避免 Windows 中文环境下的 GBK 解码崩溃。"""
    env = dict(os.environ)
    # 关键：禁止在目标目录内落 __pycache__，避免发布包里混入字节码缓存
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def ignore_factory():
    def _ignore(_dir: str, names: list[str]) -> set[str]:
        dropped: set[str] = set()
        for name in names:
            if name in EXCLUDE_DIRS or name in EXCLUDE_FILES:
                dropped.add(name)
                continue
            if any(name.endswith(suffix) for suffix in EXCLUDE_SUFFIXES):
                dropped.add(name)
        return dropped

    return _ignore


def backup_existing(target: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.old-{stamp}")
    target.rename(backup)
    return backup


def cleanup_artifacts(dest: Path) -> list[str]:
    """清理验证过程在目标目录里产生的临时产物。

    只删除**本脚本刚刚创建的目标目录内**、且由验证流程生成的固定名称对象：
    ``_smoke/``（冒烟测试工作目录）、``__pycache__/``、``*.pyc``。
    不做任何通配符式的广度删除。
    """
    removed: list[str] = []

    smoke = dest / "_smoke"
    if smoke.is_dir():
        shutil.rmtree(smoke, ignore_errors=True)
        removed.append("_smoke/")

    for cache in sorted(dest.rglob("__pycache__")):
        if cache.is_dir() and dest in cache.parents:
            shutil.rmtree(cache, ignore_errors=True)
            removed.append(str(cache.relative_to(dest)))

    for pattern in ("*.pyc", "*.pyo"):
        for stray in sorted(dest.rglob(pattern)):
            if dest in stray.parents and stray.is_file():
                stray.unlink()
                removed.append(str(stray.relative_to(dest)))

    return removed


def pinned_requirements(relative: str, wheel_env: dict[str, Optional[str]]) -> tuple[str, list[str]]:
    """把 requirements 文件里的包名替换为构建时实际安装的版本。"""
    source = _ROOT / relative
    if not source.exists():
        return "", []
    lines: list[str] = [
        f"# 由 scripts/pack_release.py 于 {version.RELEASE_DATE} 按构建环境实际版本锁定",
        f"# 源文件: {relative}",
        "",
    ]
    resolved: list[str] = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            lines.append(raw)
            continue
        name = line.split(">=")[0].split("==")[0].split("[")[0].strip()
        installed = wheel_env.get(name.lower()) or wheel_env.get(name)
        if installed:
            lines.append(f"{name}=={installed}")
            resolved.append(f"{name}=={installed}")
        else:
            lines.append(line)
            resolved.append(line)
    return "\n".join(lines) + "\n", resolved


def collect_installed(names: list[str]) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for name in names:
        try:
            out[name] = md.version(name)
        except Exception:
            try:
                out[name.lower()] = md.version(name.lower())
            except Exception:
                out[name] = None
    return out


# ---------------------------------------------------------------------------
# MANIFEST.json
# ---------------------------------------------------------------------------
def build_manifest(dest: Path, *, pinned: dict[str, list[str]],
                   verification: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from bridges import spec
    from core.registry import all_handlers, list_modules

    tools = spec.tool_summary()
    modules = []
    for entry in list_modules():
        handler = all_handlers()[entry["module"]]
        cap = handler.capabilities()
        modules.append(
            {
                "module": entry["module"],
                "display_name": cap["display_name"],
                "description": cap["description"],
                "extensions": cap["extensions"],
                "kinds": cap["kinds"],
                "actions": [s["action"] for s in cap["actions"]],
                "action_count": len(cap["actions"]),
            }
        )

    resources = [
        relative
        for relative, _ in __import__("bridges.mcp_server", fromlist=["RESOURCE_FILES"]).RESOURCE_FILES
        if relative == "MANIFEST.json" or (dest / relative).exists()
    ]

    manifest: dict[str, Any] = {
        "name": version.NAME,
        "slug": version.SLUG,
        "version": version.VERSION,
        "release": version.RELEASE,
        "release_date": version.RELEASE_DATE,
        "skill_standard": version.SKILL_STANDARD,
        "license": "Apache-2.0",
        "summary": version.SUMMARY,
        "min_python": ".".join(str(p) for p in version.MIN_PYTHON),
        "built_with_python": sys.version.split()[0],
        "entrypoints": {
            "skill_md": "SKILL.md",
            "mcp_server": {
                "command": sys.executable,
                "args": ["bridges/mcp_server.py"],
                "granular_args": ["bridges/mcp_server.py", "--granular"],
                "protocol": "mcp-stdio",
                "protocol_versions": list(version.MCP_PROTOCOL_VERSIONS),
                "note": "command 必须是安装了依赖的 Python 绝对路径；换机器部署时需替换。",
            },
            "stdio_bridge": {
                "command": sys.executable,
                "args": ["bridges/stdio_bridge.py"],
                "protocol": "line-delimited-json",
                "note": "stdin 每行一个请求，stdout 每行一个响应；支持 --once 单次模式。",
            },
            "python_api": {"import": "core.dispatcher", "callable": "run", "batch": "run_batch"},
            "cli": {"command": sys.executable, "args": ["-m", "core.cli"], "cwd": "."},
            "tools_spec": "bridges/tool_specs.json",
            "openai_tools": "harness/openai_tools.generic.json",
            "langchain_tools": "harness/langchain_tools.generic.json",
        },
        "tools": tools,
        "modules": modules,
        "module_count": len(modules),
        "action_count": sum(m["action_count"] for m in modules),
        "dependencies": {
            "python": sys.version.split()[0],
            "lock_file": "requirements.lock.txt",
            "core": pinned.get("core", []),
            "ocr": pinned.get("ocr", []),
            "extras": pinned.get("extras", []),
            "install": [
                f"{Path(sys.executable)} -m pip install -r requirements.txt",
                f"{Path(sys.executable)} -m pip install -r requirements-ocr.txt   # 可选：OCR",
            ],
        },
        "mcp_resources": resources,
        "security": {
            "network_access": "none",
            "file_scope": "仅调用方显式传入的路径；无隐式目录遍历",
            "write_policy": "edit 默认就地保存并先生成 _bak 备份；transform 输出到 <文件名>_out/",
            "destructive_delete": "无。任何动作都不删除原文件",
        },
        "verification": verification or {"status": "skipped"},
    }
    return manifest


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=f"打包发布 {version.NAME} {version.RELEASE}")
    parser.add_argument("--dest", default=str(_ROOT.parent / DEST_NAME), help="目标目录")
    parser.add_argument("--skip-verify", action="store_true", help="跳过打包后的验证")
    parser.add_argument("--no-zip", action="store_true", help="不生成 zip 压缩包")
    parser.add_argument("--keep-old", action="store_true", help="目标已存在时保留旧目录（默认同样保留，仅提示）")
    args = parser.parse_args(argv)

    dest = Path(args.dest).resolve()
    log("=" * 74)
    log(f"打包发布：{version.NAME}  {version.RELEASE}  ({version.SLUG} {version.VERSION})")
    log("=" * 74)
    log(f"源目录  : {_ROOT}")
    log(f"目标目录: {dest}")

    if dest == _ROOT:
        log("目标目录与源目录相同，已中止。")
        return 1
    if _ROOT in dest.parents and dest != _ROOT:
        # 允许（放到工作区里），但提醒不要嵌套进源目录内部造成递归复制
        if dest.parent == _ROOT:
            log("目标目录位于源目录内部会造成递归复制，已中止。请改用 --dest 指向源目录之外。")
            return 1

    # ---------- 1. 处理已存在的目标 ----------
    if dest.exists():
        backup = backup_existing(dest)
        log(f"\n[1/6] 目标已存在 → 已改名备份保留：{backup.name}")

    # ---------- 2. 复制源码树 ----------
    log("\n[2/6] 复制源码树（排除 _smoke / __pycache__ / .venv / *.pyc 等）")
    shutil.copytree(_ROOT, dest, ignore=ignore_factory())
    file_count = sum(1 for p in dest.rglob("*") if p.is_file())
    total_bytes = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    log(f"      已复制 {file_count} 个文件，{total_bytes / 1024:.1f} KB")

    # ---------- 3. 生成版本与依赖锁定文件 ----------
    log("\n[3/6] 生成 VERSION / LICENSE / requirements.lock.txt")
    (dest / "VERSION").write_text(f"{version.VERSION}\n", encoding="utf-8")

    lock_names = [
        "python-pptx", "python-docx", "openpyxl", "pandas", "numpy", "Pillow",
        "beautifulsoup4", "lxml", "markdownify", "html2text", "filetype", "charset-normalizer",
    ]
    ocr_names = ["rapidocr-onnxruntime", "onnxruntime", "pytesseract", "easyocr"]
    extras_names = ["mammoth", "opencc-python-reimplemented", "XlsxWriter", "PyMuPDF"]
    env = collect_installed(lock_names + ocr_names + extras_names)

    pinned: dict[str, list[str]] = {}
    for key, relative in (("core", "requirements.txt"), ("ocr", "requirements-ocr.txt"),
                          ("extras", "requirements-extras.txt")):
        text, resolved = pinned_requirements(relative, env)
        pinned[key] = resolved
        if text:
            target = dest / ("requirements.lock.txt" if key == "core" else f"requirements-{key}.lock.txt")
            target.write_text(text, encoding="utf-8")
    log(f"      lock 文件已按构建环境实际版本生成（Python {sys.version.split()[0]}）")

    # ---------- 4. 在目标目录内重新生成派生文档 ----------
    log("\n[4/6] 在目标目录内重新生成 API 手册与工具契约")
    for script in ("gen_api_doc.py", "gen_tool_specs.py"):
        code, output = run([sys.executable, str(dest / "scripts" / script)], cwd=dest)
        first = output.strip().splitlines()[0] if output.strip() else "(无输出)"
        log(f"      {'✓' if code == 0 else '✗'} {script}: {first}")
        if code != 0:
            log(output[-1500:])
            return 1

    # ---------- 5. 验证 ----------
    verification: dict[str, Any] = {"status": "skipped"}
    if not args.skip_verify:
        log("\n[5/6] 验证（doctor / smoke_test / MCP 自检）")
        verification = {"status": "passed", "checks": {}}
        # 冒烟测试的工作目录放到系统临时区：从源头避免在发布包里留下 _smoke/ 与样例文件
        smoke_dir = Path(tempfile.mkdtemp(prefix="omni_pack_verify_"))
        verification["smoke_workdir"] = str(smoke_dir)
        try:
            code, output = run([sys.executable, str(dest / "scripts" / "doctor.py")], cwd=dest)
            healthy = '"healthy": true' in output or "核心依赖齐备" in output
            verification["checks"]["doctor"] = {"ok": healthy, "exit": code}
            log(f"      {'✓' if healthy else '✗'} doctor：依赖{'齐备' if healthy else '有缺失'}")

            code, output = run(
                [sys.executable, str(dest / "scripts" / "smoke_test.py"), "--ocr",
                 "--workdir", str(smoke_dir)],
                cwd=dest,
            )
            summary = next((line for line in output.splitlines() if "总计" in line), output.strip()[-200:])
            passed = code == 0 and "失败 0" in summary
            verification["checks"]["smoke_test"] = {"ok": passed, "exit": code, "summary": summary.strip()}
            log(f"      {'✓' if passed else '✗'} smoke_test：{summary.strip()}")

            for mode, extra in (("generic", []), ("granular", ["--granular"])):
                code, output = run(
                    [sys.executable, str(dest / "bridges" / "mcp_server.py"), *extra, "--selftest"],
                    cwd=dest,
                )
                line = next((x for x in output.splitlines() if x.startswith("通过")), "")
                ok = code == 0 and "FAIL" not in output
                verification["checks"][f"mcp_selftest_{mode}"] = {"ok": ok, "exit": code,
                                                                  "summary": line.strip()}
                log(f"      {'✓' if ok else '✗'} MCP 自检（{mode}）：{line.strip()}")
        finally:
            shutil.rmtree(smoke_dir, ignore_errors=True)

        verification["failed"] = [k for k, v in verification["checks"].items() if not v["ok"]]
        if verification["failed"]:
            verification["status"] = "failed"
        log(f"      总体：{'全部通过' if not verification['failed'] else '存在失败项 ' + str(verification['failed'])}")
    else:
        log("\n[5/6] 已跳过验证（--skip-verify）")

    # ---------- 6. 清理验证残留 → 生成 MANIFEST.json 与压缩包 ----------
    removed = cleanup_artifacts(dest)
    if removed:
        log(f"\n[5.5/6] 清理验证残留：{', '.join(removed[:6])}"
            + (f" 等 {len(removed)} 项" if len(removed) > 6 else ""))

    log("\n[6/6] 生成 MANIFEST.json" + ("" if args.no_zip else " 与发布压缩包"))
    manifest = build_manifest(dest, pinned=pinned, verification=verification)
    (dest / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"      MANIFEST.json：{manifest['module_count']} 个模块 / {manifest['action_count']} 个动作 / "
        f"通用工具 {manifest['tools']['generic_count']} 个 + 细粒度 {manifest['tools']['granular_count']} 个")

    zip_path: Optional[Path] = None
    if not args.no_zip:
        zip_path = dest.parent / f"{DEST_NAME}-{version.RELEASE}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(dest.rglob("*")):
                if path.is_file():
                    zf.write(path, Path(DEST_NAME) / path.relative_to(dest))
        log(f"      {zip_path.name}（{zip_path.stat().st_size / 1024:.1f} KB，"
            f"{sum(1 for _ in zipfile.ZipFile(zip_path).infolist())} 个条目）")

    # ---------- 汇总 ----------
    final_files = sum(1 for p in dest.rglob("*") if p.is_file())
    log("\n" + "=" * 74)
    log(f"打包完成：{dest}")
    log(f"  文件数：{final_files}")
    if zip_path:
        log(f"  压缩包：{zip_path}")
    log("")
    log("  集成下一步：")
    log(f"    1) 装依赖  : {sys.executable} -m pip install -r \"{dest / 'requirements.txt'}\"")
    log(f"    2) 自检    : {sys.executable} \"{dest / 'scripts' / 'doctor.py'}\"")
    log(f"    3) MCP 配置: {sys.executable} \"{dest / 'bridges' / 'mcp_server.py'}\" --print-config")
    log(f"    4) 集成指南: {dest / 'bridges' / 'INTEGRATION.md'}")
    log("=" * 74)
    return 0 if verification.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
