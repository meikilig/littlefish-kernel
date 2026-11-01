"""睡前那一页（L1-10）：夜里把今天过一遍。

触发不是"到点就跑"，而是三条腿：

1. 00:05 —— 她 24:00 催主人去睡之后，屋里安静了，她把今天写成日记；
2. 开机补跑 —— 电脑晚上关了没写成，下次开机把落下的那天补上（最多 3 天）；
3. `nightly.tick` 本身也幂等：同一天写过就跳过。

写到哪里：桌上「日记 YYYY-MM」那本（正文进本子，**不进注入预算**）；
只有她判断"今天真学到了/真说错了"的那 0-2 句，才进记忆卡；"以后该怎么改"进自我卡。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

from . import budget, config, db, desk, memory, persona, selfcard
from .log import log

MAX_CATCHUP_DAYS = 3
MATERIAL_MAX = 12000
DIARY_MAX = 600
LEARN_MAX = 2
FIX_MAX = 1
LEARN_CHARS = 40
FIX_CHARS = 30

SYSTEM = (
    "现在是深夜，屋里没人了。"
    "你要把今天过一遍，写一页日记，并且只挑出真该记住的东西。"
    "日记是你写给自己的，不需要对谁交代，也不许写得像汇报。"
)

PROMPT = """今天是 {day}。下面是今天所有的对话，按时间排：

{text}
{actions}
现在写一页日记。只输出一个 JSON 对象，不要别的话，也不要代码块：

{{"mood":"一两个词","diary":"正文","learned":["..."],"fixed":["..."]}}

