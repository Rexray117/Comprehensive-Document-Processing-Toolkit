"""把本工具箱安装到 Agent 的技能目录，使其可被自动发现。

默认把技能目录放到 ``~/.workbuddy/skills/omni-file-toolkit``（用户级，全局可用）。

用法::

    python install.py                # 复制安装（默认）
    python install.py --link         # 用目录联接/软链安装（源码改完立即生效，推荐开发期使用）
    python install.py --deps         # 同时创建虚拟环境并安装依赖
    python install.py --deps --ocr   # 额外安装 OCR 后端
    python install.py --target DIR   # 自定义目标目录
    python install.py --force        # 目标已存在时，先改名备份再覆盖（不会直接删除）

安全说明：本脚本**不使用破坏性删除**。目标目录已存在时，一律改名为
``<名称>.old-<时间戳>`` 保留，用户可自行确认后清理。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
DEFAULT_SKILLS_DIR = Path.home() / ".workbuddy" / "skills"
SKIP_DIRS = {"_smoke", "__pycache__", ".venv", ".git", ".idea", ".vscode"}
SKIP_FILES = {".DS_Store"}


def default_target() -> Path:
    return DEFAULT_SKILLS_DIR / SOURCE.name


def ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in SKIP_DIRS or n in SKIP_FILES or n.endswith(".pyc")}


def make_junction(target: Path, *, dry_run: bool = False) -> str:
    """创建目录联接（Windows）或符号链接（POSIX）。"""
    if dry_run:
        return "dry-run"
    if os.name == "nt":
        # Windows 中文环境下 mklink 会输出 GBK 文本，必须显式容错解码，
        # 否则 reader 线程会抛 UnicodeDecodeError。
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(SOURCE)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not target.exists():
            detail = (result.stderr or result.stdout or "").strip()
            return f"失败: {detail}"
        return "目录联接 (mklink /J)"
    target.symlink_to(SOURCE, target_is_directory=True)
    return "符号链接 (symlink)"


def backup_existing(target: Path) -> Path:
    """把已存在的目标目录改名保留（不删除）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.with_name(f"{target.name}.old-{stamp}")
    target.rename(backup)
    return backup


def install_copy(target: Path) -> None:
    shutil.copytree(SOURCE, target, ignore=ignore, dirs_exist_ok=False)


def install_deps(target: Path, *, ocr: bool, extras: bool) -> int:
    """在目标目录创建虚拟环境并安装依赖。"""
    venv_dir = target / ".venv"
    python = Path(sys.executable)
    print(f"\n[依赖] 创建虚拟环境: {venv_dir}")
    code = subprocess.call([str(python), "-m", "venv", str(venv_dir)])
    if code != 0:
        print("虚拟环境创建失败。")
        return code
    pip_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    code = subprocess.call([str(pip_python), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    if code != 0:
        return code

    requirements = [target / "requirements.txt"]
    if extras:
        requirements.append(target / "requirements-extras.txt")
    if ocr:
        requirements.append(target / "requirements-ocr.txt")
    for req in requirements:
        if not req.exists():
            continue
        print(f"[依赖] 安装 {req.name} ...")
        code = subprocess.call([str(pip_python), "-m", "pip", "install", "-q", "-r", str(req)])
        if code != 0:
            print(f"安装 {req.name} 失败（其余功能仍可用）。")
    print(f"[依赖] 完成。解释器: {pip_python}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 omni-file-toolkit 到 Agent 技能目录")
    parser.add_argument("--target", default=str(default_target()), help="目标技能目录")
    parser.add_argument("--link", action="store_true", help="用目录联接/软链安装（开发期推荐）")
    parser.add_argument("--deps", action="store_true", help="同时创建虚拟环境并安装依赖")
    parser.add_argument("--ocr", action="store_true", help="安装 OCR 后端（配合 --deps）")
    parser.add_argument("--extras", action="store_true", help="安装增强依赖（配合 --deps）")
    parser.add_argument("--force", action="store_true", help="目标已存在时先改名备份再覆盖")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要执行的操作")
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser().resolve()
    print("=" * 66)
    print("omni-file-toolkit 安装")
    print("=" * 66)
    print(f"源目录   : {SOURCE}")
    print(f"目标目录 : {target}")
    print(f"安装方式 : {'目录联接/软链' if args.link else '复制'}")

    if target == SOURCE:
        print("\n目标目录与源目录相同，无需安装。")
        return 0

    if target.exists():
        if not args.force:
            print(f"\n目标已存在: {target}")
            print("如需覆盖请加 --force（会先改名为 .old-<时间戳> 备份，不会直接删除）。")
            return 1
        if args.dry_run:
            print("\n[dry-run] 将把已存在的目标改名为 *.old-<时间戳>")
        else:
            backup = backup_existing(target)
            print(f"\n已备份原目录 → {backup}")

    if args.dry_run:
        print("\n[dry-run] 将创建目标目录并写入文件。")
        return 0

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if args.link:
            mode = make_junction(target)
            print(f"\n已创建: {mode}")
        else:
            install_copy(target)
            file_count = sum(1 for p in target.rglob("*") if p.is_file())
            print(f"\n已复制 {file_count} 个文件。")
    except Exception as exc:
        print(f"\n安装失败: {type(exc).__name__}: {exc}")
        return 1

    if args.deps:
        code = install_deps(target, ocr=args.ocr, extras=args.extras)
        if code != 0:
            return code

    print()
    print("=" * 66)
    print("安装完成。下一步：")
    print(f"  1) 自检        : python \"{target / 'scripts' / 'doctor.py'}\"")
    print(f"  2) 端到端自测  : python \"{target / 'scripts' / 'smoke_test.py'}\"")
    print(f"  3) 查看能力    : python -m core.cli list   (在 {target} 下执行)")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
