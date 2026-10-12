"""OCR 子包：多后端可插拔的图像文字识别。"""

from __future__ import annotations

from ocr.engines import (
    OCRBlock,
    OCRResult,
    available_backends,
    find_tesseract,
    prepare_image,
    recognize,
)

__all__ = [
    "OCRBlock",
    "OCRResult",
    "recognize",
    "available_backends",
    "find_tesseract",
    "prepare_image",
]
