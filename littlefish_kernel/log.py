"""日志：优先用 AstrBot 的 logger（会路由到插件日志页），否则退回标准库。"""

from __future__ import annotations

import logging

try:  # pragma: no cover - 在 AstrBot 外部运行时走 fallback
    from astrbot.api import logger as _ab_logger  # type: ignore

    log = _ab_logger
except Exception:  # pragma: no cover
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("littlefish")
