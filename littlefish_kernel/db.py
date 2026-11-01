"""SQLite 底座：一个库，一次迁移，append-only 归档。

设计约束（来自旧系统的教训）：
1. 状态只追加或小事务更新，**绝不整文件重写**。旧系统每次维护重写 308KB JSON，
   断电即坏；这里每条记忆是一次 INSERT/UPDATE，WAL 保证崩溃后一致。
2. 每张表都有明确唯一写者，避免多处同时写。
3. schema 版本写在 PRAGMA user_version，升级只加不删。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

import aiosqlite

from . import config

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_card (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory (
    id              TEXT PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'qq',
    scope           TEXT NOT NULL DEFAULT 'qq',
    kind            TEXT NOT NULL DEFAULT 'chat',
    title           TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',
    importance      INTEGER NOT NULL DEFAULT 10,
    reasons         TEXT NOT NULL DEFAULT '[]',
    emotional       INTEGER NOT NULL DEFAULT 0,
    pinned          INTEGER NOT NULL DEFAULT 0,
    anchor          INTEGER NOT NULL DEFAULT 0,
    clarity         TEXT NOT NULL DEFAULT 'vivid',
    created_at      INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    decay_steps     INTEGER NOT NULL DEFAULT 0,
    recall_count    INTEGER NOT NULL DEFAULT 0,
    speaker         TEXT NOT NULL DEFAULT '',
    group_id        TEXT NOT NULL DEFAULT '',
    conversation_key TEXT NOT NULL DEFAULT '',
    is_self         INTEGER NOT NULL DEFAULT 0,
    sensitive       INTEGER NOT NULL DEFAULT 0,
    is_first        INTEGER NOT NULL DEFAULT 0,
    first_tier      TEXT NOT NULL DEFAULT '',
    merged_from     TEXT NOT NULL DEFAULT '[]',
    archived        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_scope   ON memory(scope, archived, clarity);
CREATE INDEX IF NOT EXISTS idx_memory_recent  ON memory(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_conv    ON memory(conversation_key);
CREATE INDEX IF NOT EXISTS idx_memory_kind    ON memory(kind, created_at DESC);

-- 中文二元组倒排索引（不用向量：我们要"她真说过这句"，不是"意思相近"）
CREATE TABLE IF NOT EXISTS memory_token (
    token   TEXT NOT NULL,
    mem_id  TEXT NOT NULL,
    PRIMARY KEY (token, mem_id)
);
CREATE INDEX IF NOT EXISTS idx_token ON memory_token(token);

-- 自我卡：她对自己的叙述，只在新会话第一次唤醒读一次
CREATE TABLE IF NOT EXISTS self_note (
    id           TEXT PRIMARY KEY,
    text         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0
);

-- 出站队列：所有"要说话"的动作先进这里，发送成功才落 sent
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    umo         TEXT NOT NULL DEFAULT '',
    chain       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL,
    sent_at     INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
);

-- 用量账本：三个 token 字段分开记账（缓存价和普通价不是一个价）
CREATE TABLE IF NOT EXISTS usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    input_other  INTEGER NOT NULL DEFAULT 0,
    input_cached INTEGER NOT NULL DEFAULT 0,
    output       INTEGER NOT NULL DEFAULT 0,
    cost         REAL    NOT NULL DEFAULT 0,
    scope        TEXT NOT NULL DEFAULT '',
    ref          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts DESC);

-- 事件总线落盘：进程内 bus 之外再留一份可回放的事件流
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    topic   TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, ts DESC);

-- 跨插件小状态（每个 key 只有一个写者，value 是 JSON）
CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL DEFAULT 'null',
    updated_at INTEGER NOT NULL DEFAULT 0
);
"""

_conn: aiosqlite.Connection | None = None
_conn_path: str = ""


async def connect() -> aiosqlite.Connection:
    """进程内单连接。切换角色时自动换库（路径变了就重开）。"""
    global _conn, _conn_path
    want = str(config.db_path())
    if _conn is not None and _conn_path == want:
        return _conn
    if _conn is not None:
        await _conn.close()
        _conn = None
    config.ensure_dirs()
    conn = await aiosqlite.connect(want)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.executescript(_SCHEMA)
    await _migrate(conn)
    await conn.commit()
    _conn = conn
    _conn_path = want
    return conn


async def _migrate(conn: aiosqlite.Connection) -> None:
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    version = int(row[0]) if row else 0
    if version < 2:
        # v2：用量账本多一列"这笔钱算在哪个功能上"（按 L1 拆账要用）
        cols = [r[1] for r in await (await conn.execute("PRAGMA table_info(usage)")).fetchall()]
        if "feature" not in cols:
            await conn.execute(
                "ALTER TABLE usage ADD COLUMN feature TEXT NOT NULL DEFAULT ''"
            )
    if version < SCHEMA_VERSION:
        await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


async def close() -> None:
    global _conn, _conn_path
    if _conn is not None:
        await _conn.close()
        _conn = None
    _conn_path = ""


# ------------------------------------------------------------ query helpers


async def fetchone(sql: str, params: tuple = ()) -> dict | None:
    conn = await connect()
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None


async def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    conn = await connect()
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def execute(sql: str, params: tuple = (), *, commit: bool = True) -> int:
    conn = await connect()
    cur = await conn.execute(sql, params)
    if commit:
        await conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- kv helpers


async def kv_get(key: str, default: Any = None) -> Any:
    conn = await connect()
    cur = await conn.execute("SELECT v FROM kv WHERE k = ?", (key,))
    row = await cur.fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return default


async def kv_set(key: str, value: Any) -> None:
    conn = await connect()
    await conn.execute(
        "INSERT INTO kv(k, v, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), int(time.time())),
    )
    await conn.commit()


async def kv_del(key: str) -> None:
    conn = await connect()
    await conn.execute("DELETE FROM kv WHERE k = ?", (key,))
    await conn.commit()


# ------------------------------------------------------------- event helpers


async def log_event(topic: str, payload: dict[str, Any] | None = None) -> None:
    conn = await connect()
    await conn.execute(
        "INSERT INTO events(ts, topic, payload) VALUES(?, ?, ?)",
        (int(time.time()), topic, json.dumps(payload or {}, ensure_ascii=False)),
    )
    await conn.commit()


async def recent_events(topic: str | None = None, limit: int = 50) -> list[dict]:
    conn = await connect()
    if topic:
        cur = await conn.execute(
            "SELECT * FROM events WHERE topic = ? ORDER BY id DESC LIMIT ?",
            (topic, limit),
        )
    else:
        cur = await conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------- append-only log


def append_jsonl(folder: str, prefix: str, record: dict[str, Any]) -> None:
    """按月分片、append-only。断电最多丢最后一行，不会毁掉整份历史。"""
    d = config.archive_dir() / folder
    d.mkdir(parents=True, exist_ok=True)
    month = time.strftime("%Y%m")
    path = d / f"{prefix}-{month}.jsonl"
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def iter_jsonl(folder: str, prefix: str) -> Iterable[dict[str, Any]]:
    d = config.archive_dir() / folder
    if not d.exists():
        return
    for path in sorted(d.glob(f"{prefix}-*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
