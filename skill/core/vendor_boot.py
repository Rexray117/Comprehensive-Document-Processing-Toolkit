"""把技能自带的纯 Python 依赖目录 ``vendor/`` 接入 ``sys.path``。

设计取舍
--------
本技能的 HTML / PDF / 编码探测能力依赖 5 个纯 Python 包（pypdf、beautifulsoup4、
soupsieve、markdownify、filetype、charset-normalizer）。这些包要么不方便在目标
机器上 pip 安装（离线、无编译环境、宿主自带解释器不让装），要么装错版本后
行为漂移。因此仓库直接随包分发它们的通用 wheel 内容（``vendor/``，纯 Python、
跨平台、无编译产物），并在导入时按需挂到 ``sys.path`` 末尾。

为什么是 ``append`` 而不是 ``insert(0)``
----------------------------------------
用户机器上若已 `pip install` 过真实包（可能有 C 加速的 charset-normalizer），
应以真实安装为准；内置副本只负责「一个都装不上时仍能跑」。所以本目录在
``sys.path`` 末位，只有 import 失败后才会命中。

副作用与可关闭性
----------------
* 幂等：重复调用不会重复插入。
* ``vendor/`` 不存在时静默跳过（例如用户自行删除了该目录）。
* 设置环境变量 ``OMNI_NO_VENDOR=1`` 可完全禁用，用于排查「到底是谁提供的包」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 内置依赖目录（技能根/vendor）
VENDOR_DIR = Path(__file__).resolve().parents[1] / "vendor"

#: 该目录里分发的包，供 doctor / 文档展示
VENDORED_PACKAGES = (
    "pypdf",
    "bs4",
    "soupsieve",
    "markdownify",
    "filetype",
    "charset_normalizer",
)


def bootstrap() -> bool:
    """把 ``vendor/`` 加入 ``sys.path``；返回本次是否真的新增了路径。"""
    if os.environ.get("OMNI_NO_VENDOR", "").strip() in ("1", "true", "yes"):
        return False
    if not VENDOR_DIR.is_dir():
        return False
    path = str(VENDOR_DIR)
    if path in sys.path:
        return False
    sys.path.append(path)
    return True


def vendor_available() -> bool:
    """``vendor/`` 目录是否存在（不代表包一定能 import）。"""
    return VENDOR_DIR.is_dir()


__all__ = ["VENDOR_DIR", "VENDORED_PACKAGES", "bootstrap", "vendor_available"]
