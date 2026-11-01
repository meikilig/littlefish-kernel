"""记忆卡（K2）：评分、检索、衰减、渲染。

三条硬规矩，都是从旧系统踩出来的：
1. **纯函数打分，不调模型**。可控、免费、可测；模型打分会让同一条记忆今天 80 明天 20。
2. **一条记忆一次写**。不重写整份文件，崩溃最多丢当前这一条。
3. **隐私靠配额，不靠脱敏**。给 B站 的渲染配额里 QQ 私事的位置是 0，
   即使代码写错也漏不出去；脱敏是第二道保险。

评分/衰减规则沿用旧系统已验证的权重（用户已跑两天、手感认可），
只把存储和检索换掉。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime
from typing import Any, Iterable

from . import config, db
from .log import log

CLARITY_ORDER = ["vivid", "clear", "fuzzy", "trace"]
CLARITY_CN = {"vivid": "清晰", "clear": "清楚", "fuzzy": "模糊", "trace": "印象"}

# 平台配额：决定"谁的事能给谁看"。0 就是完全看不到。
QUOTA = {
    "qq": {"first": 1, "anchor": 5, "initial": 6, "owner": 8, "recent": 8,
           "bili": 4, "self": 4, "fuzzy": 4},
    "bili": {"first": 0, "anchor": 4, "initial": 0, "owner": 0, "recent": 6,
             "bili": 8, "self": 3, "fuzzy": 3},
    "pet": {"first": 0, "anchor": 3, "initial": 2, "owner": 4, "recent": 4,
            "bili": 2, "self": 4, "fuzzy": 2},
}

# ---------------------------------------------------------------- 敏感与脱敏

_REDACT_RULES = [
    (re.compile(r"(?i)\b(sk-[a-z0-9_\-]{10,})"), "[密钥]"),
    (re.compile(r"(?i)(password|passwd|pwd|密码)\s*[:=：]?\s*\S+"), r"\1: [已隐藏]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机]"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[证件]"),
    (re.compile(r"https?://\S{20,}"), "[链接]"),
    (re.compile(r"(?i)(token|secret|api[_-]?key)\s*[:=]\s*\S+"), r"\1: [已隐藏]"),
]


def redact(text: str) -> str:
    if not text:
        return ""
    if not config.cfg()["privacy"]["redact"]:
        return text
    out = text
    for pat, repl in _REDACT_RULES:
        out = pat.sub(repl, out)
    return out


# ---------------------------------------------------------------- 分词（bigram）


_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-z0-9_]{2,}")


def tokenize(text: str) -> list[str]:
    """中文按二元组切，英文数字按词切。不引第三方分词库，结果可解释。"""
    if not text:
        return []
    low = text.lower()
    tokens: list[str] = []
    tokens.extend(_WORD.findall(low))
    chars = _CJK.findall(text)
    for i in range(len(chars) - 1):
        tokens.append(chars[i] + chars[i + 1])
    # 单字也进（查询只有一个字时还能命中）
    if len(chars) == 1:
        tokens.append(chars[0])
    return tokens


# ---------------------------------------------------------------- 评分


_STRONG = ["分手", "绝交", "对不起", "抱歉", "谢谢", "喜欢", "爱你", "想你", "讨厌",
           "开心", "难过", "生气", "害怕", "哭", "抱抱", "生日", "约定", "答应"]
_AGREE = ["答应你", "约好", "说好", "以后", "下次", "明天见", "一定"]
_SORRY = ["对不起", "抱歉", "我错了", "别生气", "原谅"]


def score(
    text: str,
    *,
    platform: str = "qq",
    speaker_is_owner: bool = False,
    is_private: bool = False,
    addressed_to_me: bool = False,
    kind: str = "chat",
    bili_score: int = 0,
    is_favorite: bool = False,
    is_diary: bool = False,
) -> tuple[int, list[str]]:
    """返回 (1~100, 理由列表)。纯函数。"""
    s = 8
    why = ["基础+8"]
    if speaker_is_owner:
        add = 42 if is_private else 32
        s += add
        why.append(f"主人{'私聊' if is_private else '群里'}+{add}")
    if addressed_to_me:
        s += 8
        why.append("直接对她说+8")

    emo = 0
    for w in _STRONG:
        if w in text:
            emo += 8
    for w in _AGREE:
        if w in text:
            emo += 3
    emo += min(4, text.count("!") + text.count("！"))
    emo = min(24, emo)
    if emo:
        s += emo
        why.append(f"情绪+{emo}")
    if any(w in text for w in _SORRY):
        s += 14
        why.append("道歉/和好+14")
    if any(w in text for w in _AGREE):
        s += 16
        why.append("约定+16")
    if kind == "self":
        s += 5
        why.append("关于自己+5")
    if kind == "bili":
        s += max(0, min(18, bili_score))
        why.append(f"B站+{max(0, min(18, bili_score))}")
    if is_favorite:
        s += 8
        why.append("收藏+8")
    if is_diary:
        s += 4
        why.append("日记+4")
    if len(text) < 6:
        s -= 12
        why.append("太短-12")
    return max(1, min(100, s)), why


# ---------------------------------------------------------------- 写入


def _uid() -> str:
    return f"{int(time.time()*1000):x}{uuid.uuid4().hex[:8]}"


async def _index_tokens(conn, mem_id: str, text: str) -> None:
    toks = set(tokenize(text))
    if not toks:
        return
    await conn.executemany(
        "INSERT OR IGNORE INTO memory_token(token, mem_id) VALUES(?, ?)",
        [(t, mem_id) for t in toks],
    )


async def add(
    text: str,
    *,
    platform: str = "qq",
    scope: str = "qq",
    kind: str = "chat",
    title: str = "",
    detail: str = "",
    tags: Iterable[str] = (),
    speaker: str = "",
    group_id: str = "",
    conversation_key: str = "",
    is_self: bool = False,
    sensitive: bool = False,
    pinned: bool = False,
    addressed_to_me: bool = False,
    speaker_is_owner: bool = False,
    is_private: bool = False,
    importance: int | None = None,
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    cfg = config.cfg()
    text = (text or "").strip()
    if not text:
        raise ValueError("empty memory text")
    if sensitive or kind == "sensitive":
        sensitive = True
    stored = redact(text) if config.cfg()["privacy"]["redact"] else text

    if importance is None:
        importance, why = score(
            text,
            platform=platform,
            speaker_is_owner=speaker_is_owner,
            is_private=is_private,
            addressed_to_me=addressed_to_me,
            kind=kind,
        )
    else:
        why = reasons or ["手工指定"]

    now = int(time.time())
    mid = _uid()
    conn = await db.connect()

    # 同一个人在 24 小时内说的同一句话，不重复记一条：只刷新时间、加召回次数。
    # 跨天的重复要保留——重复本身是频率，不是噪音。
    cur = await conn.execute(
        """SELECT * FROM memory
           WHERE archived = 0 AND text = ? AND IFNULL(speaker, '') = ? AND created_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (stored, speaker or "", now - 86400),
    )
    dup = await cur.fetchone()
    if dup is not None:
        rec = dict(dup)
        await conn.execute(
            "UPDATE memory SET last_seen_at = ?, recall_count = recall_count + 1 WHERE id = ?",
            (now, rec["id"]),
        )
        await conn.commit()
        rec["last_seen_at"] = now
        rec["recall_count"] = int(rec.get("recall_count") or 0) + 1
        rec["deduped"] = True
        return rec

    await conn.execute(
        """INSERT INTO memory(
            id, platform, scope, kind, title, text, detail, tags, importance, reasons,
            emotional, pinned, anchor, clarity, created_at, last_seen_at, decay_steps,
            recall_count, speaker, group_id, conversation_key, is_self, sensitive,
            is_first, first_tier, merged_from, archived
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            mid, platform, scope, kind, title, stored, detail,
            json.dumps(list(tags), ensure_ascii=False), importance,
            json.dumps(why, ensure_ascii=False), 0, 1 if pinned else 0, 0,
            "vivid", now, now, 0, 0, speaker, group_id, conversation_key,
            1 if is_self else 0, 1 if sensitive else 0, 0, "", "[]", 0,
        ),
    )
    await _index_tokens(conn, mid, stored)
    await conn.commit()

    if not await db.kv_get("memory.first_master_ts"):
        await db.kv_set("memory.first_master_ts", now)

    # 全量归档：只追加，永不删，永不注入
    db.append_jsonl("memory", "full", {
        "id": mid, "ts": now, "platform": platform, "scope": scope, "kind": kind,
        "title": title, "text": stored, "detail": detail, "importance": importance,
        "speaker": speaker, "group_id": group_id, "conversation_key": conversation_key,
        "is_self": is_self, "sensitive": sensitive,
    })

    await maybe_mark_first(mid)
    return {"id": mid, "importance": importance, "reasons": why}


async def maybe_mark_first(mem_id: str | None = None) -> None:
    """「最初」不是「重要」：只从和主人互动的最早几条里抽，抽中就一直留着。

    旧实现按 90% 概率给窗口内每条都打标 → 装上当天几乎所有记忆都成了「最初」，
    「最初」这个桶就失去意义，还顺带把衰减全冻住了。这里改成：
    只在**最早的 first_slots 条**里抽签，抽不中就不再考虑后面的。
    用记录 id 做散列，同一条记忆每次判定一致（确定性抽签）。
    """
    cfg = config.cfg()["memory"]
    conn = await db.connect()
    first_ts = await db.kv_get("memory.first_master_ts")
    slots = int(cfg.get("first_slots", 3))
    window_end = None
    if first_ts:
        window_end = int(first_ts) + int(cfg["first_window_days"]) * 86400

    sql = (
        "SELECT id, created_at FROM memory "
        "WHERE archived = 0 AND sensitive = 0 AND is_self = 0"
    )
    params: tuple = ()
    if window_end is not None:
        sql += " AND created_at <= ?"
        params = (window_end,)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params = params + (max(slots * 3, slots),)
    cur = await conn.execute(sql, params)
    candidates = [dict(r) for r in await cur.fetchall()]
    if not candidates:
        return

    cur = await conn.execute(
        "SELECT COUNT(*) AS c FROM memory WHERE is_first = 1 AND archived = 0"
    )
    cnt = (await cur.fetchone())["c"]
    if cnt >= slots:
        return

    for row in candidates:
        if cnt >= slots:
            break
        days = (int(row["created_at"]) - int(first_ts or row["created_at"])) / 86400.0
        if days < 0 or days > cfg["first_window_days"]:
            continue
        if days < 10:
            p, tier = 0.90, "first10"
        elif days < 20:
            p, tier = 0.55, "mid10"
        elif days < 30:
            p, tier = 0.30, "late10"
        else:
            p, tier = 0.03, "after"
        h = int(hashlib.sha1(row["id"].encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        if h >= p:
            continue
        await conn.execute(
            "UPDATE memory SET is_first = 1, first_tier = ?, "
            "clarity = CASE WHEN clarity IN ('fuzzy','trace') THEN 'clear' ELSE clarity END "
            "WHERE id = ?",
            (tier, row["id"]),
        )
        cnt += 1
    await conn.commit()


# ---------------------------------------------------------------- 检索


async def search(query: str, k: int = 8, scope: str | None = None) -> list[dict]:
    """二元组倒排取交集，按 (命中数, 重要度, 新近) 排序。"""
    toks = list(dict.fromkeys(tokenize(query)))[:24]
    if not toks:
        return []
    conn = await db.connect()
    ph = ",".join("?" for _ in toks)
    sql = (
        f"SELECT m.*, COUNT(t.token) AS hits FROM memory_token t "
        f"JOIN memory m ON m.id = t.mem_id "
        f"WHERE t.token IN ({ph}) AND m.archived = 0 AND m.sensitive = 0 "
    )
    args: list[Any] = list(toks)
    if scope:
        sql += "AND m.scope = ? "
        args.append(scope)
    sql += "GROUP BY m.id ORDER BY hits DESC, m.importance DESC, m.last_seen_at DESC LIMIT ?"
    args.append(k * 3)
    cur = await conn.execute(sql, args)
    rows = [dict(r) for r in await cur.fetchall()]
    now = int(time.time())
    for r in rows:
        age_days = max(0.0, (now - r["last_seen_at"]) / 86400.0)
        r["_rank"] = r["hits"] * 3 + r["importance"] / 10.0 - math.log1p(age_days)
    rows.sort(key=lambda r: r["_rank"], reverse=True)
    return rows[:k]


async def touch(mem_ids: Iterable[str]) -> None:
    """被想起来的记忆：换新时间 + 计数 +1（不清空、不重写别的字段）。"""
    ids = list(mem_ids)
    if not ids:
        return
    conn = await db.connect()
    now = int(time.time())
    await conn.executemany(
        "UPDATE memory SET last_seen_at = ?, recall_count = recall_count + 1 WHERE id = ?",
        [(now, i) for i in ids],
    )
    await conn.commit()


async def pin(mem_id: str, pinned: bool = True) -> None:
    conn = await db.connect()
    await conn.execute(
        "UPDATE memory SET pinned = ?, clarity = 'vivid' WHERE id = ?",
        (1 if pinned else 0, mem_id),
    )
    await conn.commit()


async def forget(mem_id: str) -> None:
    """软删除：归档，不物理删。删错了还能找回来。"""
    conn = await db.connect()
    await conn.execute("UPDATE memory SET archived = 1 WHERE id = ?", (mem_id,))
    await conn.commit()


# ---------------------------------------------------------------- 衰减


def _day_start(ts: int) -> int:
    """本地零点。跨天按她所在时区算，不按 UTC。"""
    d = datetime.fromtimestamp(ts)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def retention(rec: dict, now: int, cfg: dict) -> float:
    imp = rec["importance"]
    r = imp + min(12, rec["recall_count"] * 2)
    age = (now - rec["last_seen_at"]) / 86400.0
    if age <= 7:
        r += 18
    if age <= 2:
        r += 8
    after = max(0.0, age - cfg["fuzzy_after_days"])
    if after > 0:
        r -= after * (0.12 if imp >= cfg["min_owner_importance"] else 0.35)
    if imp >= cfg["min_owner_importance"]:
        r = max(55.0, r)          # 主人的重要记忆不会掉到印象以下
    return r


def target_clarity(r: float) -> str:
    if r >= 60:
        return "vivid"
    if r >= 40:
        return "clear"
    if r >= 20:
        return "fuzzy"
    return "trace"


# ---------------------------------------------------------------- 容量出口
#
# 原则（用户 m03311 定的）：宁可让主人过目再删，也不许静默丢新的，更不许
# 为了腾地方把人挤到"初次相遇"头上。所以这里只做三件事：
#   1. 报数：现在多少条 / 多少字 / 到上限还有多远
#   2. 提名：按留存分从低到高列出"可以先走的"，永久保护的一律不提名
#   3. 执行：淘汰（软删）与合并（把几条碎记忆并成一条事件）都要人点头


def _protected(rec: dict, cfg: dict) -> str:
    """这条记忆为什么不能自动动它。空串 = 可以动。"""
    if rec.get("is_first"):
        return "最初"
    if rec.get("pinned"):
        return "置顶"
    if int(rec.get("importance") or 0) >= int(cfg["min_owner_importance"]):
        return "重要"
    return ""


async def capacity() -> dict[str, Any]:
    cfg = config.cfg()["memory"]
    st = await stats()
    total = int(st["total"])
    avg = float(st.get("avg_chars") or 0)
    est = int(total * avg)
    max_rec = int(cfg.get("max_records") or 0)
    max_chars = int(cfg.get("max_chars") or 0)
    ratios = []
    if max_rec:
        ratios.append(total / max_rec)
    if max_chars:
        ratios.append(est / max_chars)
    ratio = max(ratios) if ratios else 0.0
    if ratio >= 1.0:
        level = "full"
    elif ratio >= 0.9:
        level = "high"
    elif ratio >= 0.8:
        level = "warn"
    else:
        level = "ok"
    return {
        "total": total,
        "avg_chars": round(avg, 1),
        "est_chars": est,
        "max_records": max_rec,
        "max_chars": max_chars,
        "ratio": round(ratio, 4),
        "level": level,
        "warn_chars": int(cfg.get("warn_chars") or 0),
    }


async def evict_candidates(limit: int = 20) -> list[dict[str, Any]]:
    """按留存分从低到高，列出"可以先走"的。保护的一律不进来。"""
    cfg = config.cfg()
    mcfg = cfg["memory"]
    now = int(time.time())
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM memory WHERE archived = 0 AND sensitive = 0")
    rows = [dict(r) for r in await cur.fetchall()]
    out = []
    for r in rows:
        if _protected(r, mcfg):
            continue
        out.append({
            "id": r["id"],
            "text": _body(r),
            "clarity": r["clarity"],
            "importance": r["importance"],
            "score": round(retention(r, now, mcfg), 1),
        })
    out.sort(key=lambda x: x["score"])
    return out[: max(1, int(limit))]


async def evict(ids: Iterable[str], by: str = "owner") -> dict[str, Any]:
    """软删一批：archived=1，保留原文可查。保护的会被拒绝并报回来。"""
    cfg = config.cfg()["memory"]
    conn = await db.connect()
    done, skipped = [], []
    for mid in ids:
        cur = await conn.execute("SELECT * FROM memory WHERE id = ?", (mid,))
        rec = await cur.fetchone()
        if rec is None:
            skipped.append({"id": mid, "why": "没这条"})
            continue
        rec = dict(rec)
        why = _protected(rec, cfg)
        if why:
            skipped.append({"id": mid, "why": f"{why}的不自动删"})
            continue
        await conn.execute(
            "UPDATE memory SET archived = 1, detail = ? WHERE id = ?",
            ((rec.get("detail") or "") + f"\n[淘汰 {time.strftime('%Y-%m-%d %H:%M')} by {by}]", mid),
        )
        done.append(mid)
    await conn.commit()
    return {"evicted": done, "skipped": skipped, "total": len(done)}


async def merge(ids: Iterable[str], title: str = "") -> dict[str, Any]:
    """把几条碎记忆并成一条"事件"。源记录归档，新记录带 merged_from。

    只在"同一场景、同一说话人"之间合并 —— 跨群的合并等于把两个地方的
    事缝在一起，正是最容易说漏嘴的那种错误。
    """
    cfg = config.cfg()
    mcfg = cfg["memory"]
    conn = await db.connect()
    rows = []
    for mid in ids:
        cur = await conn.execute("SELECT * FROM memory WHERE id = ? AND archived = 0", (mid,))
        rec = await cur.fetchone()
        if rec is not None:
            rows.append(dict(rec))
    if len(rows) < 2:
        return {"ok": False, "why": "至少要两条"}

    groups = {str(r.get("group_id") or "") for r in rows}
    speakers = {str(r.get("speaker") or "") for r in rows}
    if len(groups) > 1 or len(speakers) > 1:
        return {"ok": False, "why": "不在同一个场景/不是同一个人，不能合并"}

    parts = []
    seen = set()
    for r in sorted(rows, key=lambda x: x["created_at"]):
        b = _body(r)
        if b and b not in seen:
            seen.add(b)
            parts.append(b)
    text = "；".join(parts)
    imp = max(int(r["importance"]) for r in rows)
    firsts = [r for r in rows if r["is_first"]]

    new = await add(
        text,
        platform=rows[0]["platform"],
        scope=rows[0]["scope"],
        kind=rows[0]["kind"],
        title=title or (rows[0].get("title") or "合并的一件事"),
        speaker=rows[0]["speaker"],
        group_id=rows[0]["group_id"],
        conversation_key=rows[0]["conversation_key"],
        is_self=bool(rows[0]["is_self"]),
        pinned=any(bool(r["pinned"]) for r in rows),
        importance=imp,
        reasons=["合并事件"],
    )
    nid = new["id"]
    if firsts:
        await conn.execute(
            "UPDATE memory SET is_first = 1, first_tier = ? WHERE id = ?",
            (firsts[0].get("first_tier") or "", nid),
        )
        await conn.execute(
            "UPDATE memory SET is_first = 0 WHERE id IN (%s)"
            % ",".join("?" * len(firsts)),
            [r["id"] for r in firsts],
        )
    for r in rows:
        await conn.execute(
            "UPDATE memory SET archived = 1, merged_from = ?, detail = ? WHERE id = ?",
            (nid, (r.get("detail") or "") + f"\n[并入 {nid}]", r["id"]),
        )
    await conn.execute(
        "UPDATE memory SET merged_from = ? WHERE id = ?",
        (json.dumps([r["id"] for r in rows], ensure_ascii=False), nid),
    )
    await conn.commit()
    return {"ok": True, "id": nid, "merged": len(rows), "chars": len(text)}


async def trim_plan(target_ratio: float = 0.9) -> dict[str, Any]:
    """要回到 target_ratio 以内，至少得让多少条走。只算，不动手。"""
    cap = await capacity()
    max_rec = cap["max_records"]
    if not max_rec:
        return {"need": 0, "reason": "没设条数上限"}
    keep = int(max_rec * float(target_ratio))
    need = max(0, cap["total"] - keep)
    cands = await evict_candidates(limit=max(need, 1))
    return {
        "need": need,
        "keep": keep,
        "total": cap["total"],
        "suggest": cands[:need],
        "cannot": "候选不够时说明剩下的都是保护记忆，得人工合并或放宽上限",
    }


async def decay_once(now: int | None = None) -> dict[str, int]:
    """每天最多降一档。停机补跑按错过天数连续降，最多补 30 天。

    三个边界（都是旧系统踩过的坑）：
    1. 日期按本地零点切，不按 UTC —— 否则凌晨 4 点维护会跨两个 24h 桶。
    2. 第一次运行只打标记不降档 —— 否则新装那天全部记忆白掉一档。
    3. 今天刚记的事今天不降档（decay_min_age_days）。
    """
    cfg = config.cfg()["memory"]
    now = now or int(time.time())
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT * FROM memory WHERE archived = 0 AND pinned = 0"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    last = int(await db.kv_get("memory.last_decay_day", 0) or 0)
    today = _day_start(now)
    if not last:
        await db.kv_set("memory.last_decay_day", today)
        return {"decayed": 0, "forgotten": 0}
    if last > today:
        # 标记跑到未来了（测试时传过未来的时间，或系统时钟被调回）。
        # 不修的话 `missed` 恒为 0，衰减会**一直停摆**，谁也不会发现。
        log.warning(f"[memory] last_decay_day 在未来（{last} > {today}），重置为今天")
        await db.kv_set("memory.last_decay_day", today)
        return {"decayed": 0, "forgotten": 0, "reset_future_marker": 1}
    missed = min(30, max(0, (today - last) // 86400))
    if missed == 0:
        if today > last:
            await db.kv_set("memory.last_decay_day", today)
        return {"decayed": 0, "forgotten": 0}

    min_age = float(cfg.get("decay_min_age_days", 1)) * 86400
    decayed = forgotten = 0
    steps_total = max(1, missed)
    for r in rows:
        if (now - r["created_at"]) < min_age:
            continue                      # 今天刚记的，今天不降
        rr = retention(r, now, cfg)
        tgt = target_clarity(rr)
        idx_now = CLARITY_ORDER.index(r["clarity"]) if r["clarity"] in CLARITY_ORDER else 0
        idx_tgt = CLARITY_ORDER.index(tgt)
        if r["is_first"] and idx_tgt > 1:
            idx_tgt = 1                      # 「最初」最差保持清楚
        if idx_tgt <= idx_now:
            continue
        new_idx = min(idx_tgt, idx_now + steps_total * cfg["decay_per_day"])
        # 超过遗忘期才允许掉到 trace 之外（归档）
        if new_idx >= len(CLARITY_ORDER):
            new_idx = len(CLARITY_ORDER) - 1
        new_clarity = CLARITY_ORDER[new_idx]
        if (
            new_clarity == "trace"
            and (now - r["last_seen_at"]) / 86400.0 > cfg["forget_after_days"]
            and not r["is_first"]
        ):
            await conn.execute(
                "UPDATE memory SET clarity = 'trace', archived = 1 WHERE id = ?", (r["id"],)
            )
            forgotten += 1
        else:
            await conn.execute(
                "UPDATE memory SET clarity = ?, decay_steps = decay_steps + ? WHERE id = ?",
                (new_clarity, steps_total, r["id"]),
            )
            decayed += 1
    await conn.commit()
    await db.kv_set("memory.last_decay_day", today)
    return {"decayed": decayed, "forgotten": forgotten}


# ---------------------------------------------------------------- 渲染


_SPEAKER_PREFIXES = ("和主人：", "和主人:", "主人：", "主人:")


def _body(rec: dict) -> str:
    """挑注入用的正文。

    旧系统的 title 常写成"和主人：xxx"（标题里带说话人），直接拼进提示词会很脏；
    事件类记忆（最初/约定/冲突）反过来只有 title 有信息量，正文可能就一句"你好"。
    """
    title = (rec["title"] or "").strip()
    text = (rec["text"] or "").strip()
    for p in _SPEAKER_PREFIXES:
        if text.startswith(p):
            text = text[len(p):].strip()
        if title.startswith(p):
            title = title[len(p):].strip()
    if rec["kind"] in ("firstMeet", "first_meet", "promise", "conflict", "emotion") and title:
        return title
    return text or title


def _fmt(rec: dict, owner_qq: str = "", owner_label: str = "主人") -> str:
    when = datetime.fromtimestamp(rec["created_at"]).strftime("%m-%d")
    who = rec["speaker"] or ("我" if rec["is_self"] else "")
    if who and owner_qq and who == owner_qq:
        who = owner_label or "主人"
    head = f"[{when}"
    if who:
        head += f" {who}"
    head += f"·{CLARITY_CN.get(rec['clarity'], rec['clarity'])}]"
    body = _body(rec)
    if rec["clarity"] in ("fuzzy", "trace"):
        body = body[:40] + ("…" if len(body) > 40 else "")
    return f"{head} {body}"


def scene_ok(rec: dict, ctx: dict | None, iso: dict) -> bool:
    """这条记忆能不能出现在"这个场景"里。

    为什么需要：她把 A 群记住的事拿到 B 群说，比"B站 泄露主人 QQ"更容易发生，
    而且伤人。所以按"这条记忆是在哪种场景里发生的"做硬隔离，而不是靠模型自觉。

    ctx = {"is_group": bool, "group_id": str}，None 表示不做场景过滤（内部体检/预览用）。
    """
    if ctx is None or not iso.get("enabled", True):
        return True
    home = str(ctx.get("group_id") or "")
    rec_group = str(rec.get("group_id") or "")
    if ctx.get("is_group"):
        if not rec_group:
            # 私聊里发生的事，默认不进群
            return bool(iso.get("private_in_group", False))
        if rec_group == home:
            return True
        # 别的群里发生的事，默认不带过来
        return bool(iso.get("cross_group", False))
    # 私聊：群里的记忆默认不提（避免把群里的话转述给另一个人）
    if not rec_group:
        return True
    return bool(iso.get("group_in_private", False))


async def render(
    platform: str = "qq",
    budget: int | None = None,
    ctx: dict | None = None,
) -> str:
    """按平台配额挑记忆，拼成注入块。配额为 0 的类别直接不放。"""
    cfg = config.cfg()
    quota = QUOTA.get(platform, QUOTA["qq"])
    # 显式传 0 只能是 0（以前 `budget or ...` 会把 0 当成"没传"，静默换回默认值）
    budget = budget if budget is not None else cfg["memory"]["inject_max_chars"].get(platform, 2000)
    include_qq = cfg["privacy"]["include_qq_in_bili"]
    iso = dict(cfg["privacy"].get("isolation") or {})

    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM memory WHERE archived = 0 AND sensitive = 0")
    rows = [dict(r) for r in await cur.fetchall()]

    def scopes_ok(r: dict) -> bool:
        if platform == "bili" and not include_qq and r["scope"] == "qq":
            return False
        return scene_ok(r, ctx, iso)

    rows = [r for r in rows if scopes_ok(r)]
    owner_qq = str(cfg["owner"].get("qq") or "")
    owner_label = str(cfg["owner"].get("label") or "主人")

    def is_owner(r: dict) -> bool:
        return bool(owner_qq) and r["speaker"] == owner_qq

    # 桶里按"留存分"从高到低排：位置紧张时被挤掉的永远是快忘记的那些，
    # 而不是恰好在列表末尾的"初次相遇"。
    _now = int(time.time())
    _mcfg = cfg["memory"]

    def rank(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda r: retention(r, _now, _mcfg), reverse=True)

    buckets: list[tuple[str, list[dict], int]] = [
        ("最初的事", rank([r for r in rows if r["is_first"]]), quota["first"]),
        # 置顶的必须真的进提示词 —— 早先 render 根本没看 pinned 字段，
        # 只有容量淘汰看它，于是"置顶"成了个不生效的按钮（2026-10-31 修）。
        ("你特别记着的", rank([r for r in rows if r["pinned"]]), 6),
        ("约定与道歉", rank([r for r in rows if r["anchor"] or r["kind"] == "anchor"]), quota["anchor"]),
        ("主人", rank([r for r in rows if is_owner(r)]), quota["owner"]),
        ("最近聊到的", rank(rows), quota["recent"] + quota["initial"]),
        ("B站 上的", rank([r for r in rows if r["scope"] == "bili"]), quota["bili"]),
        ("印象模糊的", rank([r for r in rows if r["clarity"] in ("fuzzy", "trace")]), quota["fuzzy"]),
    ]

    out: list[str] = []
    used: set[str] = set()

    HEAD = "## 你记得的事\n"

    def _rendered(blocks: list[str]) -> str:
        return HEAD + "\n".join(blocks)

    def _fits(blocks: list[str]) -> bool:
        return len(_rendered(blocks)) <= budget

    for title, items, limit in buckets:
        if limit <= 0:
            continue
        picked: list[str] = []
        for r in items:
            if r["id"] in used:
                continue
            line = f"- {_fmt(r, owner_qq, owner_label)}"
            block = f"## {title}\n" + "\n".join(picked + [line])
            # 判据必须是"整串长度 ≤ budget"：标题行、行间换行、外层那句 ## 你记得的事
            # 都要算进去。早先只累加正文、标题行漏算，于是配置写 2200、实际注入 2228，
            # check_switch 因此永远 NO-GO（2026-10-31 修）。
            if not _fits(out + [block]):
                break
            picked.append(line)
            used.add(r["id"])
            if len(picked) >= limit:
                break
        if picked:
            out.append(f"## {title}\n" + "\n".join(picked))
    if not out:
        return ""
    return _rendered(out)


# ---------------------------------------------------------------- 统计


async def stats() -> dict[str, Any]:
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT clarity, COUNT(*) AS c FROM memory WHERE archived = 0 GROUP BY clarity"
    )
    clarity = {r["clarity"]: r["c"] for r in await cur.fetchall()}
    cur = await conn.execute("SELECT COUNT(*) AS c FROM memory WHERE archived = 0")
    total = (await cur.fetchone())["c"]
    cur = await conn.execute("SELECT COUNT(*) AS c FROM memory WHERE archived = 1")
    archived = (await cur.fetchone())["c"]
    cur = await conn.execute("SELECT COUNT(*) AS c FROM self_note")
    self_notes = (await cur.fetchone())["c"]
    cur = await conn.execute("SELECT COUNT(*) AS c FROM memory WHERE is_first = 1")
    first = (await cur.fetchone())["c"]
    cur = await conn.execute(
        "SELECT COALESCE(AVG(LENGTH(IFNULL(text,''))), 0) AS avg FROM memory WHERE archived = 0"
    )
    avg_chars = (await cur.fetchone())["avg"] or 0
    return {
        "total": total,
        "archived": archived,
        "clarity": clarity,
        "first": first,
        "self_notes": self_notes,
        "avg_chars": round(float(avg_chars), 1),
    }
