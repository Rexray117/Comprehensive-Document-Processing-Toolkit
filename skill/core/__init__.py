"""omni-file-toolkit 核心框架层。

对外只暴露少量稳定符号，便于 handler 以最短的 import 路径使用：

    from core.base import BaseHandler, action, ActionContext
    from core.registry import load_handlers, resolve_handler
    from core.dispatcher import Request, run

导入本包时会先把技能自带的 ``vendor/``（纯 Python 依赖副本）挂到 ``sys.path`` 末尾，
见 :mod:`core.vendor_boot`。这一步必须早于任何 ``require("bs4")`` 之类的懒加载，
所以放在包初始化里，而不是散落在各个 handler 中。
"""

from __future__ import annotations

from core.vendor_boot import bootstrap as _bootstrap_vendor

#: 本次进程是否成功挂载了内置依赖目录（仅用于 doctor 展示，不影响功能）
_VENDOR_ACTIVE = _bootstrap_vendor()

__version__ = "1.1.0"
__all__ = ["__version__"]
