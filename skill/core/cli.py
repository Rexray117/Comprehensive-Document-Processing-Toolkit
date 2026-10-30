"""统一命令行入口。

用法速查
--------
    python -m core.cli list                          # 列出模块与动作
    python -m core.cli cap pptx                      # 查看某模块能力契约
    python -m core.cli doctor -v                     # 环境 / 依赖自检
    python -m core.cli run -m auto -a read -i deck.pptx --json
    python -m core.cli run -a read -i deck.pptx -p include_notes=false
    python -m core.cli read deck.pptx                # 简写形式
    python -m core.cli batch -a analyze -i ./docs --kind pptx,docx --json
    python -m core.cli ask '{"module":"xlsx","action":"read","input":"a.xlsx"}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

# --- 允许 `python core/cli.py` 直接运行（把项目根加入 sys.path）-----------
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.dispatcher import Request, run_batch, run_json, run_request  # noqa: E402
from core.result import Response  # noqa: E402
from core.runtime import doctor  # noqa: E402

KNOWN_COMMANDS = {"list", "cap", "doctor", "run", "batch", "ask", "help"}


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _parse_opts(pairs: list[str]) -> dict[str, Any]:
    """``--opt key=value`` → dict，值自动尝试 JSON 解析。"""
    options: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            options[pair] = True
            continue
        key, raw = pair.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        try:
            options[key] = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            options[key] = raw
    return options


def build_parser() -> argparse.ArgumentParser:
    # 公共参数：用 SUPPRESS 作为默认值，避免子解析器的默认值覆盖父解析器已设置的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="以 JSON 输出（机器可读）")

    parser = argparse.ArgumentParser(
        prog="omni-file-toolkit",
        description="综合性文件处理工具箱：PPTX / HTML / 图片 / DOCX / XLSX 统一调度。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", parents=[common], help="列出所有已注册模块及其动作")

    cap = sub.add_parser("cap", parents=[common], help="输出模块能力契约（含参数说明）")
    cap.add_argument("module", nargs="?", help="模块名；省略则输出全部")

    doc = sub.add_parser("doctor", parents=[common], help="环境与依赖自检")
    doc.add_argument("-v", "--verbose", action="store_true", help="输出完整依赖/工具明细")

    run = sub.add_parser("run", parents=[common], help="执行单个动作")
    run.add_argument("-m", "--module", default="auto", help="模块名，默认 auto 自动识别")
    run.add_argument("-a", "--action", required=True, help="动作名")
    run.add_argument("-i", "--input", required=True, help="输入文件路径")
    run.add_argument("-o", "--output", help="输出文件或目录")
    run.add_argument("-p", "--opt", action="append", default=[], metavar="K=V",
                     help="动作参数，可重复，如 -p top_n=15 -p ocr=true")

    batch = sub.add_parser("batch", parents=[common], help="对目录批量执行同一动作")
    batch.add_argument("-m", "--module", default="auto")
    batch.add_argument("-a", "--action", required=True)
    batch.add_argument("-i", "--input", required=True, help="目录或文件")
    batch.add_argument("-o", "--output", help="输出目录")
    batch.add_argument("-p", "--opt", action="append", default=[], metavar="K=V")
    batch.add_argument("--kind", help="按类型过滤，如 pptx,docx")
    batch.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    batch.add_argument("--limit", type=int, help="最多处理 N 个文件")

    ask = sub.add_parser("ask", parents=[common], help="直接传入 JSON 请求体执行")
    ask.add_argument("payload", help="JSON 字符串，或 @文件路径")

    return parser


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _emit(payload: dict[str, Any], as_json: bool, *, ok: bool = True) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if payload.get("batch"):
        print(f"[批量] 共 {payload['total']} 个，成功 {payload['succeeded']}，失败 {payload['failed']}")
        for item in payload.get("results", []):
            print("  " + Response(**item).summary().splitlines()[0] if item.get("module") else "  " + str(item)[:160])
        if payload.get("artifacts"):
            print(f"  产物合计 {len(payload['artifacts'])} 个")
        return
    response = Response(
        ok=payload.get("ok", ok),
        module=payload.get("module", ""),
        action=payload.get("action", ""),
        input=payload.get("input"),
        output=payload.get("output"),
        elapsed_ms=payload.get("elapsed_ms", 0),
        data=payload.get("data", {}),
        artifacts=payload.get("artifacts", []),
        warnings=payload.get("warnings", []),
        error=payload.get("error"),
    )
    print(response.summary())
    if response.ok and response.data:
        preview = json.dumps(response.data, ensure_ascii=False, indent=2, default=str)
        if len(preview) > 3000:
            preview = preview[:3000] + "\n  ... (已截断，完整内容请加 --json)"
        print("  data:")
        for line in preview.splitlines():
            print("    " + line)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # ---------- 简写：`cli read deck.pptx` ≡ `cli run -a read -i deck.pptx` ----------
    head = raw[0] if raw else None
    if head and head not in KNOWN_COMMANDS and not head.startswith("-"):
        raw = ["run", "--action", head] + (["--input"] + raw[1:] if len(raw) > 1 else [])

    parser = build_parser()
    args = parser.parse_args(raw)

    if args.command in (None, "help"):
        parser.print_help()
        return 0

    global_json = getattr(args, "json", False)

    # ---------- list ----------
    if args.command == "list":
        from core.registry import list_modules, load_errors

        modules = list_modules()
        if global_json:
            print(json.dumps({"modules": modules, "load_errors": load_errors()},
                             ensure_ascii=False, indent=2))
            return 0
        print(f"已注册模块 {len(modules)} 个:\n")
        for m in modules:
            print(f"  ● {m['module']:<7} {m['display_name']}  v{m['version']}")
            print(f"    类型: {', '.join(m['extensions'])}")
            print(f"    动作: {', '.join(m['actions'])}")
            missing = m.get("missing_required_deps") or []
            if missing:
                print(f"    ✗ 硬依赖缺失: {', '.join(missing)}  ← 该模块动作会失败，先 pip install")
            if m.get("optional_deps"):
                print(f"    可选依赖: {', '.join(m['optional_deps'].values())}")
            print()
        errs = load_errors()
        if errs:
            print("加载失败的模块:")
            for name, err in errs.items():
                print(f"  ! {name}: {err.splitlines()[0]}")
        return 0

    # ---------- cap ----------
    if args.command == "cap":
        from core.registry import all_handlers

        handlers = all_handlers()
        if getattr(args, "module", None):
            handler = handlers.get(args.module.lower())
            if handler is None:
                print(f"未找到模块: {args.module}（可用: {', '.join(sorted(handlers))}）",
                      file=sys.stderr)
                return 2
            payload: Any = handler.capabilities()
        else:
            payload = [h.capabilities() for h in handlers.values()]
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    # ---------- doctor ----------
    if args.command == "doctor":
        report = doctor(verbose=getattr(args, "verbose", False))
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report.get("healthy") else 1

    # ---------- run ----------
    if args.command == "run":
        request = Request(
            action=args.action,
            input=args.input,
            module=args.module,
            output=args.output,
            options=_parse_opts(args.opt),
        )
        payload = run_request(request)
        _emit(payload, global_json)
        return 0 if payload.get("ok") else 1

    # ---------- batch ----------
    if args.command == "batch":
        kinds = None
        if getattr(args, "kind", None):
            kinds = [k.strip().lower() for k in args.kind.split(",") if k.strip()]
        payload = run_batch(
            args.input,
            args.action,
            module=args.module,
            options=_parse_opts(args.opt),
            output=args.output,
            recursive=not getattr(args, "no_recursive", False),
            kinds=kinds,
            limit=getattr(args, "limit", None),
        )
        _emit(payload, global_json)
        return 0 if payload.get("ok") else 1

    # ---------- ask ----------
    if args.command == "ask":
        text = args.payload
        if text.startswith("@"):
            text = Path(text[1:]).read_text(encoding="utf-8")
        payload = run_json(text)
        _emit(payload, True)
        return 0 if payload.get("ok") else 1

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
