"""littlefish kernel —— 本体：人设卡 + 记忆卡 + 自我卡。

这一层不依赖任何一级功能。QQ 掉了、B站 关了，它照样能读写自己的记忆。
一级插件只通过 `littlefish_kernel` 的公开函数和 `bus` 事件通信。
"""

from __future__ import annotations

from . import (abilities, bookshelf, budget, bus, caps, compat, config, console, db, memory,
               nightly, ops, persona, presence, scheduler, selfcard, think, vitals)
from .log import log

__version__ = "0.1.7"
__all__ = ["abilities", "bookshelf", "budget", "bus", "caps", "compat", "config", "console", "db",
           "memory", "nightly", "ops", "persona", "presence", "scheduler", "selfcard",
           "think", "vitals", "log", "init", "shutdown", "status"]

_ready = False


async def init(with_console: bool = True, with_scheduler: bool = True) -> None:
    """起内核。宿主用默认值；只读工具（check_switch / snapshot）传 False False。

    为什么要有这两个开关：体检脚本只要读库，却会顺手起 scheduler 并对生产库跑维护任务、
    再去抢 6197 端口（AstrBot 在线时必然 10048）—— 号称只读，其实在动。2026-10-31 修。
    """
    global _ready
    if _ready:
        return
    config.ensure_dirs()
    await db.connect()

    # 先把宿主摸一遍：升了 AstrBot 而我们还按旧契约写，要在这里就喊出来
    host = compat.cached()
    if host["missing"]:
        log.error("[littlefish] " + compat.report())
        for m in host["missing"]:
            log.error(f"[littlefish] 缺失契约：{m}")
    elif not host["known"]:
        log.warning("[littlefish] " + compat.report())
    else:
        log.info("[littlefish] " + compat.report())
    try:
        compat.save(config.ROOT)
    except Exception as exc:
        log.warning(f"[littlefish] 宿主探测结果落盘失败：{exc}")

    if with_scheduler:
        scheduler.register_defaults()
        scheduler.start()
        # 开机补跑：主人晚上关机会错过 00:05，启动一会儿后再查（先让插件把模型通道接上）
        delay = float(config.cfg().get("nightly", {}).get("catchup_delay_sec", 180))
        nightly.schedule_catch_up(delay)
    if with_console:
        await console.start()
    _ready = True
    log.info(f"[littlefish] kernel {__version__} ready at {config.ROOT}")


async def shutdown() -> None:
    global _ready
    await console.stop()
    scheduler.shutdown()
    await db.close()
    _ready = False


async def status() -> dict:
    st = await memory.stats()
    today = await budget.today()
    return {
        "version": __version__,
        "root": str(config.ROOT),
        "character": config.cfg()["character"],
        "memory": st,
        "usage_today": today,
        "scheduler_running": scheduler.get().running,
        "bus": bus.topics(),
        "ready": _ready,
        "host": compat.cached(),
        "host_report": compat.report(),
    }
