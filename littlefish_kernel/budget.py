"""用量明细（K4）：三个 token 字段分开计价。

AstrBot 只给 token 数（input_other / input_cached / output），不给钱，
也不给限额。缓存命中的输入价通常只有普通输入的 1/4，混着算会把账算错，
所以这里三档分开乘。
"""

from __future__ import annotations

import datetime
import time
from typing import Any

from . import config, db


def price_for(model: str) -> dict[str, float]:
    prices = config.cfg()["budget"]["prices"]
    for key, val in prices.items():
        if key != "default" and key and key.lower() in (model or "").lower():
            return val
    return prices["default"]


def compute_cost(model: str, input_other: int, input_cached: int, output: int) -> float:
    p = price_for(model)
    return round(
        (input_other * p["input"] + input_cached * p["cached"] + output * p["output"]) / 1_000_000,
        6,
    )


async def record(
    *,
    model: str,
    input_other: int = 0,
    input_cached: int = 0,
    output: int = 0,
    provider: str = "",
    scope: str = "",
    ref: str = "",
    feature: str = "llm",
) -> dict[str, Any]:
    cost = compute_cost(model, input_other, input_cached, output)
    conn = await db.connect()
    await conn.execute(
        "INSERT INTO usage(ts, provider, model, input_other, input_cached, output, cost, scope, ref, feature)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), provider, model, input_other, input_cached, output, cost, scope, ref, feature),
    )
    await conn.commit()
    db.append_jsonl("usage", "usage", {
        "ts": int(time.time()), "model": model, "in": input_other,
        "cached": input_cached, "out": output, "cost": cost, "scope": scope,
        "feature": feature,
    })
    return {"cost": cost}


async def today() -> dict[str, Any]:
    now = int(time.time())
    d = datetime.datetime.fromtimestamp(now)
    since = int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS cost,"
        " COALESCE(SUM(input_other),0) AS io, COALESCE(SUM(input_cached),0) AS ic,"
        " COALESCE(SUM(output),0) AS o"
        " FROM usage WHERE ts >= ? AND COALESCE(scope, '') != 'test'",
        (since,),
    )
    row = dict(await cur.fetchone())
    cfg = config.cfg()["budget"]
    limit = float(cfg.get("daily_limit") or 0)
    row["limit"] = limit
    row["ratio"] = (row["cost"] / limit) if limit else 0.0
    row["exceeded"] = bool(limit) and row["cost"] >= limit
    row["warn"] = bool(limit) and row["ratio"] >= float(cfg.get("warn_ratio", 0.8))
    return row


async def blocked() -> bool:
    cfg = config.cfg()["budget"]
    if not cfg.get("enabled"):
        return False
    t = await today()
    return t["exceeded"]


# ------------------------------------------------------------ 明细

def _since(days: int) -> int:
    now = datetime.datetime.now()
    return int((now - datetime.timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())


async def by_day(days: int = 7) -> list[dict[str, Any]]:
    conn = await db.connect()
    rows = await db.fetchall(
        "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS calls,"
        " COALESCE(SUM(cost),0) AS cost,"
        " COALESCE(SUM(input_other),0) AS input_other,"
        " COALESCE(SUM(input_cached),0) AS input_cached,"
        " COALESCE(SUM(output),0) AS output"
        " FROM usage WHERE ts >= ? AND COALESCE(scope, '') != 'test'"
        " GROUP BY day ORDER BY day DESC",
        (_since(days),),
    )
    return rows


async def by_model(days: int = 7) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost),0) AS cost,"
        " COALESCE(SUM(input_other),0) AS input_other,"
        " COALESCE(SUM(input_cached),0) AS input_cached,"
        " COALESCE(SUM(output),0) AS output"
        " FROM usage WHERE ts >= ? GROUP BY model ORDER BY cost DESC",
        (_since(days),),
    )


async def by_feature(days: int = 7) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT COALESCE(NULLIF(feature,''),'llm') AS feature, COUNT(*) AS calls,"
        " COALESCE(SUM(cost),0) AS cost"
        " FROM usage WHERE ts >= ? GROUP BY feature ORDER BY cost DESC",
        (_since(days),),
    )


async def recent(limit: int = 10) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT ts, model, feature, scope, input_other, input_cached, output, cost"
        " FROM usage ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def cache_ratio(days: int = 7) -> float:
    """缓存命中率：命中的输入占总输入的比例。这个数掉下来，账单会立刻涨。"""
    rows = await db.fetchall(
        "SELECT COALESCE(SUM(input_other),0) AS io, COALESCE(SUM(input_cached),0) AS ic"
        " FROM usage WHERE ts >= ?",
        (_since(days),),
    )
    io, ic = (rows[0]["io"], rows[0]["ic"]) if rows else (0, 0)
    total = io + ic
    return round(ic / total, 3) if total else 0.0


async def feature_today(feature: str) -> dict[str, Any]:
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS cost FROM usage"
        " WHERE ts >= ? AND COALESCE(NULLIF(feature,''),'llm') = ?",
        (_since(1), feature),
    )
    return dict(await cur.fetchone())


async def allow(feature: str = "llm") -> bool:
    """某个功能今天还能不能花钱。0 = 不限。

    全局总额和单功能额都看：单功能超了只停这一个功能，不影响她聊天。
    """
    cfg = config.cfg()["budget"]
    if not cfg.get("enabled"):
        return True
    limit = float(cfg.get("daily_limit") or 0)
    if limit:
        t = await today()
        if t["cost"] >= limit:
            return False
    per = (cfg.get("per_feature") or {}).get(feature) or 0
    if float(per or 0):
        f = await feature_today(feature)
        if f["cost"] >= float(per):
            return False
    return True
