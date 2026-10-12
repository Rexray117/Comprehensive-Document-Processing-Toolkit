"""环境自检脚本：一行命令看清「能不能跑、缺什么、装什么」。

用法::

    python scripts/doctor.py            # 摘要
    python scripts/doctor.py --verbose  # 完整依赖与外部工具明细
    python scripts/doctor.py --json     # 机器可读

退出码：0 = 核心依赖齐备；1 = 存在缺失的必需依赖。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.runtime import doctor  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="omni-file-toolkit 环境自检")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出完整明细")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    report = doctor(verbose=args.verbose)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["healthy"] else 1

    py = report["python"]
    print("=" * 68)
    print("omni-file-toolkit 环境自检")
    print("=" * 68)
    print(f"解释器      : {py['executable']}")
    print(f"Python 版本 : {py['version']}   (虚拟环境: {'是' if py['in_venv'] else '否'})")
    print(f"项目根目录  : {report['project_root']}")
    print()

    deps = report["dependencies"]
    print(f"依赖: 已安装 {deps['installed_count']}/{deps['total_count']}")
    if deps["required_missing"]:
        print(f"  ✗ 缺失必需依赖: {', '.join(deps['required_missing'])}")
        print(f"  → 修复命令: {report.get('fix_hint')}")
    else:
        print("  ✓ 必需依赖齐备")
    if deps["optional_missing"]:
        print(f"  ○ 未安装的可选依赖: {', '.join(deps['optional_missing'])}")
    print()

    ocr = report.get("ocr_backends", {})
    if isinstance(ocr, dict) and "available" in ocr:
        print(f"OCR 后端    : {', '.join(ocr['available']) if ocr['available'] else '无（图像文字识别不可用）'}")
        if not ocr["available"]:
            for row in ocr.get("backends", []):
                if row["name"] == "rapidocr":
                    print(f"  → 推荐安装: {row['install_cmd']}")
    print()

    tools = report.get("external_tools") or []
    available = [t for t in tools if t.get("available")]
    print(f"外部工具    : {len(available)} 个可用")
    for tool in available:
        print(f"  ✓ {tool['tool']:<12} {tool['purpose']}")
    if args.verbose:
        for tool in tools:
            if not tool.get("available"):
                print(f"  ○ {tool['tool']:<12} 未安装 —— {tool['purpose']}")
    print()

    print(f"已注册模块  : {len(report['modules'])} 个")
    for module in report["modules"]:
        print(f"  ● {module['module']:<7} {', '.join(module['actions'])}")
    print()

    if report["handler_load_errors"]:
        print("模块加载失败:")
        for name, err in report["handler_load_errors"].items():
            print(f"  ! {name}: {err.splitlines()[0]}")
        print()

    if args.verbose:
        print("-" * 68)
        print("依赖明细")
        print("-" * 68)
        for dep in report["dependencies"]:
            mark = "✓" if dep["installed"] else ("✗" if dep["required"] else "○")
            version = dep["version"] or "-"
            print(f"  {mark} {dep['package']:<34} {version:<12} {dep['purpose']}")

    print("=" * 68)
    print("结论: " + ("核心依赖齐备，可直接使用。" if report["healthy"] else "缺少必需依赖，请先安装。"))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
