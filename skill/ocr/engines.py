"""OCR 后端适配层：把「图像内容识别」做成可插拔的多后端链路。

为什么需要这一层
----------------
OCR 是本工具箱里唯一「没有纯 pip 标准答案」的能力：

* RapidOCR：纯 pip 安装（onnxruntime），离线、CPU 快、中文准确率高 —— **默认首选**
* Tesseract：需要额外安装系统级可执行文件，但生态成熟、参数可控
* EasyOCR：PyTorch 生态，体积大，精度弱于 RapidOCR

因此这里统一抽象成 ``recognize()``：调用方只说「给我文字」，由本层按
``auto`` 策略挑一个可用后端，并屏蔽各家 API 的差异（返回结构统一）。
若一个后端都没装，抛 ``DependencyError``，由上层降级为 warning 而不是崩溃。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.errors import ActionError, DependencyError, InputError, pip_hint, require

# 规范语言标记（tesseract 风格）→ 各后端语言码
_LANG_ALIASES = {
    "chi_sim": "ch",
    "chi_tra": "ch",
    "zh": "ch",
    "chs": "ch",
    "cht": "ch",
    "eng": "en",
    "en": "en",
    "jpn": "japan",
    "jp": "japan",
    "kor": "korean",
    "ko": "korean",
    "fra": "fr",
    "deu": "de",
    "rus": "ru",
}

_TESSERACT_HINTS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
]

_ENGINE_CACHE: dict[tuple[str, str], Any] = {}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class OCRBlock:
    """一块被识别出的文本（含位置与置信度）。"""

    text: str
    confidence: float = 0.0
    box: list[list[float]] = field(default_factory=list)  # 四点多边形 [[x,y], ...]

    def bbox(self) -> list[float]:
        """轴对齐包围盒 ``[x1, y1, x2, y2]``。"""
        if not self.box:
            return []
        xs = [pt[0] for pt in self.box]
        ys = [pt[1] for pt in self.box]
        return [min(xs), min(ys), max(xs), max(ys)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "bbox": [round(v, 1) for v in self.bbox()],
        }


@dataclass
class OCRResult:
    backend: str
    text: str = ""
    blocks: list[OCRBlock] = field(default_factory=list)
    langs: str = ""
    mean_confidence: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "langs": self.langs,
            "chars": len(self.text),
            "line_count": len([ln for ln in self.text.splitlines() if ln.strip()]),
            "mean_confidence": round(self.mean_confidence, 4),
            "blocks": [b.to_dict() for b in self.blocks],
            "text": self.text,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 后端探测
# ---------------------------------------------------------------------------
def find_tesseract() -> Optional[str]:
    """定位 tesseract 可执行文件。"""
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in _TESSERACT_HINTS:
        if Path(candidate).exists():
            return candidate
    return None


def _rapidocr_available() -> bool:
    try:
        __import__("rapidocr_onnxruntime")
        return True
    except ImportError:
        try:
            __import__("rapidocr")  # 2.x 新包名
            return True
        except ImportError:
            return False


def _pytesseract_available() -> bool:
    try:
        __import__("pytesseract")
    except ImportError:
        return False
    return find_tesseract() is not None


def _easyocr_available() -> bool:
    try:
        __import__("easyocr")
        return True
    except ImportError:
        return False


def available_backends() -> dict[str, Any]:
    """探测 OCR 后端可用性，供 doctor / analyze 使用。"""
    rows = [
        {
            "name": "rapidocr",
            "available": _rapidocr_available(),
            "purpose": "纯 pip 离线 OCR，中文精度高，CPU 快（推荐）",
            "install_cmd": pip_hint("rapidocr-onnxruntime onnxruntime"),
        },
        {
            "name": "tesseract",
            "available": _pytesseract_available(),
            "purpose": "Tesseract 引擎（需系统安装 tesseract 可执行文件）",
            "install_cmd": "安装 tesseract 后执行: " + pip_hint("pytesseract")
            if not find_tesseract()
            else pip_hint("pytesseract"),
            "binary": find_tesseract(),
        },
        {
            "name": "easyocr",
            "available": _easyocr_available(),
            "purpose": "EasyOCR（PyTorch，模型体积大，精度一般）",
            "install_cmd": pip_hint("easyocr"),
        },
    ]
    ready = [r["name"] for r in rows if r["available"]]
    return {
        "available": ready,
        "recommended": ready[0] if ready else None,
        "usable": bool(ready),
        "backends": rows,
    }


# ---------------------------------------------------------------------------
# 图像预处理
# ---------------------------------------------------------------------------
def prepare_image(
    path: str | Path,
    *,
    min_side: int = 900,
    max_side: int = 2400,
    grayscale: bool = True,
    autocontrast: bool = True,
    binarize: bool = False,
):
    """给 OCR 用的轻量预处理（缩放 + 灰度 + 对比度拉伸）。

    过小的图先放大能显著提升小字识别率；过大的图缩小可降低推理耗时。
    仅依赖 Pillow；若装了 OpenCV 则用自适应二值化做更好的增强。
    """
    Pillow = require("PIL.Image", "Pillow", hint=f"安装命令: {pip_hint('Pillow')}")
    Image, ImageOps = Pillow, require("PIL.ImageOps", "Pillow")

    img = Image.open(str(path))
    try:
        img = ImageOps.exif_transpose(img) or img
    except Exception:
        pass
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    w, h = img.size
    short = min(w, h)
    long = max(w, h)
    if short < min_side:
        scale = min_side / max(1, short)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    elif long > max_side:
        scale = max_side / long
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    if grayscale:
        img = img.convert("L")
    if autocontrast:
        img = ImageOps.autocontrast(img, cutoff=1)
    if binarize:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore

            arr = np.array(img)
            arr = cv2.adaptiveThreshold(
                arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
            )
            img = Image.fromarray(arr)
        except Exception:
            img = img.point(lambda x: 0 if x < 128 else 255, "1")
    return img


def _norm_langs(langs: str) -> str:
    parts = [p.strip() for p in str(langs or "chi_sim+eng").replace(",", "+").split("+") if p.strip()]
    return "+".join(parts) if parts else "chi_sim+eng"


def _to_backend_langs(langs: str, backend: str) -> list[str]:
    codes: list[str] = []
    for part in _norm_langs(langs).split("+"):
        code = _LANG_ALIASES.get(part.lower(), part)
        if code not in codes:
            codes.append(code)
    if backend == "rapidocr":
        # RapidOCR 必需 ch 才能识别中文；未显式给出时补 en
        return codes or ["ch", "en"]
    return codes or ["chi_sim", "eng"]


# ---------------------------------------------------------------------------
# 各后端实现
# ---------------------------------------------------------------------------
def _engine_rapidocr(langs_list: list[str]):
    key = ("rapidocr", "+".join(langs_list))
    if key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key]
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError:
        from rapidocr import RapidOCR  # type: ignore  # 2.x
    kwargs: dict[str, Any] = {}
    try:
        engine = RapidOCR(lang="+".join(langs_list) if len(langs_list) > 1 else langs_list[0])
    except TypeError:
        engine = RapidOCR()
    _ENGINE_CACHE[key] = engine
    return engine


def recognize_rapidocr(img, langs_list: list[str]) -> OCRResult:
    engine = _engine_rapidocr(langs_list)
    raw = engine(img)

    blocks: list[OCRBlock] = []
    note = ""
    payload = raw
    if isinstance(raw, tuple):  # 1.x: (result, elapse)
        payload = raw[0]
    if hasattr(raw, "boxes") and hasattr(raw, "txts"):  # 2.x/3.x RapidOCROutput
        # 注意：3.x 的 boxes/txts/scores 是 numpy 数组，不能用 `x or []`（布尔歧义），
        # 也不能用 `if payload:` 之类的真值判断 —— 必须 is None 显式判空。
        boxes = getattr(raw, "boxes", None)
        txts = getattr(raw, "txts", None)
        scores = getattr(raw, "scores", None)
        boxes = [] if boxes is None else list(boxes)
        txts = [] if txts is None else list(txts)
        scores = [] if scores is None else list(scores)
        for i, text in enumerate(txts):
            box = boxes[i] if i < len(boxes) else []
            score = float(scores[i]) if i < len(scores) else 0.0
            blocks.append(OCRBlock(text=str(text), confidence=score,
                                   box=[[float(p[0]), float(p[1])] for p in box]))
    elif payload is not None and len(payload) != 0:  # 1.x: list[(box, text, score)]
        for item in payload:
            try:
                box, text, score = item[0], item[1], item[2]
            except (TypeError, IndexError):
                continue
            blocks.append(
                OCRBlock(
                    text=str(text),
                    confidence=float(score),
                    box=[[float(p[0]), float(p[1])] for p in box],
                )
            )
    else:
        note = "RapidOCR 未检测到任何文本"
    return _pack("rapidocr", blocks, langs_list, note)


def recognize_tesseract(img, langs_list: list[str]) -> OCRResult:
    pytesseract = require("pytesseract", "pytesseract")
    binary = find_tesseract()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary
    lang = "+".join(langs_list)
    try:
        data = pytesseract.image_to_data(
            img, lang=lang, output_type=pytesseract.Output.DICT, config="--oem 3 --psm 6"
        )
    except Exception as exc:
        raise ActionError(
            f"Tesseract 识别失败: {exc}",
            hint="确认已安装对应语言包（chi_sim.traineddata / eng.traineddata）",
        ) from exc

    blocks: list[OCRBlock] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            conf = 0.0
        x, y, w, h = (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        blocks.append(
            OCRBlock(
                text=text,
                confidence=conf / 100.0,
                box=[[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
            )
        )
    # 按行重建可读文本
    lines: dict[tuple[int, int, int], list[str]] = {}
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(text)
    ordered = "\n".join(" ".join(lines[k]) for k in sorted(lines))
    result = _pack("tesseract", blocks, langs_list, "")
    result.text = ordered
    return result


def recognize_easyocr(img, langs_list: list[str]) -> OCRResult:
    easyocr = require("easyocr", "easyocr")
    code_map = {"ch": "ch_sim", "en": "en", "japan": "ja", "korean": "ko"}
    codes = [code_map.get(c, c) for c in langs_list] or ["ch_sim", "en"]
    key = ("easyocr", "+".join(codes))
    if key not in _ENGINE_CACHE:
        _ENGINE_CACHE[key] = easyocr.Reader(codes, gpu=False, verbose=False)
    reader = _ENGINE_CACHE[key]
    raw = reader.readtext(str(img) if isinstance(img, (str, Path)) else _to_array(img))
    blocks = [
        OCRBlock(
            text=str(item[1]),
            confidence=float(item[2]),
            box=[[float(p[0]), float(p[1])] for p in item[0]],
        )
        for item in raw
    ]
    return _pack("easyocr", blocks, codes, "")


def _to_array(img):
    import numpy as np  # type: ignore

    return np.array(img)


def _pack(backend: str, blocks: list[OCRBlock], langs_list: list[str], note: str) -> OCRResult:
    text = "\n".join(b.text for b in blocks if b.text.strip())
    confs = [b.confidence for b in blocks if b.confidence > 0]
    return OCRResult(
        backend=backend,
        text=text,
        blocks=blocks,
        langs="+".join(langs_list),
        mean_confidence=(sum(confs) / len(confs)) if confs else 0.0,
        note=note,
    )


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
_ORDER = ("rapidocr", "tesseract", "easyocr")


def recognize(
    path: str | Path,
    *,
    backend: str = "auto",
    langs: str = "chi_sim+eng",
    preprocess: bool = True,
    binarize: bool = False,
    min_confidence: float = 0.0,
    max_side: int = 2400,
) -> OCRResult:
    """识别图片中的文字。

    Args:
        backend: ``auto`` / ``rapidocr`` / ``tesseract`` / ``easyocr``。
        langs: 规范语言标记，如 ``"chi_sim+eng"``。
        preprocess: 是否做缩放/灰度/对比度预处理。
        min_confidence: 低于该置信度的块直接丢弃。

    Raises:
        DependencyError: 指定的（或 auto 下全部）后端不可用。
    """
    src = Path(path)
    if not src.exists():
        raise InputError(f"图片不存在: {src}")

    order = _ORDER if backend in ("auto", "", None) else (backend,)
    last_error: Optional[Exception] = None

    # 预处理结果统一落为临时 PNG 再交给各后端。
    # 原因：RapidOCR 的输入加载层只接受「路径 / bytes / ndarray」，直接传 PIL 对象会失败；
    # 而走临时文件是三家后端（rapidocr / tesseract / easyocr）都最稳定的一条路径。
    tmp_path: Optional[Path] = None
    try:
        for name in order:
            try:
                if name == "rapidocr" and not _rapidocr_available():
                    last_error = DependencyError(
                        "RapidOCR 不可用",
                        hint=f"安装命令: {pip_hint('rapidocr-onnxruntime onnxruntime')}",
                    )
                    continue
                if name == "tesseract" and not _pytesseract_available():
                    last_error = DependencyError(
                        "Tesseract 不可用",
                        hint="需先安装 tesseract 可执行文件，再安装 pytesseract",
                    )
                    continue
                if name == "easyocr" and not _easyocr_available():
                    last_error = DependencyError(
                        "EasyOCR 不可用", hint=f"安装命令: {pip_hint('easyocr')}"
                    )
                    continue

                langs_list = _to_backend_langs(langs, name)
                if preprocess:
                    if tmp_path is None:
                        prepared = prepare_image(src, binarize=binarize, max_side=max_side)
                        fd, tmp_name = tempfile.mkstemp(prefix="omni_ocr_", suffix=".png")
                        os.close(fd)
                        tmp_path = Path(tmp_name)
                        prepared.save(tmp_path, "PNG")
                    target: Any = str(tmp_path)
                else:
                    target = str(src)

                if name == "rapidocr":
                    result = recognize_rapidocr(target, langs_list)
                elif name == "tesseract":
                    result = recognize_tesseract(target, langs_list)
                else:
                    result = recognize_easyocr(target, langs_list)

                if min_confidence > 0:
                    result.blocks = [b for b in result.blocks if b.confidence >= min_confidence]
                    result.text = "\n".join(b.text for b in result.blocks)
                return result
            except DependencyError as exc:
                last_error = exc
                continue
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    if last_error is not None:
        raise last_error
    raise DependencyError(
        "没有任何可用的 OCR 后端",
        hint=f"推荐安装: {pip_hint('rapidocr-onnxruntime onnxruntime')}",
    )
