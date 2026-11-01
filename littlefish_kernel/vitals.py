"""身体（P0）：精神值 + 生物钟，读时推演。

**这一层不存"心跳"，只存两个数：精神值、以及那个数是什么时候算出来的。**
任何一次读都先用时间差补算 —— 关机 10 小时，开机那次读就等于"照着生物钟过了 10 小时"。
所以没有 tick 循环、没有后台线程、不调模型；定时任务只是定期把结果落盘（顺便记睡/醒事件）。

数据放在 `db.kv["vitals.state"]`：不建表、不动 SCHEMA_VERSION，零迁移风险。
设计文档：见 README「疲劳度」一节
"""

from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime
from typing import Any

from . import config, db
from .log import log

try:  # 上海没有夏令时，但代码不该赌这个
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    TZ = None  # type: ignore

KV = "vitals.state"
STEP = 60.0            # 推演步长（秒）：1 分钟粒度，36h 也就 2160 步
MAX_SEGMENTS = 4096    # 防呆：步数超了直接放弃推演

STATE_V = 1

# ------------------------------------------------------------ 配置


def cfg() -> dict[str, Any]:
    return dict(config.cfg().get("vitals") or {})


def _num(c: dict, key: str, default: float) -> float:
    """配置里什么都可能被写成字符串，一律兜底。"""
    try:
        v = c.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _windows(c: dict) -> list[dict]:
    """睡眠窗口：先只有"夜里长睡"和"午睡"，以后想加就往后追加一条。"""
    raw = c.get("windows")
    if not isinstance(raw, list) or not raw:
        raw = [
            {"name": "夜里长睡", "start": "23:30", "end": "07:30", "recovery_cap": 50},
            {"name": "午睡", "start": "13:00", "end": "13:40", "recovery_cap": 10},
        ]
    out = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        s = _hhmm(w.get("start"), 23 * 60 + 30)
        e = _hhmm(w.get("end"), 7 * 60 + 30)
        if s == e:
            continue
        out.append({
            "name": str(w.get("name") or "睡眠"),
            "start": s,
            "end": e,
            "rate": _num(w, "rate", _num(c, "sleep_rate", 6.0)),
            "cap": _num(w, "recovery_cap", 50.0),
            "on": bool(w.get("enabled", True)),
        })
    return out


def _hhmm(raw: Any, default: int) -> int:
    try:
        parts = str(raw).strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 24 and 0 <= m < 60:
            return (h % 24) * 60 + m
    except Exception:
        pass
    return default


# ------------------------------------------------------------ 时间


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=TZ)


def _minute_of_day(ts: float) -> int:
    d = _local(ts)
    return d.hour * 60 + d.minute


def _in_window(t: float, win: dict) -> bool:
    m = _minute_of_day(t)
    s, e = win["start"], win["end"]
    if s < e:
        return s <= m < e
    return m >= s or m < e      # 跨零点


def _window_at(t: float, wins: list[dict]) -> dict | None:
    for w in wins:
        if w["on"] and _in_window(t, w):
            return w
    return None


# ------------------------------------------------------------ 状态


def _blank(now: float) -> dict:
    return {
        "v": STATE_V,
        "vigor": 10.0,
        "at": float(now),
        "last_talk": float(now),
        "asleep": {"on": False, "since": 0.0, "seg_start": 0.0, "credit": 0.0,
                   "seg_credit": 0.0, "win": "", "wake_until": 0.0},
        "lock": {"on": False, "since": 0.0},
        "anomaly": {"n": 0, "last": None, "frozen": False},
        "effects": [],
        "stats": {"wakes": 0, "nights": 0, "spent": 0.0, "overload": 0.0},
    }


