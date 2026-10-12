"""图片处理模块：PNG/JPG 等位图的读取、内容识别、转换与就地编辑。

「内容识别」在这里拆成四层，逐层加深：
--------------------------------------------------------------------
1. **元数据层** —— 格式、尺寸、DPI、EXIF（含 GPS）、ICC、透明度、动画帧数
2. **统计层**   —— 清晰度（Laplacian 方差）、亮度、对比度、色彩丰富度、噪声估计
3. **文字层**   —— OCR 多后端识别（见 ``ocr/engines.py``），输出文本 + 行级版面
4. **语义层**   —— 类型判别（照片/截图/扫描件/图表/图标/示意图）+ **视觉桥接**：
   输出一份可直接交给多模态大模型的 payload（data URI + 结构化提问模板），
   由 Agent 侧的视觉能力完成「图里画了什么」这一层语义理解。

为什么语义层要做成桥接而不是本地模型
--------------------------------------
本地跑 VLM 需要 GPU 与数 GB 权重，与本工具箱「纯 pip、离线、轻量」的定位冲突。
把语义层显式交给调用方的多模态能力，既避免引入重型依赖，也让识别质量随模型升级而自动提升。

独立运行
--------
    python handlers/image_handler.py probe    shot.png
    python handlers/image_handler.py analyze  shot.png -p ocr=true
    python handlers/image_handler.py transform shot.png -p op=compress -p quality=70
    python handlers/image_handler.py edit      shot.png -p op=redact -p box=10,10,200,60
"""

from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.base import ActionContext, BaseHandler, action  # noqa: E402
from core.version import VERSION  # noqa: E402
from core.errors import ActionError, InputError, ParamError, require  # noqa: E402
from core.utils import human_bytes, slugify  # noqa: E402

MODULE = "image"

#: 常见屏幕/相机比例 → 名称（用于截图与照片判别）
_KNOWN_RATIOS = {
    1.0: "1:1",
    4 / 3: "4:3",
    3 / 2: "3:2",
    16 / 9: "16:9",
    16 / 10: "16:10",
    21 / 9: "21:9",
    9 / 16: "9:16",
    2 / 3: "2:3",
    3 / 4: "3:4",
}

