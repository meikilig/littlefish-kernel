"""自我卡（K3）：她对自己的叙述。

前两张卡记的都是"你"——你是谁、你说了什么。没有一张卡记"它自己"。
这一张由她自己写（llm_tool: self_note），**只在新会话第一次唤醒时读一次**。
每轮都灌进去 = 洗脑，她会变成一段自我描述的复读机。
"""

from __future__ import annotations

import time
import uuid

from . import config, db


async def add(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty self note")
    conn = await db.connect()
    now = int(time.time())
    # 重复只刷新时间，不堆同义句
    cur = await conn.execute("SELECT id FROM self_note WHERE text = ?", (text,))
    row = await cur.fetchone()
    if row:
        await conn.execute(
            "UPDATE self_note SET last_seen_at = ?, hits = hits + 1 WHERE id = ?",
            (now, row["id"]),
        )
        await conn.commit()
        return {"id": row["id"], "dup": True}

    nid = f"s{uuid.uuid4().hex[:12]}"
    await conn.execute(
        "INSERT INTO self_note(id, text, created_at, last_seen_at, hits) VALUES(?,?,?,?,0)",
        (nid, text, now, now),
    )
    await conn.commit()

    # 上限：超了就把最久没被想起的挪走（归档，不删）
    limit = int(config.cfg()["self_note"]["max_entries"])
    cur = await conn.execute("SELECT COUNT(*) AS c FROM self_note")
    if (await cur.fetchone())["c"] > limit:
        await conn.execute(
            "DELETE FROM self_note WHERE id IN ("
            " SELECT id FROM self_note ORDER BY hits ASC, last_seen_at ASC LIMIT ?)",
            ((await _count(conn)) - limit,),
        )
        await conn.commit()
    db.append_jsonl("self", "notes", {"ts": now, "id": nid, "text": text})
    return {"id": nid, "dup": False}


async def _count(conn) -> int:
    cur = await conn.execute("SELECT COUNT(*) AS c FROM self_note")
    return (await cur.fetchone())["c"]


async def list_all(limit: int = 40) -> list[dict]:
    conn = await db.connect()
    cur = await conn.execute(
        "SELECT * FROM self_note ORDER BY last_seen_at DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def render_once(conversation_key: str) -> str:
    """同一个会话只给一次。给过就记在 kv 里。"""
    key = f"self.shown.{conversation_key}"
    if await db.kv_get(key):
        return ""
    notes = await list_all()
    await db.kv_set(key, int(time.time()))
    if not notes:
        return ""
    lines = "\n".join(f"- {n['text']}" for n in notes)
    return f"## 你对自己的看法\n{lines}"