def _fix(st: dict, now: float) -> dict:
    """把读到的 state 补成完整形状（旧版本 / 手改 / 半截写入都能活）。"""
    base = _blank(now)
    if not isinstance(st, dict):
        return base
    out = dict(base)
    out.update({k: v for k, v in st.items() if k in base or k == "v"})
    for k in ("asleep", "lock", "anomaly", "stats"):
        if not isinstance(out.get(k), dict):
            out[k] = dict(base[k])
        else:
            merged = dict(base[k])
            merged.update({kk: vv for kk, vv in out[k].items() if kk in merged})
            out[k] = merged
    if not isinstance(out.get("effects"), list):
        out["effects"] = []
    try:
        out["vigor"] = max(-100.0, min(100.0, float(out.get("vigor") or 0.0)))
    except (TypeError, ValueError):
        out["vigor"] = 10.0
    for k in ("at", "last_talk"):
        try:
            out[k] = float(out.get(k) or now)
        except (TypeError, ValueError):
            out[k] = float(now)
    # 注意：**不要**在这里把 at 夹到 now 以内。时钟被拨回去的时候，
    # at 会在 now 之后，那一瞬间正是我们要识别的东西（见 project 的 dt<0 分支）。
    return out


async def load(now: float | None = None) -> dict:
    now = float(now if now is not None else time.time())
    try:
        raw = await db.kv_get(KV, None)
    except Exception as exc:
        log.warning(f"[vitals] 读状态失败，按新身体重来：{exc}")
        raw = None
    return _fix(raw, now)


# ------------------------------------------------------------ 推演（纯函数）


def _bump(st: dict, kind: str, detail: Any) -> dict:
    a = dict(st.get("anomaly") or {"n": 0, "last": None, "frozen": False})
    a["n"] = int(a.get("n") or 0) + 1
    a["last"] = {"kind": kind, "detail": detail, "at": float(st.get("at") or 0)}
    if a["n"] >= 3:
        a["frozen"] = True
    return a


def _wake(st: dict, t: float, cfgd: dict) -> None:
    a = st["asleep"]
    if not a.get("on"):
        return
    dur = max(0.0, t - float(a.get("seg_start") or t))
    short = _num(cfgd, "short_sleep_minutes", 90) * 60
    if dur < short:
        # 没睡够 90 分钟：这段只按浅睡算，差额补回去
        want = _num(cfgd, "short_sleep_rate", 2.0) * dur / 3600.0
        delta = want - float(a.get("seg_credit") or 0.0)
        st["vigor"] = _clamp(st["vigor"] + delta)
        if delta:
            st["stats"]["wakes"] = int(st["stats"].get("wakes") or 0) + 1
    a["on"] = False
    a["since"] = 0.0
    a["seg_start"] = 0.0
    a["seg_credit"] = 0.0
    a["credit"] = 0.0
    a["win"] = ""
    a["wake_until"] = t + _num(cfgd, "resleep_minutes", 25) * 60


def _clamp(v: float) -> float:
    return max(-100.0, min(100.0, float(v)))


def _effect_mult(st: dict, t: float, cfgd: dict) -> float:
    """身上还生效的速率效果（咖啡这类），只作用在"醒着的消耗"上。"""
    mult = 1.0
    for e in st.get("effects") or []:
        r = e.get("rate") if isinstance(e, dict) else None
        if isinstance(r, dict) and float(r.get("until") or 0) > t:
            try:
                mult *= float(r.get("mult") or 1.0)
            except (TypeError, ValueError):
                pass
    return max(0.2, min(mult, 3.0))


def _settle_rebound(st: dict, t: float) -> None:
    keep = []
    for e in st.get("effects") or []:
        if not isinstance(e, dict):
            continue
        rb = e.get("rebound")
        if isinstance(rb, dict) and not rb.get("done") and float(rb.get("at") or 0) <= t:
            try:
                st["vigor"] = _clamp(st["vigor"] + float(rb.get("delta") or 0.0))
            except (TypeError, ValueError):
                pass
            rb = dict(rb)
            rb["done"] = True
            e = dict(e, rebound=rb)
        # 过期的 rate 效果清掉（rebound 还没结算的留着）
        r = e.get("rate")
        expired = isinstance(r, dict) and float(r.get("until") or 0) <= t
        pending_rb = isinstance(e.get("rebound"), dict) and not e["rebound"].get("done")
        if expired and not pending_rb:
            continue
        keep.append(e)
    st["effects"] = keep[:8]


