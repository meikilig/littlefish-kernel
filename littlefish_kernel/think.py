"""她的思路 —— 每次模型调用返回的 reasoning_content，留一份给人看。

为什么要单独存：QQ 上她只发得出去"话"，发不出去"怎么想的"。
surfcore 那会儿思维链是直接显示在 DSH 对话里的，因为她就是跑在 DSH 里；
现在她跑在 AstrBot 里，中间没有那层可见的壳，所以这里自己记一份。

规矩：
- 只追加（append），不重写；超过 8MB 就轮转成 think.jsonl.1（只留一代）。
- 一条记录：{at, umo, model, chars, text, reply}
- 单条 text 最多留 20000 字（够看一轮怎么想的了，再长也没人读）。
- 这个文件不给模型看，只给人看；任何时候删掉都不影响她。
- 模型是用英文想的（deepseek-flash 就这样），所以这里顺手记一份中文：
  不额外花钱（不调模型翻译），只把常见的推理动词/连接词换掉，能看懂"它在盘算什么"就够了。
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import config
from .log import log

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 20000

# 英文思路 → 中文提示。不追求通顺，只要一眼能看出"她在盘算什么"。
# 只替换整词，避免把 BV1z3b 这种标识符切坏。
_WORDS = {
    " He ": " 他", " She ": " 她", " The user ": " 主人",
    "He asks": "主人问", "She asks": "她问", "asks": "问", "asked": "问了",
    "The user asks": "主人问", "user asks": "主人问", "User asks": "主人问",
    "Keep to": "保持", "short": "简短", "two sentences": "两句",
    "I need to": "我需要", "I should": "我应该", "I will": "我会",
    "I can": "我可以", "I think": "我觉得", "I want": "我想",
    "maybe": "也许", "perhaps": "或许", "but": "不过", "so": "所以",
    "from ": "来自 ", "about ": "关于 ", "the ": "", "and ": "然后 ",
    "remember": "记得", "memory": "记忆", "reply": "回复", "answer": "回答",
    "careful": "小心", "avoid": "避免", "allowed": "允许", "should not": "不该",
}


def zh_hint(text: str) -> str:
    """给一段英文思路配一句中文提示（纯字符串处理，不调模型）。"""
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    head = t[:120].replace("\n", " ")
    hits = []
    for en, zh in (("asks", "主人在问"), ("two sentences", "要简短"),
                   ("keep to", "要求控制长度"), ("remember", "在回忆"),
                   ("memory", "翻了记忆"), ("bv1", "在看那条视频"),
                   ("comment", "考虑评论"), ("don't", "在克制")):
        if en in low:
            hits.append(zh)
    if not hits:
        hits.append("在组织回答")
    out = head
    for en, zh in _WORDS.items():
        out = out.replace(en, zh)
    return f"[中文提示] {' / '.join(hits)}；原文开头：{out[:110]}（英文原样见下）"


def _path():
    d = config.ROOT / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / "think.jsonl"


def _rotate(p) -> None:
    try:
        if p.exists() and p.stat().st_size > MAX_BYTES:
            old = p.with_name("think.jsonl.1")
            if old.exists():
                old.unlink()
            p.rename(old)
    except Exception as exc:
        log.warning(f"[think] rotate failed: {exc}")


def record(*, text: str, umo: str = "", model: str = "", reply: str = "",
           ask: str = "") -> None:
    """记一条思路。text 为空就什么都不做。"""
    text = (text or "").strip()
    if not text:
        return
    rec = {
        "at": round(time.time(), 1),
        "umo": umo or "",
        "model": model or "",
        "chars": len(text),
        "text": text[-MAX_TEXT:],
        "zh": zh_hint(text),
        "reply": (reply or "")[:600],
        "ask": (ask or "")[:600],
    }
    p = _path()
    _rotate(p)
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning(f"[think] write failed: {exc}")


def recent(n: int = 30, umo: str | None = None) -> list[dict[str, Any]]:
    """最近 n 条（从文件尾部读 1MB，够 30 条了）。"""
    p = _path()
    if not p.exists():
        return []
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, size - 1024 * 1024))
            data = f.read().decode("utf-8", "replace")
    except Exception as exc:
        log.warning(f"[think] read failed: {exc}")
        return []

    out: list[dict[str, Any]] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if umo and rec.get("umo") != umo:
            continue
        out.append(rec)
    return out[-max(1, n):]


def stats() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"exists": False, "records": 0, "bytes": 0}
    size = p.stat().st_size
    n = 0
    try:
        with open(p, "rb") as f:
            f.seek(max(0, size - 1024 * 1024))
            n = sum(1 for line in f.read().decode("utf-8", "replace").splitlines() if line.strip())
    except Exception:
        pass
    return {"exists": True, "records": n, "bytes": size, "path": str(p)}


def clear() -> int:
    """清空（保留文件）。返回删掉的字节数。"""
    p = _path()
    if not p.exists():
        return 0
    size = p.stat().st_size
    try:
        p.write_text("", encoding="utf-8")
    except Exception as exc:
        log.warning(f"[think] clear failed: {exc}")
        return 0
    return size
