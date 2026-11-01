"""她现在在不在家。

为什么要单独一层：
    L1-2（B站）知道她什么时候出门、什么时候回来；
    L1-1（QQ）需要知道"这会儿要不要接话"。
    两个一级之间不许互相 import，所以中间放这一层，只存一个 kv。

写的人只有一个：L1-2（出门时 set_away，回来时 clear）。
读的人只有一个：L1-1（来消息时 get）。
到点了没人来清，get() 自己会把它当过期处理 —— 兜底，不等人。
"""

from __future__ import annotations

import time
from typing import Any

from . import db

KV = "presence"

# 她出门时别人找她，就回这一句（不回模型、不花钱、不产生记忆）
AWAY_REPLY = "我出门啦，一会儿回来"
# 回来时对主人说的第一句
RETURN_TEXT = "我回来啦"


def eta_clock(until: int | None) -> str:
    """把时间戳说成人话：17:05。"""
    if not until:
        return ""
    return time.strftime("%H:%M", time.localtime(int(until)))


async def set_away(minutes: int = 0, note: str = "", kind: str = "bili") -> dict[str, Any]:
    """标记她出门了。minutes<=0 表示不设归期（到时由调用方 clear）。"""
    now = int(time.time())
    ent = {
        "away": True,
        "since": now,
        "until": int(now + minutes * 60) if int(minutes or 0) > 0 else 0,
        "note": str(note or ""),
        "kind": str(kind or "bili"),
    }
    await db.kv_set(KV, ent)
    return ent


async def get() -> dict[str, Any]:
    """她现在在不在家。过了归期就当已经回来（顺手清掉）。"""
    ent = await db.kv_get(KV) or {}
    if not ent.get("away"):
        return {"away": False}
    until = int(ent.get("until") or 0)
    if until and int(time.time()) >= until:
        await db.kv_del(KV)
        return {"away": False, "expired": True, "until": until}
    ent["left_min"] = round((time.time() - int(ent.get("since") or 0)) / 60, 1)
    return ent


async def clear() -> dict[str, Any]:
    await db.kv_del(KV)
    return {"away": False}
