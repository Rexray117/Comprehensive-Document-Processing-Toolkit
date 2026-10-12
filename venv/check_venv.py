# -*- coding: utf-8 -*-
"""venv 文件清单比对器 —— 在新电脑上检测缺失/损坏文件并给出补足方案。

用法（在捆绑包内，任意 Python ≥3.10 运行均可）：
    python requirements/check_venv.py              # 只报告
    python requirements/check_venv.py --verbose    # 列出全部差异明细

比对逻辑：
  venv_files.csv 是源机 venv 的全量文件快照（8998 个文件）。
  本脚本逐项核对本机 ../venv 下对应文件：缺失 / 体积不符 / 多余。
  缺失文件按所属包聚合，直接给出 pip 强制重装命令。
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

REQ_DIR = Path(__file__).resolve().parent
VENV = REQ_DIR.parent / "venv"
VERBOSE = "--verbose" in sys.argv

with (REQ_DIR / "venv_files.csv").open(encoding="utf-8-sig") as fh:
    expected = {row["relative_path"]: (int(row["size_bytes"]), row["owner"])
                for row in csv.DictReader(fh)}

missing: dict[str, list[str]] = defaultdict(list)   # owner -> [rel paths]
size_diff: list[tuple[str, int, int, str]] = []     # rel, expected, actual, owner
extra: list[str] = []
actual_paths = set()

for path in VENV.rglob("*"):
    if path.is_file():
        actual_paths.add(path.relative_to(VENV).as_posix())

for rel, (size, owner) in expected.items():
    target = VENV / rel
    if not target.exists():
        missing[owner].append(rel)
    elif target.stat().st_size != size:
        size_diff.append((rel, size, target.stat().st_size, owner))

for rel in sorted(actual_paths - set(expected)):
    extra.append(rel)

total_missing_bytes = sum(
    size for paths in missing.values() for rel in paths
    for size in [next((s for r, (s, _) in expected.items() if r == rel), 0)]
)

print("=" * 60)
print("venv 清单比对报告")
print("=" * 60)
print(f"清单文件数 : {len(expected)}")
print(f"本机实有   : {len(actual_paths)}")
print(f"缺失       : {sum(len(v) for v in missing.values())} 个文件，涉及 {len(missing)} 个包")
print(f"体积不符   : {len(size_diff)} 个文件")
print(f"多出       : {len(extra)} 个文件（通常无害，如 __pycache__ 再生成）")

if missing:
    print("\n-- 缺失文件按包聚合（补足命令）--")
    owners = sorted(missing)
    pkgs = [o.split("==")[0] for o in owners if o not in ("venv-base", "unattributed")]
    if pkgs:
        print("  强制重装受影响包：")
        print(f"  python -m pip install --force-reinstall {' '.join(pkgs)}")
    for owner in owners:
        paths = missing[owner]
        print(f"  [{owner}] 缺 {len(paths)} 个，如: {paths[:3]}")

if size_diff:
    print("\n-- 体积不符（前 10）--")
    for rel, exp, act, owner in size_diff[:10]:
        print(f"  {rel}: 清单 {exp} B / 实际 {act} B ({owner})")

if VERBOSE:
    print("\n-- 全部缺失明细 --")
    for owner, paths in missing.items():
        for rel in paths:
            print(f"  {rel}")
    print("\n-- 多出文件明细 --")
    for rel in extra:
        print(f"  {rel}")

print("\n结论:", "环境完整" if not missing and not size_diff else "需要按上方命令补足")
sys.exit(0 if not missing and not size_diff else 1)
