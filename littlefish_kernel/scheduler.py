"""调度（K6）：AstrBot 没有插件级 scheduler，这里自己起一个。

只在启动时按代码注册任务（不让任务定义散落在各家数据库里），
停机期间错过的任务在启动后补跑一次。
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, memory, nightly, vitals
from .log import log

_scheduler: AsyncIOScheduler | None = None


def get() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    return _scheduler


def start() -> None:
    sched = get()
    if sched.running:
        return
    sched.start()
    log.info("[littlefish] scheduler started")


def shutdown() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def every_minutes(func: Callable[[], Awaitable], minutes: int, job_id: str):
    return get().add_job(
        func, IntervalTrigger(minutes=minutes), id=job_id, replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=300,
    )


def daily(func: Callable[[], Awaitable], hour: int, job_id: str, minute: int = 0):
    return get().add_job(
        func, CronTrigger(hour=hour, minute=minute), id=job_id, replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )


# ------------------------------------------------------------ 内置任务


async def job_daily_decay() -> None:
    res = await memory.decay_once()
    log.info(f"[littlefish] daily decay: {res}")


async def job_maintain() -> None:
    """定期体检：只在异常时说话，不打扰。

    2026-10-31 修：原来的条件写成 `if cfg["max_chars"] and 条数超限`，
    而 max_chars 默认 0（=不限制字数）→ **条数告警永远不会触发**。
    容量要分两个维度看：条数、以及真正注入时占的字数。
    """
    st = await memory.stats()
    cfg = config.cfg()["memory"]
    if cfg["max_records"] and st["total"] > cfg["max_records"]:
        log.warning(f"[littlefish] 记忆条数 {st['total']} 超过上限 {cfg['max_records']}")
    if cfg["max_chars"] and st["total"] > 0:
        # 粗估：平均每条正文长度 × 条数，超过 warn_chars 的 80% 就提一句
        used = int(st.get("avg_chars", 0)) * int(st["total"])
        if used >= float(cfg["warn_chars"]) * 0.8:
            log.warning(f"[littlefish] 记忆正文约 {used} 字，接近告警线 {cfg['warn_chars']}")


def register_defaults() -> None:
    cfg = config.cfg()
    daily(job_daily_decay, int(cfg["memory"]["daily_hour"]), "littlefish.daily_decay")
    every_minutes(
        job_maintain, max(1, int(cfg["memory"]["maintain_interval_min"])), "littlefish.maintain"
    )
    # 睡前那一页：00:05 写昨天（主人 24:00 睡、00:30 关机，这段时间正好）
    ncfg = dict(cfg.get("nightly") or {})
    if ncfg.get("enabled", True):
        daily(
            nightly.tick, int(ncfg.get("hour", 0)), "littlefish.nightly",
            minute=int(ncfg.get("minute", 5)),
        )
    # 身体：定期把"读时推演"的结果落盘（数值不靠它算，它只是存档 + 睡/醒留痕）
    vcfg = dict(cfg.get("vitals") or {})
    if vcfg.get("enabled", True):
        every_minutes(
            vitals.tick, max(1, int(vcfg.get("tick_minutes", 5))), "littlefish.vitals"
        )
