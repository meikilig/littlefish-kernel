"""事件总线（K5）：一级插件之间唯一的通信方式。

硬规矩：L1 之间**不许互相 import**。B站 想说话，发一个 `outbound.request` 事件，
由 QQ 那个插件决定怎么发。这样拔掉任何一个 L1，其余照常跑。
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections import defaultdict
from typing import Any, Awaitable, Callable

from . import db

Handler = Callable[[str, dict[str, Any]], Awaitable[None] | None]

_handlers: dict[str, list[Handler]] = defaultdict(list)
_lock = asyncio.Lock()


def on(topic: str):
    """订阅。topic 支持 `bili.*` 这种通配。"""

    def deco(fn: Handler) -> Handler:
        _handlers[topic].append(fn)
        return fn

    return deco


def subscribe(topic: str, fn: Handler) -> None:
    _handlers[topic].append(fn)


def unsubscribe(topic: str, fn: Handler) -> None:
    if fn in _handlers.get(topic, []):
        _handlers[topic].remove(fn)


async def emit(topic: str, payload: dict[str, Any] | None = None, *, persist: bool = True) -> None:
    payload = payload or {}
    if persist:
        try:
            await db.log_event(topic, payload)
        except Exception:
            pass
    targets: list[Handler] = []
    for pattern, fns in _handlers.items():
        if pattern == topic or fnmatch.fnmatch(topic, pattern):
            targets.extend(fns)
    for fn in targets:
        try:
            res = fn(topic, payload)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:  # 一个订阅者炸了不能拖垮其他人
            try:
                from .log import log

                log.warning(f"bus handler failed for {topic}: {exc}")
            except Exception:
                pass


def topics() -> dict[str, int]:
    return {k: len(v) for k, v in _handlers.items()}
