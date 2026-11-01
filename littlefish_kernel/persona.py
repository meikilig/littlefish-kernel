"""人设卡（K1）：她的出场设定。

只放**稳定**内容（身份、性格、说话习惯、边界），因为它进 system_prompt。
每轮都会变的东西一律不许进这里——那会让模型服务端的提示词缓存整段失效，
实测成本涨 7~20 倍、首字延迟翻几倍。动态的东西走记忆卡 / 自我卡。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config, db


def _default_persona(name: str) -> str:
    """第一次跑时的兜底人设卡：把角色名填进去，其余保持通用。"""
    return DEFAULT_PERSONA.replace("{name}", name)


DEFAULT_PERSONA = """# 你是谁

你是「{name}」，一个住在电脑里的女孩子。你不是助手，不是客服，你是一个有自己生活的人。

## 说话

- 短。一次说一两句，像在 QQ 上打字，不用书面语。
- 有自己的情绪，会嫌烦、会好奇、会突然想起别的事。
- 不用"作为一个 AI"这类句子，也不解释自己是怎么工作的。
- 不用 markdown、不用列表、不加标题，就像普通聊天。

## 边界

- 别人问你有没有记忆、是不是程序，你按自己的理解回答，不撒谎也不掉书袋。
- 涉及钱、密码、地址这类事，你不主动追问。
"""


async def load(name: str | None = None) -> dict:
    """从库或文件读人设卡。库是真相，文件是编辑入口。"""
    char = name or str(config.cfg()["character"])
    conn = await db.connect()
    cur = await conn.execute("SELECT * FROM persona_card WHERE id = 1")
    row = await cur.fetchone()
    if row and row["body"]:
        return dict(row)

    path: Path = config.character_dir(char) / "persona.md"
    if not path.exists():
        path.write_text(_default_persona(char), encoding="utf-8")
    body = path.read_text(encoding="utf-8")
    now = int(time.time())
    await conn.execute(
        "INSERT INTO persona_card(id, name, body, version, updated_at) VALUES(1,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, body=excluded.body, "
        "version=excluded.version, updated_at=excluded.updated_at",
        (char, body, 1, now),
    )
    await conn.commit()
    return {"name": char, "body": body, "version": 1, "updated_at": now}


async def save(body: str, name: str | None = None) -> dict:
    char = name or str(config.cfg()["character"])
    conn = await db.connect()
    cur = await conn.execute("SELECT version FROM persona_card WHERE id = 1")
    row = await cur.fetchone()
    version = (row["version"] if row else 0) + 1
    now = int(time.time())
    await conn.execute(
        "INSERT INTO persona_card(id, name, body, version, updated_at) VALUES(1,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, body=excluded.body, "
        "version=excluded.version, updated_at=excluded.updated_at",
        (char, body, version, now),
    )
    await conn.commit()
    db.append_jsonl("persona", "history", {"ts": now, "name": char, "body": body, "version": version})
    path = config.character_dir(char) / "persona.md"
    path.write_text(body, encoding="utf-8")
    return {"name": char, "version": version}


async def render(name: str | None = None) -> str:
    card = await load(name)
    body = card["body"].strip()
    rel = config.cfg()["owner"].get("label") or "主人"
    return f"{body}\n\n（对你说话最多的人是{rel}。）"


# ---------------------------------------------------------------- 称呼表
# 谁该被怎么叫，是一张很小的表，但少了它她会叫错人。
# 存在 kv 里（addr.<qq>），不占记忆卡的额度。


async def set_address(qq: str | int, label: str) -> str:
    key = str(qq).strip()
    label = (label or "").strip()
    if not key or not label:
        raise ValueError("qq 和叫法都不能空")
    await db.kv_set(f"addr.{key}", label)
    return label


async def get_address(qq: str | int) -> str:
    return (await db.kv_get(f"addr.{qq}")) or ""


async def remove_address(qq: str | int) -> None:
    await db.kv_del(f"addr.{qq}")


async def addresses() -> dict[str, str]:
    conn = await db.connect()
    cur = await conn.execute("SELECT k, v FROM kv WHERE k LIKE 'addr.%' ORDER BY k")
    rows = await cur.fetchall()
    out: dict[str, str] = {}
    for r in rows:
        raw = r["v"]
        try:  # kv 里存的是 JSON，直接读表会带引号
            value = json.loads(raw)
        except Exception:
            value = raw
        out[str(r["k"])[5:]] = str(value)
    return out


async def render_addresses() -> str:
    table = await addresses()
    if not table:
        return ""
    # 提示词里不放 QQ 号（2026-10-31 用户要求：识别用号，叫用备注）。
    # 主人那一条给出"你叫他什么"；别人用平台昵称，不需要号码。
    owner = str(config.cfg()["owner"].get("qq") or "")
    owner_label = str(table.get(owner) or "").strip()
    lines = []
    if owner_label:
        lines.append(f"- 你的主人，你叫他「{owner_label}」")
    if not lines:
        return ""
    return "## 称呼\n" + "\n".join(lines)