_VLM_PROMPT = """请分析这张图片，按以下结构输出（简体中文）：
1. 图片类型：照片 / 截图 / 文档扫描件 / 数据图表 / 示意图 / 图标 / 其他
2. 主体内容：一句话概括画面主题
3. 关键元素：列出可辨识的对象、人物、界面控件、数据系列
4. 图片内文字：逐条转写可见文字（若已提供 OCR 结果，请做纠错与补全）
5. 场景与用途：可能的使用场景，例如技术文档配图、产品截图、宣传素材
6. 若为图表：说明图表类型、坐标轴含义与主要数据结论
如某个维度无法判断，请明确写"无法判断"，不要臆测。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _pil():
    return require("PIL.Image", "Pillow")


def _open_image(path: Path):
    Image = _pil()
    try:
        img = Image.open(str(path))
        img.load()
    except Exception as exc:
        raise InputError(f"无法打开图片: {exc}", details={"path": str(path)}) from exc
    return img


def _stringify(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")[:256]
        except Exception:
            return f"<bytes:{len(value)}>"
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)[:256]


def _exif_dict(img: Any) -> dict[str, Any]:
    try:
        from PIL.ExifTags import TAGS  # noqa: PLC0415

        exif = img.getexif()
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for tag_id, value in exif.items():
        name = TAGS.get(tag_id, str(tag_id))
        if name == "GPSInfo":
            out["GPS"] = _gps_to_decimal(value)
        elif name == "ExifOffset":
            continue
        else:
            out[name] = _stringify(value)
    try:
        for tag_id, value in exif.get_ifd(0x8769).items():
            name = TAGS.get(tag_id, str(tag_id))
            out[name] = _stringify(value)
    except Exception:
        pass
    return out


def _gps_to_decimal(raw: Any) -> dict[str, Any]:
    try:
        from PIL.ExifTags import GPSTAGS  # noqa: PLC0415

        data = {GPSTAGS.get(k, k): v for k, v in dict(raw).items()}

        def to_deg(values: Sequence[Any]) -> Optional[float]:
            try:
                d, m, s = (float(v) for v in values)
                return round(d + m / 60 + s / 3600, 6)
            except Exception:
                return None

        lat = to_deg(data.get("GPSLatitude", []))
        lon = to_deg(data.get("GPSLongitude", []))
        if lat is None or lon is None:
            return {"raw": str(data)[:200]}
        if data.get("GPSLatitudeRef") in ("S", "s"):
            lat = -lat
        if data.get("GPSLongitudeRef") in ("W", "w"):
            lon = -lon
        return {
            "latitude": lat,
            "longitude": lon,
            "altitude": _stringify(data.get("GPSAltitude")),
            "timestamp": str(data.get("GPSDateStamp") or "") + " " + str(data.get("GPSTimeStamp") or ""),
            "note": "图片包含地理位置信息，对外发布前请评估隐私风险",
        }
    except Exception as exc:
        return {"error": str(exc)}


def _metrics(img: Any) -> dict[str, Any]:
    """清晰度 / 亮度 / 对比度 / 色彩丰富度 / 噪声 / 边缘密度。"""
    try:
        import numpy as np  # type: ignore
    except ImportError:
        return {"available": False, "reason": "缺少 numpy，无法计算质量指标"}

    gray = img.convert("L").resize(
        (min(img.width, 1200), max(1, int(min(img.width, 1200) * img.height / max(1, img.width))))
    )
    arr = np.asarray(gray, dtype=np.float32)
    if arr.size == 0:
        return {"available": False, "reason": "图像尺寸异常"}

    lap = (
        arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:] - 4 * arr[1:-1, 1:-1]
    )
    blur_score = float(lap.var())
    gy, gx = np.gradient(arr)
    edge_density = float(np.mean(np.hypot(gx, gy) > 24))
    noise = float(np.std(arr - _box_blur(arr)))

    rgb = np.asarray(img.convert("RGB").resize((min(img.width, 480), max(1, int(min(img.width, 480) * img.height / max(1, img.width))))), dtype=np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colorfulness = float(
        math.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * math.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )

    hist, _ = np.histogram(arr, bins=256, range=(0, 255), density=True)
    hist = hist[hist > 0]
    entropy = float(-(hist * np.log2(hist)).sum())

    return {
        "available": True,
        "blur_laplacian_var": round(blur_score, 2),
        "is_blurry": blur_score < 60,
        "brightness": round(float(arr.mean()), 2),
        "contrast_std": round(float(arr.std()), 2),
        "edge_density": round(edge_density, 4),
        "noise_level": round(noise, 2),
        "colorfulness": round(colorfulness, 2),
        "entropy": round(entropy, 3),
        "is_grayscale": _is_grayscale(rgb),
        "exposure_hint": (
            "偏暗" if arr.mean() < 60 else "偏亮" if arr.mean() > 200 else "正常"
        ),
    }


def _box_blur(arr: Any) -> Any:
    import numpy as np  # type: ignore

    k = np.ones((3, 3), dtype=np.float32) / 9.0
    out = np.zeros_like(arr)
    padded = np.pad(arr, 1, mode="edge")
    for dy in range(3):
        for dx in range(3):
            out += k[dy, dx] * padded[dy : dy + arr.shape[0], dx : dx + arr.shape[1]]
    return out


def _is_grayscale(rgb: Any) -> bool:
    import numpy as np  # type: ignore

    diff = np.abs(rgb[..., 0] - rgb[..., 1]).mean() + np.abs(rgb[..., 1] - rgb[..., 2]).mean()
    return bool(diff < 3)


def _palette(img: Any, count: int = 8) -> list[dict[str, Any]]:
    try:
        small = img.convert("RGB").copy()
        small.thumbnail((200, 200))
        quantized = small.quantize(colors=count, method=2) if hasattr(small, "quantize") else small
        palette = quantized.getpalette() or []
        counts = sorted(quantized.getcolors() or [], reverse=True)
        total = sum(c for c, _ in counts) or 1
        out: list[dict[str, Any]] = []
        for cnt, index in counts[:count]:
            base = index * 3
            if base + 2 >= len(palette):
                continue
            r, g, b = palette[base], palette[base + 1], palette[base + 2]
            out.append(
                {
                    "hex": f"#{r:02X}{g:02X}{b:02X}",
                    "rgb": [r, g, b],
                    "ratio": round(cnt / total, 4),
                }
            )
        return out
    except Exception:
        return []


def _n_colors(img: Any, cap: int = 200_000) -> int:
    try:
        small = img.convert("RGB").copy()
        small.thumbnail((256, 256))
        colors = small.getcolors(maxcolors=cap)
        return len(colors) if colors else cap
    except Exception:
        return cap


def _ratio_name(ratio: float) -> Optional[str]:
    for value, name in _KNOWN_RATIOS.items():
        if abs(ratio - value) < 0.02:
            return name
    return None


def _classify(img: Any, metrics: dict[str, Any], ocr: Optional[dict[str, Any]]) -> dict[str, Any]:
    """启发式判别图片类型，返回得分明细便于解释。"""
    w, h = img.size
    colors = _n_colors(img)
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    ratio = round(w / max(1, h), 3)
    ratio_name = _ratio_name(w / max(1, h))

    text_chars = int((ocr or {}).get("chars", 0) or 0)
    area = max(1, w * h)
    text_coverage = 0.0
    if ocr and ocr.get("blocks"):
        covered = 0.0
        for block in ocr["blocks"]:
            box = block.get("bbox") or []
            if len(box) == 4:
                covered += max(0.0, (box[2] - box[0])) * max(0.0, (box[3] - box[1]))
        text_coverage = min(1.0, covered / area)
    text_density = text_chars / max(1, area / 10000)  # 每万像素字数

    scores = {
        "photo": 0.0,
        "screenshot": 0.0,
        "document_scan": 0.0,
        "chart_or_diagram": 0.0,
        "logo_or_icon": 0.0,
    }
    if colors > 20_000:
        scores["photo"] += 2.0
    if metrics.get("colorfulness", 0) > 40:
        scores["photo"] += 1.5
    if metrics.get("noise_level", 0) > 2.5:
        scores["photo"] += 1.0
    if text_coverage < 0.05 and metrics.get("edge_density", 0) < 0.2:
        scores["photo"] += 1.0

    if ratio_name and colors < 20_000:
        scores["screenshot"] += 1.5
    if colors < 6_000:
        scores["screenshot"] += 1.0
    if text_coverage > 0.03:
        scores["screenshot"] += 1.5
    if metrics.get("noise_level", 0) < 1.5:
        scores["screenshot"] += 1.0
    if not has_alpha and metrics.get("brightness", 0) > 190:
        scores["screenshot"] += 0.5

    if text_coverage > 0.15 and colors < 40_000:
        scores["document_scan"] += 2.5
    if metrics.get("brightness", 0) > 175 and metrics.get("contrast_std", 0) > 35:
        scores["document_scan"] += 1.0
    if text_density > 1.2:
        scores["document_scan"] += 1.0
    if metrics.get("is_blurry"):
        scores["document_scan"] -= 0.5

    if text_coverage > 0.05 and colors < 12_000:
        scores["chart_or_diagram"] += 1.5
    if metrics.get("edge_density", 0) > 0.05 and colors < 8_000 and text_chars > 0:
        scores["chart_or_diagram"] += 1.0
    if has_alpha:
        scores["chart_or_diagram"] += 0.5

    if has_alpha and colors <= 5_000 and min(w, h) <= 1024:
        scores["logo_or_icon"] += 3.0
    if min(w, h) <= 256:
        scores["logo_or_icon"] += 2.0
    if text_chars == 0 and colors <= 2_000:
        scores["logo_or_icon"] += 1.0

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = round(min(1.0, max(0.0, (top_score - second_score) / 3.0)) + (0.35 if top_score > 1.5 else 0.0), 3)
    return {
        "guess": top if top_score >= 1.0 else "unknown",
        "confidence": min(1.0, confidence),
        "scores": {k: round(v, 2) for k, v in scores.items()},
        "signals": {
            "width": w,
            "height": h,
            "aspect_ratio": ratio,
            "aspect_name": ratio_name,
            "unique_colors": colors,
            "has_alpha": has_alpha,
            "text_chars": text_chars,
            "text_coverage": round(text_coverage, 4),
            "text_density_per_10k_px": round(text_density, 3),
        },
    }


def _layout_from_ocr(blocks: list[dict[str, Any]], height: int) -> dict[str, Any]:
    """把 OCR 文本块按行聚合，粗略还原版面结构。"""
    if not blocks:
        return {"lines": [], "columns_estimate": 0, "regions": {}}
    boxed = [b for b in blocks if len(b.get("bbox") or []) == 4]
    boxed.sort(key=lambda b: (round(b["bbox"][1] / 12), b["bbox"][0]))
    lines: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for block in boxed:
        if not current:
            current = [block]
            continue
        last = current[-1]["bbox"]
        if abs(block["bbox"][1] - last[1]) <= 10:
            current.append(block)
        else:
            lines.append(current)
            current = [block]
    if current:
        lines.append(current)

    rendered = []
    for group in lines:
        text = " ".join(b["text"] for b in group)
        x1 = min(b["bbox"][0] for b in group)
        x2 = max(b["bbox"][2] for b in group)
        y1 = min(b["bbox"][1] for b in group)
        y2 = max(b["bbox"][3] for b in group)
        rendered.append({"text": text, "bbox": [x1, y1, x2, y2]})

    x_centers = [(l["bbox"][0] + l["bbox"][2]) / 2 for l in rendered]
    columns = 1
    if len(x_centers) >= 4:
        left = [x for x in x_centers if x < sum(x_centers) / len(x_centers) * 0.75]
        right = [x for x in x_centers if x > sum(x_centers) / len(x_centers) * 1.25]
        if len(left) >= 2 and len(right) >= 2:
            columns = 2

    regions = {"header": [], "body": [], "footer": []}
    for line in rendered:
        y = line["bbox"][1]
        if y < height * 0.12:
            regions["header"].append(line["text"])
        elif y > height * 0.88:
            regions["footer"].append(line["text"])
        else:
            regions["body"].append(line["text"])
    return {
        "lines": rendered[:200],
        "line_count": len(rendered),
        "columns_estimate": columns,
        "regions": {k: v[:40] for k, v in regions.items()},
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
class ImageHandler(BaseHandler):
    module = MODULE
    version = VERSION
    display_name = "图片（PNG/JPG 等）"
    description = (
        "处理位图图片：读取元数据与 EXIF、OCR 识别图中文字、评估清晰度与配色、"
        "启发式判别图片类型并生成多模态视觉分析载荷，支持转换/压缩/裁剪/加水印/打码。"
    )
    extensions = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
    kinds = ("png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff")
    optional_deps = {"PIL": "Pillow", "rapidocr_onnxruntime": "rapidocr-onnxruntime"}

    # ======================================================== probe
    @action(
        "probe",
        summary="快速探测：格式、尺寸、模式、DPI、透明度、动画、体积",
        params={"exif": "是否附带 EXIF 概要（默认 false）"},
        tags=("read",),
    )
    def probe(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        img = _open_image(path)
        info: dict[str, Any] = {
            "file": path.name,
            "format": img.format,
            "mode": img.mode,
            "width": img.width,
            "height": img.height,
            "megapixels": round(img.width * img.height / 1e6, 3),
            "aspect_ratio": round(img.width / max(1, img.height), 3),
            "size": path.stat().st_size,
            "size_human": human_bytes(path.stat().st_size),
            "dpi": img.info.get("dpi"),
            "has_alpha": img.mode in ("RGBA", "LA") or "transparency" in img.info,
            "is_animated": bool(getattr(img, "is_animated", False)),
            "n_frames": int(getattr(img, "n_frames", 1) or 1),
            "icc_profile": bool(img.info.get("icc_profile")),
            "has_exif": bool(img.info.get("exif")),
            "comment": _stringify(img.info.get("comment")),
        }
        from ocr.engines import available_backends  # noqa: PLC0415

        info["ocr_backends"] = available_backends()["available"]
        if ctx.flag("exif", False):
            info["exif"] = _exif_dict(img)
        img.close()
        return info

    # ======================================================== read
    @action(
        "read",
        summary="读取元数据：EXIF（含 GPS）、ICC、调色板、质量指标",
        params={
            "include_exif": "是否输出完整 EXIF（默认 true）",
            "include_palette": "是否输出主色板（默认 true）",
            "palette_size": "主色数量（默认 8）",
            "include_metrics": "是否输出质量指标（默认 true）",
        },
        tags=("read",),
    )
    def read(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        img = _open_image(path)
        out: dict[str, Any] = {
            "file": path.name,
            "format": img.format,
            "mode": img.mode,
            "size_px": [img.width, img.height],
            "dpi": img.info.get("dpi"),
            "has_alpha": img.mode in ("RGBA", "LA") or "transparency" in img.info,
            "n_frames": int(getattr(img, "n_frames", 1) or 1),
            "file_size": path.stat().st_size,
        }
        if ctx.flag("include_exif", True):
            out["exif"] = _exif_dict(img)
        if ctx.flag("palette", True) or ctx.flag("include_palette", True):
            out["palette"] = _palette(img, ctx.bounded_int("palette_size", 8, low=1, high=32))
        if ctx.flag("include_metrics", True):
            out["metrics"] = _metrics(img)
        img.close()
        return out

    # ======================================================== analyze
    @action(
        "analyze",
        summary="内容识别：OCR 文字 + 版面还原 + 类型判别 + 视觉桥接载荷",
        params={
            "ocr": "是否做 OCR（默认 true；设为 false 则只做统计与判别）",
            "ocr_langs": "OCR 语言，默认 chi_sim+eng",
            "ocr_backend": "auto / rapidocr / tesseract / easyocr",
            "ocr_min_confidence": "过滤低于该置信度的文本块（默认 0.4）",
            "binarize": "OCR 前是否二值化（默认 false，扫描件可开启）",
            "vision_payload": "是否输出多模态视觉分析载荷（默认 false，会显著增大响应体）",
            "palette_size": "主色数量（默认 8）",
        },
        tags=("read", "analyze"),
    )
    def analyze(self, ctx: ActionContext) -> dict[str, Any]:
        path = ctx.input_path()
        img = _open_image(path)
        width, height = img.size

        metrics = _metrics(img)
        palette = _palette(img, ctx.bounded_int("palette_size", 8, low=1, high=32))

        ocr_payload: Optional[dict[str, Any]] = None
        ocr_available = True
        if ctx.flag("ocr", True):
            from ocr import engines  # noqa: PLC0415

            backends = engines.available_backends()
            if not backends["usable"]:
                ocr_available = False
                ctx.warn(
                    "未检测到可用 OCR 后端，已跳过文字识别。"
                    f"推荐安装命令: {backends['backends'][0]['install_cmd']}"
                )
            else:
                try:
                    result = engines.recognize(
                        path,
                        backend=ctx.text("ocr_backend", "auto"),
                        langs=ctx.text("ocr_langs", "chi_sim+eng"),
                        binarize=ctx.flag("binarize", False),
                        min_confidence=ctx.number("ocr_min_confidence", 0.4) or 0.0,
                    )
                    ocr_payload = result.to_dict()
                    ocr_payload["blocks"] = [
                        b for b in ocr_payload["blocks"] if b.get("confidence", 0) >= (ctx.number("ocr_min_confidence", 0.4) or 0.0)
                    ]
                    ocr_payload["chars"] = len(ocr_payload.get("text", ""))
                    ocr_payload["layout"] = _layout_from_ocr(ocr_payload["blocks"], height)
                except Exception as exc:
                    ctx.warn(f"OCR 执行失败，已跳过: {exc}")
                    ocr_available = False

        classification = _classify(img, metrics, ocr_payload)
        result: dict[str, Any] = {
            "file": path.name,
            "size_px": [width, height],
            "classification": classification,
            "metrics": metrics,
            "palette": palette,
            "ocr": ocr_payload
            if ocr_payload is not None
            else {"skipped": True, "backend_available": ocr_available},
            "summary": self._summarize(classification, metrics, ocr_payload),
        }

        if ctx.flag("vision_payload", False):
            Image = _pil()
            target = ctx.out_dir("_vision") / f"{slugify(path.stem)}.png"
            img.convert("RGB").save(target, "PNG")
            payload = base64.b64encode(target.read_bytes()).decode("ascii")
            result["vision_bridge"] = {
                "mime": "image/png",
                "data_uri": f"data:image/png;base64,{payload}",
                "prompt": _VLM_PROMPT,
                "local_signals_to_include": {
                    "heuristic_type": classification["guess"],
                    "aspect_ratio": classification["signals"]["aspect_ratio"],
                    "ocr_text": (ocr_payload or {}).get("text", "")[:3000],
                },
                "usage": "把 data_uri（或本地图片路径）连同 prompt 交给多模态模型，即可获得语义级内容描述",
            }
            target.unlink(missing_ok=True)

        img.close()
        return result

    # ======================================================== transform
    @action(
        "transform",
        summary="转换与导出：格式转换、缩放、裁剪、旋转、压缩、水印、转 PDF、base64",
        params={
            "op": "convert | resize | thumbnail | crop | rotate | flip | grayscale | compress | "
                  "watermark | to_pdf | to_base64 | auto_orient | strip_exif | sprite",
            "format": "convert：目标格式 png/jpg/webp/bmp/tiff",
            "width": "resize：目标宽度（像素）",
            "height": "resize：目标高度（像素）",
            "percent": "resize/thumbnail：按百分比缩放",
            "keep_aspect": "resize：是否保持比例（默认 true）",
            "box": "crop/redact：left,top,right,bottom",
            "angle": "rotate：旋转角度（度，逆时针）",
            "quality": "compress：JPEG 质量 1-100（默认 80）",
            "text": "watermark：水印文字",
            "max_side": "thumbnail：最长边像素（默认 512）",
        },
        produces_files=True,
        tags=("transform",),
    )
    def transform(self, ctx: ActionContext) -> dict[str, Any]:
        op = (ctx.text("op", "convert") or "convert").strip().lower()
        path = ctx.input_path()
        stem = slugify(path.stem)
        img = _open_image(path)
        Image = _pil()

        if op in ("convert", "resize", "thumbnail", "crop", "rotate", "flip",
                  "grayscale", "auto_orient", "strip_exif"):
            return self._transform_geometry(ctx, img, Image, stem, op)

        if op in ("compress", "optimize"):
            return self._transform_compress(ctx, img, stem)

        if op in ("watermark", "stamp"):
            return self._transform_watermark(ctx, img, Image, stem)

        if op in ("to_pdf", "pdf"):
            target = ctx.out_file(f"{stem}.pdf")
            img.convert("RGB").save(str(target), "PDF", resolution=float(ctx.number("dpi", 150) or 150))
            ctx.emit(target, "pdf")
            return {"op": "to_pdf", "output": str(target), "pages": 1}

        if op in ("to_base64", "base64", "data_uri"):
            fmt = (ctx.text("format", "png") or "png").upper()
            if fmt == "JPG":
                fmt = "JPEG"
            from io import BytesIO  # noqa: PLC0415

            buffer = BytesIO()
            payload_img = img if img.mode in ("RGB", "L") or fmt != "JPEG" else img.convert("RGB")
            payload_img.save(buffer, fmt)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
                    "BMP": "image/bmp", "TIFF": "image/tiff", "GIF": "image/gif"}.get(fmt, "image/png")
            target = ctx.out_file(f"{stem}.datauri.txt")
            target.write_text(f"data:{mime};base64,{encoded}", encoding="utf-8")
            ctx.emit(target, "text")
            result = {
                "op": "to_base64",
                "mime": mime,
                "base64_chars": len(encoded),
                "output": str(target),
                "preview_prefix": f"data:{mime};base64,{encoded[:64]}…",
            }
            if ctx.flag("inline", False):
                result["data_uri"] = f"data:{mime};base64,{encoded}"
                ctx.warn("已按要求把完整 data URI 放入响应，体积较大，请谨慎转发")
            return result

        if op in ("sprite", "contact_sheet"):
            return self._transform_sprite(ctx, img, stem)

        raise ParamError(
            f"不支持的 op: {op}",
            hint="可选: convert / resize / thumbnail / crop / rotate / flip / grayscale / compress / "
                 "watermark / to_pdf / to_base64 / auto_orient / strip_exif / sprite",
        )

    def _transform_geometry(self, ctx: ActionContext, img: Any, Image: Any, stem: str, op: str) -> dict[str, Any]:
        from PIL import ImageOps  # noqa: PLC0415

        if op == "auto_orient":
            rotated = ImageOps.exif_transpose(img)
            meta = {"op": op, "orientation_applied": rotated is not img}
            img = rotated
        elif op == "strip_exif":
            data = list(img.getdata())
            img = Image.new(img.mode, img.size)
            img.putdata(data)
            meta = {"op": op, "exif_removed": True}
        elif op == "rotate":
            angle = ctx.number("angle", 90) or 0
            img = img.rotate(angle, expand=ctx.flag("expand", True), resample=Image.BICUBIC)
            meta = {"op": op, "angle": angle}
        elif op == "flip":
            direction = ctx.text("direction", "horizontal").lower()
            img = ImageOps.mirror(img) if direction.startswith("h") else ImageOps.flip(img)
            meta = {"op": op, "direction": direction}
        elif op == "grayscale":
            img = img.convert("L")
            meta = {"op": op}
        elif op == "crop":
            left, top, right, bottom = self._parse_box(ctx.require("box"))
            right = right or img.width
            bottom = bottom or img.height
            if left < 0 or top < 0 or right > img.width or bottom > img.height or right <= left or bottom <= top:
                raise ParamError(
                    f"裁剪区域越界: ({left},{top},{right},{bottom}) 超出图片尺寸 {img.width}x{img.height}"
                )
            img = img.crop((left, top, right, bottom))
            meta = {"op": op, "box": [left, top, right, bottom]}
        elif op == "thumbnail":
            max_side = ctx.bounded_int("max_side", 512, low=16, high=20000)
            img = img.copy()
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            meta = {"op": op, "max_side": max_side}
        else:  # resize
            percent = ctx.number("percent", None)
            if percent:
                img = img.resize(
                    (max(1, int(img.width * percent / 100)), max(1, int(img.height * percent / 100))),
                    Image.LANCZOS,
                )
            else:
                width = ctx.integer("width", None)
                height = ctx.integer("height", None)
                if not width and not height:
                    raise ParamError("resize 需要 width / height 或 percent 参数")
                if ctx.flag("keep_aspect", True):
                    ratio = img.width / max(1, img.height)
                    if width and not height:
                        height = max(1, int(width / ratio))
                    elif height and not width:
                        width = max(1, int(height * ratio))
                img = img.resize((max(1, width), max(1, height)), Image.LANCZOS)
            meta = {"op": "resize", "size": [img.width, img.height]}

        img = self._prepare_for_save(ctx, img, Image)
        ext = self._target_ext(ctx, img)
        target = ctx.out_file(f"{stem}{ext}")
        self._save(ctx, img, target)
        ctx.emit(target, "image")
        meta.update({"output": str(target), "size": [img.width, img.height],
                     "format": (img.format or ext.lstrip(".")).upper()})
        return meta

    def _transform_compress(self, ctx: ActionContext, img: Any, stem: str) -> dict[str, Any]:
        quality = ctx.bounded_int("quality", 80, low=1, high=100)
        original_size = ctx.input_path().stat().st_size
        target = ctx.out_file(f"{stem}.jpg")
        img.convert("RGB").save(str(target), "JPEG", quality=quality, optimize=True,
                                progressive=ctx.flag("progressive", True))
        ctx.emit(target, "image")
        new_size = target.stat().st_size
        return {
            "op": "compress",
            "quality": quality,
            "original_bytes": original_size,
            "new_bytes": new_size,
            "saved_ratio": round(1 - new_size / max(1, original_size), 3),
            "output": str(target),
        }

    def _transform_watermark(self, ctx: ActionContext, img: Any, Image: Any, stem: str) -> dict[str, Any]:
        from PIL import ImageDraw, ImageFont  # noqa: PLC0415

        text = ctx.text("text", "") or "CONFIDENTIAL"
        opacity = ctx.bounded_int("opacity", 28, low=1, high=100)
        position = ctx.text("position", "bottom-right").lower()
        scale = float(ctx.number("font_scale", 0.05) or 0.05)

        base = img.convert("RGBA")
        layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(layer)
        size = max(12, int(min(base.size) * scale))
        font = None
        for candidate in ("msyh.ttc", "simhei.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(text) * size // 2, size
        margin = max(12, int(min(base.size) * 0.02))
        if "bottom-right" in position:
            xy = (base.width - tw - margin, base.height - th - margin * 2)
        elif "bottom-left" in position:
            xy = (margin, base.height - th - margin * 2)
        elif "top-right" in position:
            xy = (base.width - tw - margin, margin)
        elif "center" in position:
            xy = ((base.width - tw) // 2, (base.height - th) // 2)
        else:
            xy = (margin, margin)

        draw.text(xy, text, font=font, fill=(255, 255, 255, int(255 * opacity / 100)),
                  stroke_width=max(1, size // 24), stroke_fill=(0, 0, 0, int(255 * opacity / 100)))
        merged = Image.alpha_composite(base, layer)
        img = self._prepare_for_save(ctx, merged, Image)
        ext = self._target_ext(ctx, img)
        target = ctx.out_file(f"{stem}-watermarked{ext}")
        self._save(ctx, img, target)
        ctx.emit(target, "image")
        return {"op": "watermark", "text": text, "position": position, "output": str(target)}

    def _transform_sprite(self, ctx: ActionContext, img: Any, stem: str) -> dict[str, Any]:
        """把图片切成网格小图（九宫格/精灵图拆分），或生成联系表。"""
        Image = _pil()
        mode = ctx.text("mode", "split").lower()
        cols = ctx.bounded_int("cols", 3, low=1, high=50)
        rows = ctx.bounded_int("rows", 3, low=1, high=50)
        out_dir = ctx.out_dir(f"{stem}-sprite")
        written: list[str] = []
        if mode == "split":
            for r in range(rows):
                for c in range(cols):
                    left = int(img.width * c / cols)
                    right = int(img.width * (c + 1) / cols)
                    top = int(img.height * r / rows)
                    bottom = int(img.height * (r + 1) / rows)
                    tile = img.crop((left, top, right, bottom))
                    target = out_dir / f"{stem}-r{r + 1}c{c + 1}.png"
                    tile.save(str(target), "PNG")
                    written.append(target.name)
        else:
            frame = img.convert("RGB")
            target = out_dir / f"{stem}-contact-sheet.png"
            frame.save(str(target), "PNG")
            written.append(target.name)
        ctx.emit(out_dir, "directory", f"{len(written)} 张子图")
        return {"op": "sprite", "mode": mode, "count": len(written),
                "output_dir": str(out_dir), "files": written}

    # ======================================================== edit
    @action(
        "edit",
        summary="就地编辑：打码遮盖、模糊/马赛克区域、文字标注、画框、加边框、拼网格",
        params={
            "op": "redact | blur_region | pixelate_region | annotate_text | draw_box | "
                  "add_border | draw_grid | watermark",
            "box": "目标区域 left,top,right,bottom（redact/blur/pixelate/draw_box）",
            "text": "annotate_text：标注文字；watermark：水印文字",
            "color": "draw_box / annotate_text：颜色，如 #FF0000 或 red",
            "width": "draw_box / add_border：线宽（默认 3）",
            "radius": "blur_region：模糊半径（默认 12）",
            "block": "pixelate_region：马赛克块大小（默认 12）",
            "opacity": "redact：遮盖不透明度（默认 100）",
        },
        modifies_input=True,
        tags=("edit",),
    )
    def edit(self, ctx: ActionContext) -> dict[str, Any]:
        op = (ctx.text("op", "redact") or "redact").strip().lower()
        path = ctx.input_path()
        Image = _pil()
        img = _open_image(path)

        if op in ("redact", "blur_region", "pixelate_region", "draw_box"):
            box = self._parse_box(ctx.require("box"))
            if op == "redact":
                from PIL import ImageDraw  # noqa: PLC0415

                opacity = self._opacity(ctx, "opacity", 100)
                region = img.crop(box).convert("RGBA")
                overlay = Image.new("RGBA", region.size, (0, 0, 0, opacity))
                img.paste(Image.alpha_composite(region, overlay).convert(img.mode), box[:2])
            elif op == "blur_region":
                from PIL import ImageFilter  # noqa: PLC0415
                from PIL import ImageDraw  # noqa: PLC0415

                radius = ctx.bounded_int("radius", 12, low=1, high=200)
                mask = Image.new("L", img.size, 0)
                ImageDraw.Draw(mask).rectangle(box, fill=255)
                blurred = img.filter(ImageFilter.GaussianBlur(radius))
                img.paste(blurred, (0, 0), mask)
            elif op == "pixelate_region":
                block = ctx.bounded_int("block", 12, low=2, high=200)
                region = img.crop(box)
                small = region.resize(
                    (max(1, region.width // block), max(1, region.height // block)), Image.NEAREST
                )
                img.paste(small.resize(region.size, Image.NEAREST), box[:2])
            else:
                from PIL import ImageDraw  # noqa: PLC0415

                draw = ImageDraw.Draw(img)
                draw.rectangle(box, outline=self._color(ctx.text("color", "#FF0000")),
                               width=ctx.bounded_int("width", 3, low=1, high=64))
            changed = 1
        elif op == "annotate_text":
            from PIL import ImageDraw, ImageFont  # noqa: PLC0415

            text = str(ctx.require("text"))
            left, top = self._parse_point(ctx.text("at", "10,10"))
            size = ctx.bounded_int("size", 24, low=6, high=400)
            font = None
            for candidate in ("msyh.ttc", "simhei.ttf", "arial.ttf", "DejaVuSans.ttf"):
                try:
                    font = ImageFont.truetype(candidate, size)
                    break
                except Exception:
                    continue
            draw = ImageDraw.Draw(img)
            draw.text((left, top), text, fill=self._color(ctx.text("color", "#FF0000")),
                      font=font, stroke_width=ctx.bounded_int("stroke", 0, low=0, high=20),
                      stroke_fill=self._color(ctx.text("stroke_color", "#FFFFFF")))
            changed = 1
        elif op == "add_border":
            from PIL import ImageOps  # noqa: PLC0415

            width = ctx.bounded_int("width", 8, low=1, high=500)
            img = ImageOps.expand(img, border=width, fill=self._color(ctx.text("color", "#000000")))
            changed = 1
        elif op == "draw_grid":
            from PIL import ImageDraw  # noqa: PLC0415

            cols = ctx.bounded_int("cols", 10, low=1, high=200)
            color = self._color(ctx.text("color", "#CCCCCC"))
            draw = ImageDraw.Draw(img)
            for c in range(1, cols):
                x = int(img.width * c / cols)
                draw.line([(x, 0), (x, img.height)], fill=color, width=1)
            for r in range(1, cols):
                y = int(img.height * r / cols)
                draw.line([(0, y), (img.width, y)], fill=color, width=1)
            changed = 1
        elif op == "watermark":
            sub = ActionContext(handler=self, action="transform", input=path, output=ctx.output, options=ctx.options)
            result = self._transform_watermark(sub, img, Image, slugify(path.stem))
            img.close()
            return result
        else:
            raise ParamError(
                f"不支持的 op: {op}",
                hint="可选: redact / blur_region / pixelate_region / annotate_text / draw_box / "
                     "add_border / draw_grid / watermark",
            )

        img = self._prepare_for_save(ctx, img, Image)
        ext = self._target_ext(ctx, img, default=path.suffix.lower())
        target = self._save_target(ctx, path, ext)
        self._save(ctx, img, target)
        if target != path:
            ctx.emit(target, "image")
        return {
            "op": op,
            "changed": changed,
            "saved_to": str(target),
            "in_place": target == path,
            "backup": ctx.get("__backup__"),
            "size": [img.width, img.height],
        }

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _parse_box(spec: Any) -> tuple[int, int, int, int]:
        if isinstance(spec, str):
            parts = [p.strip() for p in spec.replace(":", ",").split(",") if p.strip()]
        elif isinstance(spec, (list, tuple)):
            parts = list(spec)
        else:
            raise ParamError(f"box 参数格式错误: {spec!r}，应为 left,top,right,bottom")
        if len(parts) != 4:
            raise ParamError(f"box 需要 4 个值，收到 {len(parts)} 个: {spec!r}")
        try:
            return tuple(int(float(p)) for p in parts)  # type: ignore[return-value]
        except (TypeError, ValueError) as exc:
            raise ParamError(f"box 数值无法解析: {spec!r}") from exc

    @staticmethod
    def _parse_point(spec: str) -> tuple[int, int]:
        parts = [p.strip() for p in str(spec).replace(":", ",").split(",") if p.strip()]
        if len(parts) != 2:
            raise ParamError(f"坐标格式错误: {spec!r}，应为 x,y")
        return int(float(parts[0])), int(float(parts[1]))

    @staticmethod
    def _opacity(ctx: ActionContext, key: str, default: int) -> int:
        pct = ctx.bounded_int(key, default, low=0, high=100)
        return int(255 * pct / 100)

    @staticmethod
    def _color(value: str) -> Any:
        text = str(value or "").strip()
        named = {"red": "#FF0000", "green": "#00AA00", "blue": "#0000FF", "black": "#000000",
                 "white": "#FFFFFF", "gray": "#808080", "grey": "#808080", "yellow": "#FFD700"}
        text = named.get(text.lower(), text)
        if text.startswith("#"):
            text = text[1:]
        if len(text) == 6:
            try:
                return tuple(int(text[i : i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
        return (0, 0, 0)

    @staticmethod
    def _prepare_for_save(ctx: ActionContext, img: Any, Image: Any) -> Any:
        """根据目标格式决定是否压平透明通道（JPEG 不支持 alpha）。"""
        fmt = (ctx.text("format", "") or "").lower().lstrip(".")
        if fmt in ("jpg", "jpeg", "bmp", "pdf"):
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                converted = img.convert("RGBA")
                background.paste(converted, mask=converted.split()[-1])
                return background
            if img.mode not in ("RGB", "L"):
                return img.convert("RGB")
        return img

    @staticmethod
    def _target_ext(ctx: ActionContext, img: Any, default: str = "") -> str:
        fmt = (ctx.text("format", "") or "").lower().lstrip(".")
        if fmt:
            return "." + ("jpg" if fmt in ("jpeg", "jpg") else fmt)
        if default:
            return default
        base = (img.format or "PNG").lower()
        return "." + ("jpg" if base == "jpeg" else base)

    @staticmethod
    def _save(ctx: ActionContext, img: Any, target: Path) -> None:
        ext = target.suffix.lower().lstrip(".")
        fmt = {"jpg": "JPEG", "jpeg": "JPEG", "tif": "TIFF"}.get(ext, ext.upper())
        kwargs: dict[str, Any] = {}
        if fmt == "JPEG":
            kwargs.update(quality=ctx.bounded_int("quality", 90, low=1, high=100),
                          optimize=True, progressive=True)
        elif fmt == "PNG":
            kwargs.update(optimize=True, compress_level=ctx.bounded_int("compress_level", 6, low=0, high=9))
        elif fmt == "WEBP":
            kwargs.update(quality=ctx.bounded_int("quality", 85, low=1, high=100))
        try:
            img.save(str(target), fmt, **kwargs)
        except Exception as exc:
            raise ActionError(f"保存图片失败: {exc}", hint=f"目标路径: {target}") from exc

    @staticmethod
    def _save_target(ctx: ActionContext, original: Path, ext: str) -> Path:
        if ctx.output is not None:
            return ctx.out_file(f"{slugify(original.stem)}{ext}")
        if not ctx.flag("in_place", True):
            return original.with_name(f"{original.stem}_edited{ext}")
        return original

    @staticmethod
    def _summarize(classification: dict[str, Any], metrics: dict[str, Any],
                   ocr: Optional[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        kind_label = {
            "photo": "照片", "screenshot": "屏幕截图", "document_scan": "文档扫描件",
            "chart_or_diagram": "图表/示意图", "logo_or_icon": "图标/Logo", "unknown": "无法确定",
        }.get(classification.get("guess", "unknown"), classification.get("guess"))
        lines.append(f"类型判别：{kind_label}（置信度 {classification.get('confidence', 0):.2f}）")
        if metrics.get("available"):
            lines.append(
                f"画质：清晰度指标 {metrics['blur_laplacian_var']}（{'偏模糊' if metrics.get('is_blurry') else '清晰'}），"
                f"亮度 {metrics['brightness']}（{metrics.get('exposure_hint')}），对比度 {metrics['contrast_std']}"
            )
        if ocr and ocr.get("text"):
            lines.append(
                f"文字识别：{ocr.get('chars', 0)} 字符，后端 {ocr.get('backend')}，"
                f"平均置信度 {ocr.get('mean_confidence')}"
            )
        elif ocr and ocr.get("skipped"):
            lines.append("文字识别：未执行或后端不可用")
        return lines


# ---------------------------------------------------------------------------
# 独立运行入口
# ---------------------------------------------------------------------------
_HELP = """image_handler 独立运行

  python handlers/image_handler.py probe     shot.png -p exif=true
  python handlers/image_handler.py read      shot.png
  python handlers/image_handler.py analyze   shot.png -p ocr=true -p vision_payload=true
  python handlers/image_handler.py transform shot.png -p op=compress -p quality=70 -o ./out
  python handlers/image_handler.py edit      shot.png -p op=redact -p box=10,10,200,60
  python handlers/image_handler.py cap
"""


def main(argv: Optional[list[str]] = None) -> int:
    from core.cli import main as cli_main  # noqa: PLC0415

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0
    if args[0] == "cap":
        print(json.dumps(ImageHandler().capabilities(), ensure_ascii=False, indent=2))
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