def project(state: dict, now: float, cfgd: dict | None = None) -> dict:
    """纯函数：给定状态和"现在"，算出"现在"的状态。喂任意 now 都行（测试就这么跑）。"""
    c = dict(cfgd or {})
    now = float(now)
    st = _fix(copy.deepcopy(state), now)
    wins = _windows(c)
    awake_rate = _num(c, "awake_rate", 3.0)
    oversleep = _num(c, "oversleep_penalty", 0.8)
    onset = _num(c, "onset_at", 60.0)
    wake_at = _num(c, "wake_at", 100.0)
    lock_at = _num(c, "lock_at", -30.0)
    unlock_at = _num(c, "unlock_at", -10.0)
    quiet = _num(c, "quiet_minutes", 10.0) * 60
    max_h = _num(c, "max_dt_hours", 36.0)

    dt = now - st["at"]
    if dt < 0:
        # 时钟回拨：不扣不回，只对齐。绝不按负数积分。
        st["anomaly"] = _bump(st, "clock_back", dt)
        st["at"] = now
        return st
    if dt > max_h * 3600:
        # 关机很久 / 时间被拨快：宁可少扣，不可多扣
        st["anomaly"] = _bump(st, "dt_clamp", dt)
        dt = max_h * 3600

    if st["anomaly"].get("frozen"):
        st["at"] = now
        return st

    steps = int(dt // STEP) + (1 if dt % STEP else 0)
    if steps > MAX_SEGMENTS:  # pragma: no cover - dt 已被 max_h 卡住
        st["anomaly"] = _bump(st, "too_many_steps", steps)
        st["at"] = now
        return st

    t = st["at"]
    left = dt
    while left > 0:
        s = min(STEP, left)
        _advance(st, t, s, wins, c,
                 awake_rate, oversleep, onset, wake_at, quiet, now)
        t += s
        left -= s
        _settle_rebound(st, t)
    st["at"] = now
    st["vigor"] = _clamp(st["vigor"])

    # 锁：迟滞 20 点，防抖
    lk = st["lock"]
    if not lk.get("on") and st["vigor"] <= lock_at:
        lk["on"] = True
        lk["since"] = now
    elif lk.get("on") and st["vigor"] >= unlock_at:
        lk["on"] = False
        lk["since"] = 0.0
    return st


def _advance(st, t, s, wins, c, awake_rate, oversleep, onset, wake_at, quiet, now) -> None:
    win = _window_at(t, wins)
    a = st["asleep"]
    if a.get("on"):
        if win is None or st["vigor"] >= wake_at:
            _wake(st, t, c)
            return
        cap = float(win["cap"])
        room = cap - float(a.get("credit") or 0.0)
        if room <= 0:
            return                      # 睡到上限：可以继续睡，但不再回血
        gain = min(win["rate"] * s / 3600.0, room)
        st["vigor"] = _clamp(st["vigor"] + gain)
        a["credit"] = float(a.get("credit") or 0.0) + gain
        a["seg_credit"] = float(a.get("seg_credit") or 0.0) + gain
        return

    # 醒着
    rate = awake_rate * _effect_mult(st, t, c)
    st["vigor"] = _clamp(st["vigor"] - rate * s / 3600.0)
    if win is not None:
        # 窗口里还醒着 = 熬夜 / 硬撑，多扣一点
        st["vigor"] = _clamp(st["vigor"] - oversleep * s / 3600.0)
        can_sleep = (
            st["vigor"] <= onset
            and t >= float(a.get("wake_until") or 0.0)
            and (t - float(st.get("last_talk") or 0.0)) >= quiet
        )
        if can_sleep:
            a["on"] = True
            a["since"] = t
            a["seg_start"] = t
            a["seg_credit"] = 0.0
            a["credit"] = 0.0
            a["win"] = win["name"]
            st["stats"]["nights"] = int(st["stats"].get("nights") or 0) + 1


# ------------------------------------------------------------ 档位 / 文本


def band(state: dict, now: float | None = None, cfgd: dict | None = None) -> str:
    if state.get("asleep", {}).get("on"):
        return "asleep"
    v = float(state.get("vigor") or 0.0)
    c = dict(cfgd or {})
    if v <= _num(c, "lock_at", -30.0):
        return "empty"
    if v <= _num(c, "drowsy_at", 0.0):
        return "drowsy"
    return "full"


_LINES = {
    "asleep": "〖状态〗睡着了，睡得正沉",
    "empty": "〖状态〗熬得太狠了，脑子发木、眼睛睁不开，说话都不过脑子",
    "drowsy": "〖状态〗有点困了，眼皮发沉，回话会比平时短",
}


def prompt_line(snap: dict) -> str:
    """给模型看的一行。正常档返回空串 —— 正常是默认值，不需要说。"""
    b = str(snap.get("band") or "full")
    if b == "full":
        return ""
    return _LINES.get(b, "")


def strategy(snap: dict) -> dict:
    """建议，不是命令：给"多学习少陪聊"用。"""
    b = str(snap.get("band") or "full")
    if b == "asleep":
        return {"mode": "sleep", "allow_outreach": False, "reply_max_chars": 20,
                "suggest": "sleep", "reasons": ["asleep"]}
    if b == "empty":
        return {"mode": "rest", "allow_outreach": False, "reply_max_chars": 40,
                "suggest": "rest", "reasons": ["vigor<=lock_at"]}
    if b == "drowsy":
        return {"mode": "low_key", "allow_outreach": False, "reply_max_chars": 40,
                "suggest": "rest", "reasons": ["vigor<=drowsy_at"]}
    v = float(snap.get("vigor") or 0.0)
    if v >= 70:
        return {"mode": "full", "allow_outreach": True, "reply_max_chars": 0,
                "suggest": "talk", "reasons": []}
    return {"mode": "steady", "allow_outreach": True, "reply_max_chars": 0,
            "suggest": "read", "reasons": []}


def describe(state: dict, now: float, cfgd: dict | None = None) -> dict:
    """面板/命令用：数字 + 中文，一起给。"""
    c = dict(cfgd or {})
    b = band(state, now, c)
    v = float(state.get("vigor") or 0.0)
    fatigue = max(0, min(100, round(100 - v)))
    owes = max(0, round(-v))
    a = state.get("asleep") or {}
    win = _window_at(now, _windows(c))
    label = {"full": "正常", "drowsy": "发困", "empty": "透支（睡眠锁）", "asleep": "睡着"}[b]
    seg = ""
    if a.get("on") and a.get("since"):
        seg = f"，已睡 {int((now - float(a['since'])) // 60)} 分钟"
    return {
        "vigor": round(v, 1),
        "fatigue": fatigue,
        "owes": owes,
        "band": b,
        "label": label,
        "asleep": bool(a.get("on")),
        "since": a.get("since") or 0,
        "lock": bool((state.get("lock") or {}).get("on")),
        "window": (win or {}).get("name", ""),
        "detail": f"精神 {round(v, 1)}（疲劳 {fatigue}"
                  + (f"，倒欠 {owes} 点" if owes else "") + f"）· {label}{seg}",
    }


# ------------------------------------------------------------ 写（单写者）


_lock = asyncio.Lock()


async def _save(st: dict) -> None:
    await db.kv_set(KV, st)


async def _consume_spend(st: dict, now: float, lo: float) -> None:
    """把这段时间里"发过的话/出过的门"折算成消耗。只读 events，不改它。

    lo 必须是**推演之前**的 at：project 会把 at 推到 now，算晚了就一条都捞不着。
    """
    try:
        rows = await db.fetchall(
            "SELECT ts, payload FROM events WHERE topic=? AND ts>? AND ts<=?",
            ("vitals.spend", float(lo), now + 1),
        )
    except Exception as exc:
        log.debug(f"[vitals] spend 汇总跳过：{exc}")
        return
    total = 0.0
    for row in rows or []:
        try:
            # db.fetchall 返回 dict（不是元组）；两种都认，免得以后再踩
            payload = row.get("payload") if isinstance(row, dict) else row[1]
            if isinstance(payload, str):
                import json as _json
                payload = _json.loads(payload)
            total += float((payload or {}).get("units") or 0.0)
        except Exception:
            continue
    if total > 0:
        # 一次补算最多扣 20 点，防止异常数据一次把人扣死
        total = min(total, 20.0)
        st["vigor"] = _clamp(st["vigor"] - total)
        st["stats"]["spent"] = round(float(st["stats"].get("spent") or 0.0) + total, 2)


async def _today_cost(now: float) -> float:
    """今天（本地 0 点起）花了多少钱。口径和 budget.today() 一致：排除 test 记账。"""
    try:
        d = _local(now).replace(hour=0, minute=0, second=0, microsecond=0)
        since = int(d.timestamp())
        rows = await db.fetchall(
            "SELECT COALESCE(SUM(cost),0) AS cost FROM usage"
            " WHERE ts >= ? AND COALESCE(scope, '') != 'test'",
            (since,),
        )
    except Exception as exc:
        log.debug(f"[vitals] 花费查询跳过：{exc}")
        return 0.0
    if not rows:
        return 0.0
    row = rows[0]
    try:
        return float((row.get("cost") if isinstance(row, dict) else row[0]) or 0.0)
    except Exception:
        return 0.0


async def _apply_overload(st: dict, now: float, lo: float, cfgd: dict | None = None) -> float:
    """超速疲劳：当天花超线，这段推演时间里额外多扣。

    **只改精神值，不碰 budget 的闸门** —— budget.daily_limit 一旦有数，
    L1-1 会直接 stop_event 拦掉回复；这里刻意用独立配置，两套额度互不干扰。
    睡着时不扣：睡觉是回血，罚睡着的她没道理。
    """
    try:
        c = cfgd if cfgd is not None else cfg()
        yuan = _num(c, "overload_yuan", 0.0)
        if yuan <= 0:
            return 0.0
        if (st.get("asleep") or {}).get("on"):
            return 0.0
        hours = max(0.0, (float(now) - float(lo)) / 3600.0)
        if hours <= 0:
            return 0.0
        if await _today_cost(now) < yuan:
            return 0.0
        rate = max(0.0, _num(c, "overload_per_hour", 6.0))
        cut = min(rate * hours, 12.0)   # 一次补算封顶，和 spend 的 20 点同一套防呆
        if cut <= 0:
            return 0.0
        st["vigor"] = _clamp(st["vigor"] - cut)
        st["stats"]["overload"] = round(float(st["stats"].get("overload") or 0.0) + cut, 2)
        log.debug(f"[vitals] 超速疲劳 -{round(cut, 2)} → {round(float(st['vigor']), 1)}")
        return cut
    except Exception as exc:
        log.debug(f"[vitals] 超速疲劳跳过：{exc}")
        return 0.0


async def settle(now: float | None = None) -> dict:
    """唯一写入口：推演到 now、汇总消耗、落盘。"""
    now = float(now if now is not None else time.time())
    async with _lock:
        st = await load(now)
        lo = float(st["at"])
        st = project(st, now, cfg())
        await _consume_spend(st, now, lo)
        await _apply_overload(st, now, lo)
        st["at"] = now
        await _save(st)
        return st


async def snapshot(now: float | None = None) -> dict:
    """只读快照（不落盘）：每轮注入都走这条 —— 读到的永远是"此刻"。"""
    now = float(now if now is not None else time.time())
    c = cfg()
    st = project(await load(now), now, c)
    return {
        "vigor": round(float(st["vigor"]), 1),
        "band": band(st, now, c),
        "asleep": bool(st["asleep"].get("on")),
        "lock": bool(st["lock"].get("on")),
        "line": prompt_line({"band": band(st, now, c)}),
        "strategy": strategy({"band": band(st, now, c), "vigor": st["vigor"]}),
        "effects": st.get("effects") or [],
        "state": st,
    }


async def spend(units: float, note: str = "") -> None:
    """记一笔活动消耗（只 INSERT，不抢写状态）。"""
    try:
        await db.log_event("vitals.spend", {"units": float(units), "note": note})
    except Exception as exc:
        log.debug(f"[vitals] spend 记录失败：{exc}")


async def touch(now: float | None = None) -> None:
    """有人说话：刷新"安静了多久"，别让正在聊天的她突然睡过去。"""
    now = float(now if now is not None else time.time())
    async with _lock:
        st = await load(now)
        st["last_talk"] = now
        await _save(st)


async def wake(reason: str = "") -> dict:
    """叫她起来（主人私聊 / 暗号 / 直呼其名）。叫醒 ≠ 睡饱。"""
    now = time.time()
    async with _lock:
        st = await load(now)
        st = project(st, now, cfg())
        _wake(st, now, cfg())
        st["vigor"] = _clamp(st["vigor"] - _num(cfg(), "wake_penalty", 3.0))
        st["at"] = now
        await _save(st)
        log.info(f"[vitals] 叫醒（{reason or '手动'}）→ 精神 {round(st['vigor'], 1)}")
        return describe(st, now, cfg())


async def sleep_at_will() -> dict:
    """主动去睡（主人让她去睡 / 她自己趴下）。"""
    now = time.time()
    async with _lock:
        st = await load(now)
        st = project(st, now, cfg())
        a = st["asleep"]
        if not a.get("on"):
            a.update({"on": True, "since": now, "seg_start": now,
                      "seg_credit": 0.0, "credit": 0.0, "win": "手动"})
            st["stats"]["nights"] = int(st["stats"].get("nights") or 0) + 1
        st["at"] = now
        await _save(st)
        return describe(st, now, cfg())


async def apply_effect(effect: dict) -> dict:
    """物品联动的唯一口子（P0 只留接口，咖啡延后）。"""
    now = time.time()
    eff = dict(effect or {})
    async with _lock:
        st = await load(now)
        st = project(st, now, cfg())
        inst = eff.get("instant") or {}
        if isinstance(inst, dict):
            try:
                st["vigor"] = _clamp(st["vigor"] + float(inst.get("delta") or 0.0))
            except (TypeError, ValueError):
                pass
        stored: dict[str, Any] = {
            "id": str(eff.get("id") or f"eff-{int(now)}"),
            "name": str(eff.get("name") or "东西"),
            "note": str(eff.get("note") or ""),
        }
        rate = eff.get("rate") or {}
        if isinstance(rate, dict) and rate:
            mins = max(1, min(int(_num(rate, "minutes", 90)), 240))
            stored["rate"] = {"mult": max(0.2, min(_num(rate, "mult", 1.0), 3.0)),
                              "until": now + mins * 60}
        rb = eff.get("rebound") or {}
        if isinstance(rb, dict) and rb:
            mins = max(1, min(int(_num(rb, "minutes", 90)), 240))
            stored["rebound"] = {"delta": _num(rb, "delta", 0.0), "at": now + mins * 60,
                                 "done": False}
        if eff.get("unlock"):
            st["lock"]["on"] = False
            st["lock"]["since"] = 0.0
        st["effects"] = (list(st.get("effects") or []) + [stored])[-8:]
        st["at"] = now
        await _save(st)
        return describe(st, now, cfg())


# ------------------------------------------------------------ 定时（只为落盘）


async def _record_tick(st: dict, now: float | None = None) -> None:
    """每 5 分钟留一条历史，append 一条事件就走。

    只给"看几天趋势"用：一天 288 条、payload 二十来字节，不想留就把
    config.vitals.history 设 false（记录和落盘是两件事，关它不影响推演）。
    """
    if not bool(cfg().get("history", True)):
        return
    try:
        now = float(now if now is not None else (st.get("at") or time.time()))
        await db.log_event("vitals.tick", {
            "v": round(float(st["vigor"]), 1),
            "band": band(st),
            "asleep": bool((st.get("asleep") or {}).get("on")),
            "lock": bool((st.get("lock") or {}).get("on")),
        })
    except Exception as exc:
        log.debug(f"[vitals] tick 历史没记上：{exc}")


async def tick() -> None:
    """每 5 分钟落一次盘：状态可读可回看，崩了也不丢。"""
    if not bool(cfg().get("enabled", True)):
        return
    try:
        st = await settle()
        log.debug(f"[vitals] tick vigor={round(float(st['vigor']), 1)}")
        await _record_tick(st)
    except Exception as exc:
        log.warning(f"[vitals] tick 失败（不影响对话）：{exc}")
