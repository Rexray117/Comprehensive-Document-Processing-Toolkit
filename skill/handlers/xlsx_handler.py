"""XLSX 工作表处理模块（openpyxl 结构层 + pandas 分析层）。

双引擎分工
----------
* **openpyxl** —— 负责「结构真相」：公式、合并单元格、列宽、数字格式、条件格式、图表。
* **pandas**  —— 负责「数据洞察」：描述统计、缺失、去重、分组聚合、相关性、离群点。

一个常见坑：``data_only=True`` 读到的公式单元格只有在 Excel/WPS 打开并保存过之后
才有缓存值，否则是 ``None``。本模块会同时加载两份工作簿（公式版 + 值版），
优先取缓存值、缺失时回退为公式文本，并显式给出 warning，避免误判为空数据。

独立运行
--------
    python handlers/xlsx_handler.py read    book.xlsx -p sheet=Sheet1
    python handlers/xlsx_handler.py analyze book.xlsx -p top_n=30
    python handlers/xlsx_handler.py edit    book.xlsx -p op=set_cell -p cell=B2 -p value=100
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ActionError, InputError, ParamError, require  # noqa: E402
from core.utils import col_index, col_letter, human_bytes, slugify, to_json  # noqa: E402

MODULE = "xlsx"

_RANGE_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)(?::\$?([A-Za-z]{1,3})\$?(\d+))?$")


def _parse_range(spec: str) -> tuple[int, int, int, int]:
    """``"A1:C10"`` → ``(min_col, min_row, max_col, max_row)``（0 基列、1 基行）。"""
    m = _RANGE_RE.match(str(spec).strip())
    if not m:
        raise ParamError(f"非法单元格区间: {spec!r}（示例 A1、B2:C10）")
    c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4) or m.group(2))
    lo_c, hi_c = sorted((col_index(c1), col_index(c2)))
    lo_r, hi_r = sorted((r1, r2))
    return lo_c, lo_r, hi_c, hi_r


def _cell_text(cell: Any) -> str:
    return f"{col_letter(cell.column - 1)}{cell.row}"


class XlsxHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "XLSX 工作表"
    description = (
        "处理 .xlsx 工作簿：读取值/公式/合并单元格/数字格式，做描述统计与分组聚合，"
        "导出 CSV/JSON/Markdown/HTML，写入单元格与公式、追加行、设置格式、插入图表。"
    )
    extensions = (".xlsx", ".xlsm", ".xltx")
    kinds = ("xlsx",)
    required_deps = {"pandas": "pandas"}  # 描述统计/聚合依赖 pandas，缺了这些动作不可用
    optional_deps = {"xlsxwriter": "XlsxWriter"}

    # ======================================================== probe
    @action(
        "probe",
        summary="工作簿概览：工作表、尺寸、公式/图表/合并/冻结、命名区域",
        params={"deep": "true 时统计每张表的公式与合并单元格数量（默认 true）"},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        openpyxl = require("openpyxl", "openpyxl")
        path = ctx.input_path()
        wb = openpyxl.load_workbook(str(path), read_only=False, data_only=False)

        sheets: list[dict[str, Any]] = []
        total_formulas = 0
        for ws in wb.worksheets:
            entry: dict[str, Any] = {
                "name": ws.title,
                "state": ws.sheet_state,
                "max_row": ws.max_row,
                "max_column": ws.max_column,
                "dimensions": str(ws.dimensions),
                "merged_cells": len(ws.merged_cells.ranges),
                "freeze_panes": ws.freeze_panes,
                "has_charts": bool(getattr(ws, "_charts", [])),
                "chart_count": len(getattr(ws, "_charts", []) or []),
                "has_autofilter": ws.auto_filter.ref is not None,
                "hidden": ws.sheet_state != "visible",
            }
            if ctx.flag("deep", True):
                formulas = 0
                non_empty = 0
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value is None:
                            continue
                        non_empty += 1
                        if isinstance(cell.value, str) and cell.value.startswith("="):
                            formulas += 1
                entry["non_empty_cells"] = non_empty
                entry["formula_cells"] = formulas
                total_formulas += formulas
                entry["conditional_formatting_ranges"] = len(list(ws.conditional_formatting))
                entry["data_validation_count"] = len(ws.data_validations.dataValidation)
            sheets.append(entry)

        wb.close()
        return {
            "file": path.name,
            "size": path.stat().st_size,
            "size_human": human_bytes(path.stat().st_size),
            "sheet_count": len(sheets),
            "sheet_names": [s["name"] for s in sheets],
            "formula_cells_total": total_formulas,
            "defined_names": sorted(wb.defined_names.keys()) if hasattr(wb.defined_names, "keys") else str(wb.defined_names),
            "sheets": sheets,
        }

    # ======================================================== read
    @action(
        "read",
        summary="读取表格数据：值 + 公式 + 表头 + 记录 + 合并单元格",
        params={
            "sheet": "工作表名或索引（默认第一张）；all 表示全部",
            "header_row": "表头所在行（默认 1；0 表示无表头）",
            "max_rows": "每张表最多返回的数据行数（默认 500）",
            "max_cols": "每张表最多返回的列数（默认 100）",
            "mode": "values（默认，优先缓存值）| formulas | both",
            "include_formulas": "是否附带公式明细（默认 true）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        openpyxl = require("openpyxl", "openpyxl")
        path = ctx.input_path()
        wb_formula = openpyxl.load_workbook(str(path), data_only=False, read_only=False)
        wb_value = openpyxl.load_workbook(str(path), data_only=True, read_only=False)

        names = self._select_sheets(wb_formula, ctx.text("sheet", ""))
        header_row = ctx.bounded_int("header_row", 1, low=0, high=1000)
        max_rows = ctx.bounded_int("max_rows", 500, low=1, high=200_000)
        max_cols = ctx.bounded_int("max_cols", 100, low=1, high=2000)
        mode = (ctx.text("mode", "values") or "values").lower()
        include_formulas = ctx.flag("include_formulas", True)

        sheets: list[dict[str, Any]] = []
        missing_cache = 0
        for name in names:
            ws_v = wb_value[name]
            ws_f = wb_formula[name]
            sheets.append(
                self._read_sheet(
                    ws_v, ws_f, header_row=header_row, max_rows=max_rows,
                    max_cols=max_cols, mode=mode, include_formulas=include_formulas,
                    counter=lambda n=1: None,
                )
            )
            # 统计缺少缓存值的公式单元格
            for row in ws_f.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        if ws_v[cell.coordinate].value is None:
                            missing_cache += 1

        wb_formula.close()
        wb_value.close()
        if missing_cache:
            ctx.warn(
                f"{missing_cache} 个公式单元格没有缓存值（该文件可能从未被 Excel/WPS 打开保存过），"
                "已回退显示公式文本"
            )
        return {
            "sheet_count": len(names),
            "sheet_names": names,
            "formula_without_cache": missing_cache,
            "sheets": sheets,
        }

    # ======================================================== analyze
    @action(
        "analyze",
        summary="数据分析：描述统计、缺失率、唯一值、重复行、相关性、分组聚合、离群点",
        params={
            "sheet": "工作表名或索引（默认第一张）",
            "header_row": "表头所在行（默认 1）",
            "by": "分组聚合的分组列名（可选）",
            "agg": "分组聚合方式，如 sum,mean,count（默认 sum,mean,count）",
            "top_n": "每列 Top 值个数（默认 5）",
            "corr": "是否输出数值列相关性（默认 true）",
            "sample_rows": "预览行数（默认 8）",
        },
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        pandas = require("pandas", "pandas")
        openpyxl = require("openpyxl", "openpyxl")

        path = ctx.input_path()
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        names = self._select_sheets(wb, ctx.text("sheet", ""))
        name = names[0]
        ws = wb[name]
        header_row = ctx.bounded_int("header_row", 1, low=1, high=1000)
        rows = list(ws.iter_rows(values_only=True))
        wb.close()

        if not rows:
            return {"sheet": name, "empty": True, "issues": ["工作表为空"]}

        header = [("" if v is None else str(v)).strip() or f"列{i + 1}" for i, v in enumerate(rows[header_row - 1])]
        data = rows[header_row:]
        width = len(header)
        normalised = [list(r[:width]) + [None] * max(0, width - len(r)) for r in data]
        df = pandas.DataFrame(normalised, columns=self._dedupe(header))

        top_n = ctx.bounded_int("top_n", 5, low=1, high=50)
        sample_rows = ctx.bounded_int("sample_rows", 8, low=0, high=100)

        columns: list[dict[str, Any]] = []
        for col in df.columns:
            series = df[col]
            non_null = series.dropna()
            info: dict[str, Any] = {
                "column": str(col),
                "dtype": str(series.dtype),
                "non_null": int(non_null.shape[0]),
                "null_count": int(series.isna().sum()),
                "null_ratio": round(float(series.isna().mean()), 4),
                "unique": int(non_null.nunique()),
            }
            numeric = pandas.to_numeric(non_null, errors="coerce").dropna()
            if not numeric.empty and numeric.shape[0] >= max(3, len(non_null) * 0.6):
                info.update(
                    {
                        "inferred_numeric": True,
                        "min": round(float(numeric.min()), 6),
                        "max": round(float(numeric.max()), 6),
                        "mean": round(float(numeric.mean()), 6),
                        "median": round(float(numeric.median()), 6),
                        "std": round(float(numeric.std(ddof=0)), 6),
                        "sum": round(float(numeric.sum()), 6),
                    }
                )
                q1, q3 = numeric.quantile(0.25), numeric.quantile(0.75)
                iqr = q3 - q1
                if iqr > 0:
                    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                    outliers = numeric[(numeric < low) | (numeric > high)]
                    if not outliers.empty:
                        info["outlier_count"] = int(outliers.shape[0])
                        info["outlier_range"] = [round(float(low), 4), round(float(high), 4)]
            else:
                info["top_values"] = [
                    {"value": str(v), "count": int(c)}
                    for v, c in non_null.astype(str).value_counts().head(top_n).items()
                ]
            if info["unique"] == 1 and info["non_null"]:
                info["constant"] = True
            columns.append(info)

        numeric_df = df.apply(pandas.to_numeric, errors="coerce")
        numeric_cols = [c for c in numeric_df.columns if numeric_df[c].notna().sum() >= 2]
        integrity: dict[str, Any] = {}
        if ctx.flag("corr", True) and len(numeric_cols) >= 2:
            corr = numeric_df[numeric_cols].corr(numeric_only=True).round(3)
            pairs = []
            for i, a in enumerate(numeric_cols):
                for b in numeric_cols[i + 1:]:
                    val = corr.loc[a, b]
                    if pandas.notna(val) and abs(float(val)) >= 0.5:
                        pairs.append({"a": str(a), "b": str(b), "r": float(val)})
            integrity["strong_correlations"] = sorted(pairs, key=lambda p: -abs(p["r"]))[:15]

        grouped = None
        by = ctx.text("by", "")
        if by:
            if by not in df.columns:
                raise ParamError(f"分组列不存在: {by}", hint=f"可用列: {list(df.columns)[:30]}")
            aggs = [a.strip() for a in ctx.text("agg", "sum,mean,count").split(",") if a.strip()]
            try:
                grouped_frame = df.groupby(by, dropna=False)
                if numeric_cols:
                    grouped = (
                        grouped_frame.agg({c: aggs for c in numeric_cols})
                        .round(4)
                        .reset_index()
                        .head(200)
                    )
                else:
                    grouped = (
                        grouped_frame.size().reset_index(name="count").head(200)
                    )
                grouped = grouped.astype(str).to_dict(orient="records")
            except Exception as exc:
                ctx.warn(f"分组聚合失败: {exc}")
                grouped = None

        issues: list[str] = []
        for info in columns:
            if info["null_ratio"] > 0.5 and info["non_null"]:
                issues.append(f"列「{info['column']}」缺失率 {info['null_ratio']:.0%}，分析结论需谨慎")
            if info.get("constant"):
                issues.append(f"列「{info['column']}」为常量列，对分析无信息量")
            if info["dtype"] == "object" and info.get("inferred_numeric"):
                issues.append(f"列「{info['column']}」数值被存为文本，建议转为数值型以便公式计算")
        duplicates = int(df.duplicated().sum())
        if duplicates:
            issues.append(f"存在 {duplicates} 行完全重复的数据")

        preview = df.head(sample_rows).astype(str).to_dict(orient="records") if sample_rows else []
        return {
            "sheet": name,
            "rows": int(df.shape[0]),
            "columns_count": int(df.shape[1]),
            "columns": columns,
            "duplicate_rows": duplicates,
            "preview": preview,
            "correlation": integrity,
            "groupby": {"by": by, "agg": ctx.text("agg", "sum,mean,count"), "result": grouped} if by else None,
            "issues": issues,
        }

    # ======================================================== transform
    @action(
        "transform",
        summary="导出与转换：CSV / JSON / Markdown / HTML / 筛选 / 排序 / 合并 / 透视",
        params={
            "op": "to_csv | to_json | to_markdown | to_html | filter | sort | merge_sheets | pivot",
            "sheet": "工作表名或索引（默认第一张）",
            "header_row": "表头行（默认 1）",
            "query": "filter：pandas query 表达式，如 '数值 > 10 and 区域 == \"华东\"'",
            "columns": "sort/select：列名列表",
            "ascending": "sort：是否升序（默认 true）",
            "index": "pivot：行索引列",
            "pivot_columns": "pivot：列方向字段",
            "values": "pivot：聚合值字段",
            "aggfunc": "pivot：聚合函数（默认 sum）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        pandas = require("pandas", "pandas")
        op = (ctx.text("op", "to_csv") or "to_csv").strip().lower()
        stem = slugify(ctx.input_path().stem)

        if op in ("merge_sheets", "merge"):
            frames = self._load_all_frames(ctx, pandas)
            merged = pandas.concat(frames, ignore_index=True, sort=False)
            target = ctx.out_file(f"{stem}.merged.xlsx")
            merged.to_excel(target, index=False)
            ctx.emit(target, "xlsx")
            return {"op": op, "sheets": len(frames), "rows": int(merged.shape[0]),
                    "columns": list(map(str, merged.columns))}

        df, sheet_name = self._load_frame(ctx, pandas)

        if op in ("to_csv", "csv"):
            target = ctx.out_file(f"{stem}-{slugify(sheet_name)}.csv")
            df.to_csv(target, index=False, encoding="utf-8-sig")
            ctx.emit(target, "csv")
            return {"op": op, "sheet": sheet_name, "rows": int(df.shape[0]), "output": str(target)}

        if op in ("to_json", "json"):
            target = ctx.out_file(f"{stem}-{slugify(sheet_name)}.json")
            target.write_text(df.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
            ctx.emit(target, "json")
            return {"op": op, "rows": int(df.shape[0]), "output": str(target)}

        if op in ("to_markdown", "markdown", "md"):
            target = ctx.out_file(f"{stem}-{slugify(sheet_name)}.md")
            try:
                body = df.to_markdown(index=False)
            except Exception:
                body = self._df_to_markdown(df)
            target.write_text(f"## {sheet_name}\n\n{body}\n", encoding="utf-8")
            ctx.emit(target, "markdown")
            return {"op": op, "rows": int(df.shape[0]), "output": str(target)}

        if op in ("to_html", "html"):
            target = ctx.out_file(f"{stem}-{slugify(sheet_name)}.html")
            body = df.to_html(index=False, border=1, escape=False)
            target.write_text(
                "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head><meta charset=\"utf-8\">"
                f"<title>{sheet_name}</title>\n<style>body{{font-family:-apple-system,'Segoe UI',"
                "'Microsoft YaHei',sans-serif;margin:32px}}table{{border-collapse:collapse}}"
                "th,td{border:1px solid #d0d0d0;padding:6px 10px}th{background:#f5f5f5}</style>"
                f"</head>\n<body>\n<h2>{sheet_name}</h2>\n{body}\n</body>\n</html>\n",
                encoding="utf-8",
            )
            ctx.emit(target, "html")
            return {"op": op, "rows": int(df.shape[0]), "output": str(target)}

        if op in ("filter", "sort", "select", "dedupe"):
            result = df
            if op == "filter":
                query = ctx.require("query")
                try:
                    result = df.query(str(query))
                except Exception as exc:
                    raise ActionError(
                        f"筛选表达式执行失败: {exc}",
                        hint="pandas query 语法示例: 数值 > 10 and 区域 == \"华东\"；字符串需用引号包裹。",
                    ) from exc
            if op == "sort":
                columns = ctx.list_of("columns")
                if not columns:
                    raise ParamError("sort 需要 columns 参数，如 -p columns=数值,日期")
                missing = [c for c in columns if c not in df.columns]
                if missing:
                    raise ParamError(f"排序列不存在: {missing}", hint=f"可用列: {list(df.columns)[:30]}")
                result = result.sort_values(by=columns, ascending=ctx.flag("ascending", True))
            if op == "select":
                columns = ctx.list_of("columns")
                if not columns:
                    raise ParamError("select 需要 columns 参数")
                result = result[[c for c in columns if c in result.columns]]
            if op == "dedupe":
                result = result.drop_duplicates()

            fmt = ctx.text("format", "xlsx").lower()
            if fmt == "csv":
                target = ctx.out_file(f"{stem}-{op}.csv")
                result.to_csv(target, index=False, encoding="utf-8-sig")
                ctx.emit(target, "csv")
            else:
                target = ctx.out_file(f"{stem}-{op}.xlsx")
                result.to_excel(target, index=False)
                ctx.emit(target, "xlsx")
            return {"op": op, "rows_in": int(df.shape[0]), "rows_out": int(result.shape[0]),
                    "output": str(target)}

        if op in ("pivot", "pivot_table"):
            index = ctx.text("index", "")
            values = ctx.text("values", "")
            columns = ctx.text("pivot_columns", "")
            if not index or not values:
                raise ParamError("pivot 需要 index 与 values 参数")
            aggfunc = ctx.text("aggfunc", "sum")
            try:
                table = pandas.pivot_table(
                    df, index=index, columns=columns or None, values=values, aggfunc=aggfunc
                ).round(4)
            except Exception as exc:
                raise ActionError(f"透视表构建失败: {exc}") from exc
            target = ctx.out_file(f"{stem}-pivot.xlsx")
            table.to_excel(target)
            ctx.emit(target, "xlsx")
            return {"op": op, "shape": [int(table.shape[0]), int(table.shape[1])],
                    "preview": table.head(20).reset_index().astype(str).to_dict(orient="records"),
                    "output": str(target)}

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: to_csv / to_json / to_markdown / to_html / filter / sort / select / dedupe / merge_sheets / pivot",
        )

    # ======================================================== edit
    @action(
        "edit",
        summary="修改工作簿：写单元格/公式、追加行、增删改工作表、设置格式、图表、冻结窗格",
        params={
            "op": "set_cell | write_range | append_rows | set_formula | add_sheet | delete_sheet | "
                  "rename_sheet | format_range | autofit | freeze_panes | merge_cells | "
                  "insert_rows | delete_rows | replace_text | add_chart | set_column_width",
            "sheet": "目标工作表（默认第一张）",
            "cell": "set_cell / set_formula：单元格坐标，如 B2",
            "value": "set_cell：写入值",
            "formula": "set_formula：公式，如 =SUM(B2:B10)",
            "range": "write_range / format_range 的区间，如 A1:C10",
            "rows": "write_range / append_rows：二维数组",
            "name": "add_sheet / rename_sheet 的工作表名",
            "in_place": "是否就地保存（默认 true）",
        },
        modifies_input=True,
        tags=("edit",),
    )
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        openpyxl = require("openpyxl", "openpyxl")
        path = ctx.input_path()
        wb = openpyxl.load_workbook(str(path), data_only=False)
        op = (ctx.text("op", "set_cell") or "set_cell").strip().lower()

        operations = {
            "set_cell": self._op_set_cell,
            "write_range": self._op_write_range,
            "append_rows": self._op_append_rows,
            "set_formula": self._op_set_formula,
            "add_sheet": self._op_add_sheet,
            "delete_sheet": self._op_delete_sheet,
            "rename_sheet": self._op_rename_sheet,
            "format_range": self._op_format_range,
            "autofit": self._op_autofit,
            "freeze_panes": self._op_freeze,
            "merge_cells": self._op_merge,
            "insert_rows": self._op_insert_rows,
            "delete_rows": self._op_delete_rows,
            "replace_text": self._op_replace_text,
            "add_chart": self._op_add_chart,
            "set_column_width": self._op_set_column_width,
        }
        handler_fn = operations.get(op)
        if handler_fn is None:
            raise ParamError(f"不支持的 op: {op}", hint=f"可选: {', '.join(sorted(operations))}")

        changed = handler_fn(ctx, wb, openpyxl) or 0
        sheet_names = list(wb.sheetnames)
        target = self._save_target(ctx, path)
        wb.save(str(target))
        wb.close()
        if target != path:
            ctx.emit(target, "xlsx")
        return {
            "op": op,
            "changed": changed,
            "saved_to": str(target),
            "in_place": target == path,
            "backup": ctx.get("__backup__"),
            "sheet_names": sheet_names,
        }

    # ------------------------------------------------------------------ edit 子操作
    def _ws(self, ctx: ActionContext, wb: Any, *, create: bool = False) -> Any:
        name = ctx.text("sheet", "")
        if not name:
            return wb.worksheets[0]
        if name in wb.sheetnames:
            return wb[name]
        if name.isdigit():
            index = int(name)
            if 0 <= index < len(wb.worksheets):
                return wb.worksheets[index]
        if create:
            return wb.create_sheet(name)
        raise ParamError(f"工作表不存在: {name}", hint=f"可用: {wb.sheetnames}")

    def _op_set_cell(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        coordinate = str(ctx.require("cell")).upper().replace("$", "")
        value = ctx.get("value")
        ws[coordinate] = self._coerce_value(value)
        return 1

    def _op_set_formula(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        coordinate = str(ctx.require("cell")).upper().replace("$", "")
        formula = str(ctx.require("formula")).strip()
        if not formula.startswith("="):
            formula = "=" + formula
        ws[coordinate] = formula
        return 1

    def _op_write_range(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        spec = ctx.text("range", "")
        rows = self._coerce_rows(ctx.get("rows"))
        if not rows:
            raise ParamError("write_range 需要 rows 参数（二维数组）")
        if spec:
            min_c, min_r, _hi_c, _hi_r = _parse_range(spec)
        else:
            min_c, min_r = 0, 1
        written = 0
        for dr, row in enumerate(rows):
            for dc, value in enumerate(row):
                ws.cell(row=min_r + dr, column=min_c + dc + 1).value = self._coerce_value(value)
                written += 1
        return written

    def _op_append_rows(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        rows = self._coerce_rows(ctx.get("rows"))
        if not rows:
            raise ParamError("append_rows 需要 rows 参数")
        for row in rows:
            ws.append([self._coerce_value(v) for v in row])
        return len(rows)

    def _op_add_sheet(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        name = ctx.text("name", "") or "Sheet"
        if name in wb.sheetnames:
            raise ParamError(f"工作表已存在: {name}")
        index = ctx.get("index")
        if index is not None:
            wb.create_sheet(name, int(index))
        else:
            wb.create_sheet(name)
        return 1

    def _op_delete_sheet(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        name = ctx.text("name", "") or ctx.text("sheet", "")
        if name not in wb.sheetnames:
            raise ParamError(f"工作表不存在: {name}", hint=f"可用: {wb.sheetnames}")
        if len(wb.sheetnames) == 1:
            raise ActionError("不能删除唯一的工作表")
        del wb[name]
        return 1

    def _op_rename_sheet(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        old = ctx.text("sheet", "") or ctx.text("old", "")
        new = ctx.text("name", "") or ctx.text("new", "")
        if old not in wb.sheetnames:
            raise ParamError(f"工作表不存在: {old}", hint=f"可用: {wb.sheetnames}")
        if not new:
            raise ParamError("rename_sheet 需要 name 参数作为新名称")
        wb[old].title = new
        return 1

    def _op_format_range(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: PLC0415

        ws = self._ws(ctx, wb)
        spec = ctx.require("range")
        min_c, min_r, max_c, max_r = _parse_range(str(spec))
        font_kwargs: dict[str, Any] = {}
        if ctx.get("bold") is not None:
            font_kwargs["bold"] = ctx.flag("bold")
        if ctx.get("italic") is not None:
            font_kwargs["italic"] = ctx.flag("italic")
        if ctx.get("size") is not None:
            font_kwargs["size"] = float(ctx.number("size", 11) or 11)
        if ctx.text("color", ""):
            font_kwargs["color"] = ctx.text("color").lstrip("#").upper()
        if ctx.text("font", ""):
            font_kwargs["name"] = ctx.text("font")

        fill_color = ctx.text("fill", "").lstrip("#").upper()
        number_format = ctx.text("number_format", "")
        align = ctx.text("align", "")
        add_border = ctx.flag("border", False)
        thin = Side(style="thin", color="D0D0D0")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        changed = 0
        for row in ws.iter_rows(min_row=min_r, max_row=max_r, min_col=min_c + 1, max_col=max_c + 1):
            for cell in row:
                if font_kwargs:
                    cell.font = Font(**(dict(cell.font.__dict__ if False else {}) | font_kwargs))
                if fill_color:
                    cell.fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
                if number_format:
                    cell.number_format = number_format
                if align:
                    cell.alignment = Alignment(horizontal=align, vertical="center", wrap_text=ctx.flag("wrap", False))
                elif ctx.flag("wrap", False):
                    cell.alignment = Alignment(wrap_text=True)
                if add_border:
                    cell.border = border
                changed += 1
        return changed

    def _op_autofit(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        limit = ctx.bounded_int("max_width", 60, low=8, high=255)
        widths: dict[int, int] = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                text = str(cell.value)
                # 中日韩字符按 2 个宽度估算
                width = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
                widths[cell.column] = max(widths.get(cell.column, 0), width)
        for column, width in widths.items():
            ws.column_dimensions[col_letter(column - 1)].width = min(limit, max(8, width + 2))
        return len(widths)

    def _op_freeze(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        cell = ctx.text("cell", "A2")
        ws.freeze_panes = None if cell.lower() in ("none", "false", "") else cell.upper()
        return 1

    def _op_merge(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        spec = str(ctx.require("range"))
        if ctx.flag("unmerge", False):
            ws.unmerge_cells(spec)
        else:
            ws.merge_cells(spec)
        return 1

    def _op_insert_rows(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        start = ctx.integer("start", 1) or 1
        count = ctx.bounded_int("count", 1, low=1, high=10_000)
        ws.insert_rows(start, count)
        return count

    def _op_delete_rows(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        start = ctx.integer("start", 1) or 1
        count = ctx.bounded_int("count", 1, low=1, high=100_000)
        ws.delete_rows(start, count)
        return count

    def _op_replace_text(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        find = str(ctx.require("find"))
        repl = ctx.text("replace", "")
        regex = ctx.flag("regex", False)
        pattern = re.compile(find if regex else re.escape(find),
                             re.IGNORECASE if ctx.flag("ignore_case", False) else 0)
        sheets = [self._ws(ctx, wb)] if ctx.text("sheet", "") else wb.worksheets
        changed = 0
        for ws in sheets:
            for row in ws.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        continue  # 不动公式
                    if isinstance(cell.value, str) and pattern.search(cell.value):
                        cell.value = pattern.sub(repl, cell.value)
                        changed += 1
        return changed

    def _op_add_chart(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        from openpyxl.chart import BarChart, LineChart, PieChart, Reference  # noqa: PLC0415

        ws = self._ws(ctx, wb)
        chart_type = ctx.text("chart_type", "bar").lower()
        chart = {"bar": BarChart, "column": BarChart, "line": LineChart, "pie": PieChart}.get(chart_type)
        if chart is None:
            raise ParamError(f"不支持的图表类型: {chart_type}", hint="可选: bar / column / line / pie")
        chart = chart()
        chart.title = ctx.text("title", "") or None
        if chart_type == "column":
            chart.type = "col"
        spec = ctx.text("range", "")
        if spec:
            min_c, min_r, max_c, max_r = _parse_range(spec)
        else:
            min_c, min_r = 0, 1
            max_c, max_r = max(1, ws.max_column - 1), max(1, ws.max_row)
        data = Reference(ws, min_col=min_c + 2, max_col=max_c + 1, min_row=min_r, max_row=max_r)
        chart.add_data(data, titles_from_data=ctx.flag("titles_from_data", True))
        if ctx.flag("categories", True):
            chart.set_categories(Reference(ws, min_col=min_c + 1, min_row=min_r + 1, max_row=max_r))
        anchor = ctx.text("anchor", f"{col_letter(max_c + 2)}{min_r}")
        ws.add_chart(chart, anchor)
        return 1

    def _op_set_column_width(self, ctx: ActionContext, wb: Any, openpyxl: Any) -> int:
        ws = self._ws(ctx, wb)
        columns = ctx.list_of("columns")
        width = ctx.number("width", 20) or 20
        if not columns:
            raise ParamError("set_column_width 需要 columns 参数，如 -p columns=A,B,C")
        for name in columns:
            ws.column_dimensions[str(name).upper()].width = float(width)
        return len(columns)

    # ------------------------------------------------------------------ 数据装配
    def _select_sheets(self, wb: Any, spec: str) -> list[str]:
        names = list(wb.sheetnames)
        if not names:
            raise InputError("工作簿中没有工作表")
        key = (spec or "").strip()
        if not key:
            return [names[0]]
        if key.lower() == "all":
            return names
        if key in names:
            return [key]
        if key.isdigit():
            index = int(key)
            if 0 <= index < len(names):
                return [names[index]]
            raise ParamError(f"工作表索引越界: {index}（共 {len(names)} 张）")
        raise ParamError(f"工作表不存在: {key}", hint=f"可用: {names}")

    def _read_sheet(
        self,
        ws_value: Any,
        ws_formula: Any,
        *,
        header_row: int,
        max_rows: int,
        max_cols: int,
        mode: str,
        include_formulas: bool,
        counter: Any,
    ) -> dict[str, Any]:
        max_col = min(ws_value.max_column or 1, max_cols)
        max_row = ws_value.max_row or 0

        header: list[str] = []
        if header_row and header_row <= max_row:
            header = [
                ("" if ws_value.cell(row=header_row, column=c).value is None
                 else str(ws_value.cell(row=header_row, column=c).value)).strip() or f"列{c}"
                for c in range(1, max_col + 1)
            ]
        records: list[dict[str, Any]] = []
        matrix: list[list[Any]] = []
        formula_cells: list[dict[str, Any]] = []

        start = header_row + 1 if header_row else 1
        for r in range(start, min(max_row, start + max_rows - 1) + 1):
            row_values: list[Any] = []
            for c in range(1, max_col + 1):
                value = ws_value.cell(row=r, column=c).value
                formula = ws_formula.cell(row=r, column=c).value
                is_formula = isinstance(formula, str) and formula.startswith("=")
                if is_formula:
                    formula_cells.append(
                        {"cell": f"{col_letter(c - 1)}{r}", "formula": formula, "cached_value": value}
                    )
                    if mode == "formulas":
                        value = formula
                    elif mode == "both":
                        value = {"value": value, "formula": formula}
                row_values.append(value)
            matrix.append(row_values)
            if header:
                record = {}
                for i, name in enumerate(header):
                    column_name = f"{name}_{i + 1}" if header.count(name) > 1 else name
                    record[column_name] = row_values[i] if i < len(row_values) else None
                records.append(record)

        return {
            "name": ws_value.title,
            "dimensions": str(ws_value.dimensions),
            "max_row": max_row,
            "max_column": max_col,
            "header": header,
            "returned_rows": len(matrix),
            "truncated": max_row - (start - 1) > max_rows,
            "records": records,
            "matrix": matrix if ctx_matrix_enabled(mode) else None,
            "formula_cells": formula_cells if include_formulas else [],
            "formula_cell_count": len(formula_cells),
            "merged_cells": [str(rng) for rng in ws_value.merged_cells.ranges],
            "freeze_panes": ws_value.freeze_panes,
            "number_formats": self._number_formats(ws_value, max_col),
        }

    @staticmethod
    def _number_formats(ws: Any, max_col: int) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in range(1, max_col + 1):
            for r in range(1, min(ws.max_row or 1, 50) + 1):
                fmt = ws.cell(row=r, column=c).number_format
                if fmt and fmt != "General":
                    out[col_letter(c - 1)] = fmt
                    break
        return out

    def _load_frame(self, ctx: ActionContext, pandas: Any) -> tuple[Any, str]:
        openpyxl = require("openpyxl", "openpyxl")
        path = ctx.input_path()
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        names = self._select_sheets(wb, ctx.text("sheet", ""))
        name = names[0]
        ws = wb[name]
        header_row = ctx.bounded_int("header_row", 1, low=1, high=1000)
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return pandas.DataFrame(), name
        header = [("" if v is None else str(v)).strip() or f"列{i + 1}" for i, v in enumerate(rows[header_row - 1])]
        width = len(header)
        data = [list(r[:width]) + [None] * max(0, width - len(r)) for r in rows[header_row:]]
        return pandas.DataFrame(data, columns=self._dedupe(header)), name

    def _load_all_frames(self, ctx: ActionContext, pandas: Any) -> list[Any]:
        openpyxl = require("openpyxl", "openpyxl")
        path = ctx.input_path()
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        header_row = ctx.bounded_int("header_row", 1, low=1, high=1000)
        frames = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            header = [("" if v is None else str(v)).strip() or f"列{i + 1}"
                      for i, v in enumerate(rows[header_row - 1])]
            width = len(header)
            data = [list(r[:width]) + [None] * max(0, width - len(r)) for r in rows[header_row:]]
            frame = pandas.DataFrame(data, columns=self._dedupe(header))
            frame.insert(0, "_sheet", name)
            frames.append(frame)
        wb.close()
        if not frames:
            raise InputError("工作簿中没有可用的数据表")
        return frames

    @staticmethod
    def _dedupe(names: list[str]) -> list[str]:
        seen: dict[str, int] = {}
        out: list[str] = []
        for name in names:
            if name in seen:
                seen[name] += 1
                out.append(f"{name}_{seen[name]}")
            else:
                seen[name] = 1
                out.append(name)
        return out

    @staticmethod
    def _coerce_value(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if text.lower() in ("none", "null"):
                return None
            if text.lower() in ("true", "false"):
                return text.lower() == "true"
            try:
                if re.fullmatch(r"-?\d+", text):
                    return int(text)
                if re.fullmatch(r"-?\d+\.\d+", text):
                    return float(text)
            except ValueError:
                pass
            return value
        return value

    def _coerce_rows(self, raw: Any) -> list[list[Any]]:
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = [[c.strip() for c in line.split(",")] for line in raw.splitlines() if line.strip()]
            raw = parsed
        if not isinstance(raw, (list, tuple)):
            return []
        rows: list[list[Any]] = []
        for row in raw:
            rows.append(list(row) if isinstance(row, (list, tuple)) else [row])
        return rows

    @staticmethod
    def _df_to_markdown(df: Any) -> str:
        columns = [str(c) for c in df.columns]
        lines = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
        for _, row in df.head(500).iterrows():
            lines.append("| " + " | ".join(str(v).replace("\n", " ") for v in row.tolist()) + " |")
        return "\n".join(lines)

    @staticmethod
    def _save_target(ctx: ActionContext, original: Path) -> Path:
        if ctx.output is not None:
            return ctx.out_file(f"{slugify(original.stem)}.xlsx")
        if not ctx.flag("in_place", True):
            return original.with_name(f"{original.stem}_edited{original.suffix}")
        return original


def ctx_matrix_enabled(mode: str) -> bool:
    """仅在 both/formulas 模式下附带原始矩阵，避免 values 模式响应过大。"""
    return mode in ("both", "formulas")


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """xlsx_handler 独立运行

  python handlers/xlsx_handler.py probe     book.xlsx
  python handlers/xlsx_handler.py read      book.xlsx -p sheet=Sheet1 -p max_rows=100
  python handlers/xlsx_handler.py analyze   book.xlsx -p sheet=Sheet1 -p by=区域
  python handlers/xlsx_handler.py transform book.xlsx -p op=to_csv -o ./out
  python handlers/xlsx_handler.py edit      book.xlsx -p op=set_cell -p cell=B2 -p value=100
  python handlers/xlsx_handler.py cap
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(XlsxHandler().capabilities(), ensure_ascii=False, indent=2))
        return 0
    action_name, rest = args[0], args[1:]
    cli_args = ["run", "-m", MODULE, "-a", action_name]
    if rest and not rest[0].startswith("-"):
        cli_args += ["-i", rest[0]]
        rest = rest[1:]
    cli_args += ["--json"] if "--json" in rest else []
    cli_args += [tok for tok in rest if tok != "--json"]
    return cli_main(cli_args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
