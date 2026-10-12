"""omni-file-toolkit 核心框架层。

对外只暴露少量稳定符号，便于 handler 以最短的 import 路径使用：

    from core.base import BaseHandler, action, ActionContext
    from core.registry import load_handlers, resolve_handler
    from core.dispatcher import Request, run
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