规矩：
- diary 80-200 字，第一人称，写具体的人和事、你当时怎么想；不写"今天很充实"这种空话，不编没发生的事。
- learned 0-2 条，每条 ≤{lc} 字。只写"今天真学到了 / 真说错了什么"；没有就给 []，不要凑。
- fixed 0-1 条，≤{fc} 字。写"我以后该怎么做"，要具体到行为。
- 主人说的话和你说的话都算今天的事；别把别人的话当成你说的。
"""

_chat: Callable[[str, str], Awaitable[str]] | None = None
# 补跑任务的强引用：`asyncio.create_task` 的结果如果没人拿住，可能被 GC 掉，
# 长睡（180s）的任务尤其容易被回收 —— 2026-11-01 实测踩过。
_catchup_task: "asyncio.Task | None" = None


# ---------------------------------------------------------------- 接线

def set_chat(fn: Callable[[str, str], Awaitable[str]] | None) -> None:
    """由 AstrBot 那边的内核插件把"调一次模型"的能力接进来。"""
    global _chat
    _chat = fn


def chat_ready() -> bool:
    return _chat is not None


# ---------------------------------------------------------------- 日期

def day_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def _bounds(day: str) -> tuple[int, int]:
    start = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
    return start, start + 86400


def _shift(day: str, days: int) -> str:
    return day_of(int(time.mktime(time.strptime(day, "%Y-%m-%d"))) + days * 86400)


def last_night() -> str:
    """刚结束的那一天：00:05 跑的时候是昨天；早上补跑也是昨天。"""
    return day_of(time.time() - 86400)


# ---------------------------------------------------------------- 取材

async def _labels() -> dict[str, str]:
    try:
        return {str(k): str(v) for k, v in (await persona.addresses()).items()}
    except Exception:
        return {}


async def material(day: str) -> dict:
    """把某一天的对话和动作捞出来，拼成一段材料。"""
    start, end = _bounds(day)
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT text, speaker, is_self, created_at FROM memory "
        "WHERE created_at >= ? AND created_at < ? AND archived = 0 "
        "ORDER BY created_at",
        (start, end),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    labels = await _labels()
    owner = str(config.cfg()["owner"].get("qq") or "")

    lines: list[str] = []
    for r in rows:
        text = str(r["text"] or "").strip()
        if not text:
            continue
        stamp = time.strftime("%H:%M", time.localtime(int(r["created_at"])))
        spk = str(r["speaker"] or "")
        if r["is_self"]:
            who = "我"
        elif spk and spk == owner:
            who = "主人"
        elif spk and spk in labels:
            who = labels[spk]
        elif spk:
            who = f"朋友{spk[-4:]}"
        else:
            who = "某人"
        lines.append(f"[{stamp}] {who}：{text}")

    text = "\n".join(lines)
    if len(text) > MATERIAL_MAX:
        text = "…（前面省掉了）\n" + text[-MATERIAL_MAX:]

    return {"day": day, "n": len(rows), "chars": len(text), "text": text,
            "actions": await _actions(day)}


async def _actions(day: str) -> str:
    start, end = _bounds(day)
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT topic, COUNT(*) AS n FROM events WHERE ts >= ? AND ts < ? GROUP BY topic",
        (start, end),
    )
    counts = {str(r["topic"]): int(r["n"]) for r in await cur.fetchall()}
    bits: list[str] = []
    if counts.get("bili.running"):
        bits.append(f"出门去了 B站 {counts['bili.running']} 趟")
    if counts.get("code.used"):
        bits.append(f"用了 {counts['code.used']} 次暗号")
    if counts.get("outreach.request"):
        bits.append(f"主动找人说 {counts['outreach.request']} 次")
    if counts.get("closing.request"):
        bits.append(f"想收尾 {counts['closing.request']} 次")
    return ("\n今天还发生了：" + "；".join(bits) + "。") if bits else ""


# ---------------------------------------------------------------- 生成

def build_prompt(day: str, mat: dict) -> str:
    return PROMPT.format(day=day, text=mat["text"], actions=mat.get("actions", ""),
                         lc=LEARN_CHARS, fc=FIX_CHARS)


def parse(reply: str) -> dict:
    """模型回什么都要能收住：优先 JSON，收不住就把整段当日记。"""
    raw = str(reply or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    data: dict[str, Any] = {}
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        try:
            loaded = json.loads(raw[i:j + 1])
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    if not data:
        data = {"diary": raw}

    diary = str(data.get("diary") or "").strip()[:DIARY_MAX]
    mood = str(data.get("mood") or "").strip()[:20]
    learned = [str(x).strip()[:LEARN_CHARS] for x in (data.get("learned") or [])
               if str(x).strip()][:LEARN_MAX]
    fixed = [str(x).strip()[:FIX_CHARS] for x in (data.get("fixed") or [])
             if str(x).strip()][:FIX_MAX]
    if not diary and not learned and not fixed:
        return {"ok": False, "error": "这次什么都没写出来"}
    return {"ok": True, "mood": mood, "diary": diary, "learned": learned, "fixed": fixed}


# ---------------------------------------------------------------- 落地

def _book_ref(day: str) -> str:
    """日记按月一本，别把桌面撑满。"""
    month = day[:7]
    want = f"日记 {month}"
    for b in desk.list_books():
        if str(b.get("title") or "").strip() == want:
            return str(b["slug"])
    made = desk.new_book(want, visibility="private", limit=desk.HARD_LIMIT)
    if not made.get("ok"):
        return ""
    return str(made["slug"])


async def apply(day: str, data: dict, when: str = "睡前") -> dict:
    out: dict[str, Any] = {"ok": True, "day": day, "learned": 0, "fixed": 0, "diary_chars": 0}

    diary = str(data.get("diary") or "").strip()
    if diary:
        ref = _book_ref(day)
        if not ref:
            out.update(ok=False, error="日记本拿不出来")
            return out
        wrote = desk.append_page(ref, diary, at=when)
        if not wrote.get("ok"):
            # 一个月写满了：换一本接着写，不覆盖旧页
            desk.seal(ref)
            made = desk.new_book(f"日记 {day[:7]}-下", visibility="private",
                                 limit=desk.HARD_LIMIT)
            if made.get("ok"):
                wrote = desk.append_page(str(made["slug"]), diary, at=when)
        if not wrote.get("ok"):
            out.update(ok=False, error=wrote.get("error", "日记没写进去"))
            return out
        out["diary_chars"] = len(re.sub(r"\s", "", diary))
        out["book"] = wrote.get("slug")

    for text in data.get("learned") or []:
        try:
            await memory.add(
                text, platform="qq", scope="qq", kind="self", is_self=True,
                importance=55, reasons=["睡前回顾"],
            )
            out["learned"] += 1
        except Exception as exc:
            log.warning(f"[nightly] learned 写不进记忆：{exc}")

    for text in data.get("fixed") or []:
        try:
            await selfcard.add(text)
            out["fixed"] += 1
        except Exception as exc:
            log.warning(f"[nightly] fixed 写不进自我卡：{exc}")

    return out


# ---------------------------------------------------------------- 跑一次

async def run(day: str = "", force: bool = False, when: str = "睡前") -> dict:
    if not chat_ready():
        return {"ok": False, "skipped": "no_chat", "day": day or last_night()}
    day = day or last_night()
    if not force and await db.kv_get(f"nightly.done.{day}"):
        return {"ok": True, "skipped": "done", "day": day}

    mat = await material(day)
    if mat["n"] == 0:
        await db.kv_set(f"nightly.done.{day}", {"at": int(time.time()), "n": 0,
                                                "skipped": "no_talk"})
        await db.kv_set("nightly.last", {"day": day, "at": int(time.time()), "n": 0,
                                         "skipped": "no_talk"})
        log.info(f"[nightly] {day} 没人说话，跳过")
        return {"ok": True, "skipped": "no_talk", "day": day, "n": 0}

    prompt = build_prompt(day, mat)
    try:
        reply = await _chat(SYSTEM, prompt)  # type: ignore[misc]
    except Exception as exc:
        log.warning(f"[nightly] {day} 调模型失败：{exc}")
        return {"ok": False, "error": f"调模型失败：{exc}", "day": day}

    data = parse(reply)
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error", "没解析出东西"), "day": day}

    res = await apply(day, data, when=when)
    if not res.get("ok"):
        return res

    inp = (len(SYSTEM) + len(prompt)) // 2
    outp = len(str(reply)) // 2
    cost = 0.0
    try:
        rec = await budget.record(
            model="nightly", provider="", input_other=inp, output=outp,
            feature="nightly", scope="nightly", ref=day,
        )
        cost = float(rec.get("cost") or 0.0) if isinstance(rec, dict) else 0.0
    except Exception as exc:
        log.warning(f"[nightly] 记账失败：{exc}")

    stamp = {"day": day, "at": int(time.time()), "n": mat["n"], "cost": round(cost, 4),
             "diary_chars": res.get("diary_chars"), "learned": res.get("learned"),
             "fixed": res.get("fixed"), "mood": data.get("mood")}
    await db.kv_set(f"nightly.done.{day}", stamp)
    await db.kv_set("nightly.last", stamp)
    log.info(f"[nightly] {day} 写完：{res.get('diary_chars')} 字，学 {res.get('learned')} 条，"
             f"改 {res.get('fixed')} 条，¥{cost:.4f}")
    return {"ok": True, **stamp}


# ---------------------------------------------------------------- 触发

async def tick() -> dict:
    """00:05 的任务：写昨天。"""
    if not config.cfg().get("nightly", {}).get("enabled", True):
        return {"ok": True, "skipped": "off"}
    res = await run()
    if res.get("skipped") == "no_chat":
        log.warning("[nightly] 没有接模型通道，今晚这页跳过")
    return res


async def catch_up(delay: float = 0.0) -> dict:
    """开机补跑：`nightly.last` 落后的那几天补上，最多 3 天。"""
    if delay:
        await asyncio.sleep(delay)
    if not chat_ready():
        log.warning("[nightly] 补跑：模型通道还没接上，跳过")
        return {"ok": False, "skipped": "no_chat"}
    try:
        last = (await db.kv_get("nightly.last")) or {}
    except Exception:
        last = {}
    last_day = str(last.get("day") or "")
    today = day_of()
    # 没有记录：只补昨天（更早的没材料也没意义）
    start = _shift(last_day, 1) if last_day else _shift(today, -1)
    todo: list[str] = []
    d = start
    while d < today:
        todo.append(d)
        d = _shift(d, 1)
    todo = todo[-MAX_CATCHUP_DAYS:]
    if not todo:
        log.info(f"[nightly] 补跑检查：没有落下的（last={last_day or '无'} today={today}）")
        return {"ok": True, "days": []}
    log.info(f"[nightly] 补跑检查：要补 {todo}")
    done: list[dict] = []
    for d in todo:
        res = await run(day=d, when="补记")
        done.append(res)
        if not res.get("ok") and res.get("skipped") != "done":
            break
    if done:
        log.info(f"[nightly] 开机补跑 {len(done)} 天："
                 + ", ".join(f"{x.get('day')}={x.get('skipped') or ('ok' if x.get('ok') else x.get('error'))}"
                             for x in done))
    return {"ok": True, "days": done}


def schedule_catch_up(delay: float = 180.0) -> None:
    """开机排一次补跑。**必须留强引用**，否则任务可能被 GC 掉（实测踩过）。"""
    global _catchup_task
    try:
        _catchup_task = asyncio.create_task(catch_up(delay=delay))
        log.info(f"[nightly] 补跑已排：{delay:.0f}s 后检查")
    except Exception as exc:
        log.warning(f"[nightly] 补跑没排上：{exc}")


async def status() -> dict:
    """面板/体检看的状态。"""
    try:
        last = (await db.kv_get("nightly.last")) or {}
    except Exception:
        last = {}
    today = day_of()
    prev = day_of(time.time() - 86400)
    done_prev = bool(await db.kv_get(f"nightly.done.{prev}"))
    return {
        "enabled": bool(config.cfg().get("nightly", {}).get("enabled", True)),
        "chat_ready": chat_ready(),
        "last": last,
        "yesterday": prev,
        "yesterday_done": done_prev,
        "today": today,
        "pending": (not done_prev) and (last.get("day") != prev),
    }
