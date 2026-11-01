"""管理台（角色控制台）。

设计上的三条硬规矩：

1. **只听回环**：bind 127.0.0.1，别的机器连不上，非本机请求一律 403。
2. **秘密路径当钥匙**：地址里带一段随机 token（`data/console.token`），
   不靠 URL 猜、不靠 header，浏览器直接打开就能用；别的网页 CSRF 也打不中，
   因为它不知道这段路径。
3. **改的是真文件**：面板写的就是 `data/config/<plugin>_config.json`，
   写完后让 caps 立刻失效缓存，人不用重启。

它是「宿主能力」，不是「她的能力」——所以放在内核里，不挂任何一级插件。
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import (bookshelf, compat, abilities, budget, caps, config, db, desk, memory, nightly,
               ops, persona, selfcard, think)
from .log import log

PORT = 6197
HOST = "127.0.0.1"

_APP = None
_RUNNER = None
_TOKEN = ""
_STARTED_AT = 0.0

# 一级的顺序与中文名（面板只认这一张表）
L1: list[tuple[str, str, str, str]] = [
    ("astrbot_littlefish_l1_qq", "L1-1", "QQ 通讯", "通道层：引用、拆条、冷却、拉黑、黑话、工具面"),
    ("astrbot_littlefish_l1_bili", "L1-2", "B站", "许可制出门：她说想去 → 你批 → 她看 → 回来讲"),
    ("astrbot_littlefish_l1_language", "L1-3", "语言系统", "说话风格、叫得醒她、主动搭话、聊完怎么收"),
    ("astrbot_littlefish_l1_budget", "L1-4", "用量明细", "按 token 三档计价，缓存命中率掉下来账单就涨"),
    ("astrbot_littlefish_l1_voice", "L1-5", "语音", "空壳：引擎接上之前一直安静"),
    ("astrbot_littlefish_l1_api", "L1-6", "功能接口", "本地接口与回调，给别的程序用"),
    ("astrbot_littlefish_l1_ops", "L1-7", "运维诊断", "体检、备份、日志；插件管理也在这"),
    ("astrbot_littlefish_l1_abilities", "L1-8", "能力", "她会的新东西：听歌、看书，以后可能是一整个虚拟社会"),
]

# 分组的中文名（不认识的 key 直接用 key 本身）
GROUP_TITLES = {
    "wake": "谁叫得醒她",
    "style": "说话风格",
    "outreach": "主动搭话",
    "closing": "聊完怎么收尾",
    "address": "称呼",
    "forbid": "别主动提的",
    "group": "群聊",
    "limit": "频率限制",
    "tools": "她手上的工具",
    "protect": "鱼群保护",
    "actions": "她能做什么",
    "limits": "一次出门的上限",
    "http": "本地接口",
    "webhook": "回调",
    "platforms": "别的平台",
    "local": "本地引擎",
    "cloud": "云端引擎",
    "quota": "额度",
    "watch": "观看",
}


def _config_dir() -> Path:
    return Path(caps.config_path("x")).parent


def _plugin_config_path(plugin: str) -> Path:
    return _config_dir() / f"{plugin}_config.json"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _token_file() -> Path:
    return config.ROOT / "data" / "console.token"


def _ensure_token() -> str:
    global _TOKEN
    if _TOKEN and len(_TOKEN) >= 16:
        return _TOKEN
    p = _token_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        _TOKEN = p.read_text(encoding="utf-8").strip()
    # 太短的令牌等于没有：24 个十六进制字符 = 96 bit，穷举不现实
    if len(_TOKEN) < 16:
        _TOKEN = secrets.token_hex(12)
        p.write_text(_TOKEN, encoding="utf-8")
        try:
            import os
            os.chmod(p, 0o600)
        except Exception:
            pass
        log.info("[console] 令牌太短或不存在，已重新生成")
    return _TOKEN


def _walk(node: dict, prefix: str = "") -> tuple[list[dict], list[dict]]:
    """把 schema 拆成「分组」和「开关」两摞。"""
    groups: list[dict] = []
    items: list[dict] = []
    for key, spec in (node or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{key}"
        is_group = (
            isinstance(spec.get("items"), dict)
            and spec.get("type") in (None, "object", "dict")
            and spec["items"]
        )
        if is_group:
            sub_groups, sub_items = _walk(spec["items"], path + ".")
            if sub_items:
                groups.append({
                    "key": path,
                    "title": GROUP_TITLES.get(key, spec.get("description") or key),
                    "items": sub_items,
                })
            groups.extend(sub_groups)
            continue
        t = str(spec.get("type") or "string")
        floaty = t in ("bool", "int", "float", "string", "text", "list", "dict", "object")
        if not floaty:
            continue
        items.append({
            "path": path,
            "label": spec.get("description") or key,
            "hint": spec.get("hint") or "",
            "type": t,
            "default": spec.get("default"),
        })
    return groups, items


def _get_path(data: dict, path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def build_menu() -> list[dict]:
    out: list[dict] = []
    for plugin, code, title, desc in L1:
        schema_path = config.astrbot_dir() / "data" / "plugins" / plugin / "_conf_schema.json"
        schema = _read_json(schema_path)
        live = _read_json(_plugin_config_path(plugin))
        groups, loose = _walk(schema)
        if loose:
            groups.insert(0, {"key": "", "title": "总开关", "items": loose})
        for g in groups:
            for it in g["items"]:
                v = _get_path(live, it["path"], it["default"])
                it["value"] = v
        out.append({
            "plugin": plugin,
            "code": code,
            "title": title,
            "desc": desc,
            "enabled": bool(_get_path(live, "enabled", (schema.get("enabled") or {}).get("default", True))),
            "groups": [g for g in groups if g["items"]],
        })
    return out


async def build_summary() -> dict:
    st = await memory.stats()
    today = await budget.today()
    try:
        bili = json.loads(await db.kv_get("bili.state") or "{}")
    except Exception:
        bili = {}
    notes = await selfcard.list_all()
    try:
        night = await nightly.status()
    except Exception as exc:
        night = {"ok": False, "error": str(exc)}
    return {
        "character": config.cfg().get("character"),
        "memory": st,
        "today": today,
        "bili": bili,
        "self_notes": len(notes or []),
        "nightly": night,
        "uptime_sec": int(time.time() - _STARTED_AT) if _STARTED_AT else 0,
        "token": _TOKEN,
    }


# --------------------------------------------------------------------------- web


def _otp_file() -> Path:
    return config.ROOT / "data" / "console.otp.json"


def _otps() -> list[dict]:
    """读临时地址清单；顺手把过期的删掉（过期就是过期，不留着）。"""
    try:
        data = json.loads(_otp_file().read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
    except Exception:
        return []
    now = time.time()
    live = [e for e in data if isinstance(e, dict) and float(e.get("exp") or 0) > now]
    if len(live) != len(data):
        try:
            _otp_file().write_text(json.dumps(live, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
    return live


def mint_otp(minutes: int = 30, scope: str = "read", note: str = "") -> dict:
    """发一个临时地址。默认只读、30 分钟。"""
    if scope not in ("read", "full"):
        raise ValueError("scope 只能是 read 或 full")
    entries = _otps()
    ent = {
        "id": secrets.token_hex(4),
        "token": secrets.token_urlsafe(18).replace("-", "x").replace("_", "y"),
        "exp": time.time() + max(1, int(minutes)) * 60,
        "scope": scope,
        "note": note[:80],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(ent)
    _otp_file().write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        _TOKEN  # 只为确保长期令牌已生成，避免两者同时初始化时打架
    except NameError:
        pass
    return ent


def revoke_otps(keep_id: str | None = None) -> int:
    """撤销临时地址。给了 keep_id 就只留那一个。"""
    entries = _otps()
    if keep_id is None:
        keep: list[dict] = []
    else:
        keep = [e for e in entries if e.get("id") == keep_id]
    _otp_file().write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(entries) - len(keep)


def otp_url(ent: dict) -> str:
    return f"http://127.0.0.1:{PORT}/c/{ent['token']}/"


def _scope_of(request) -> str:
    """路径里的令牌属于谁：长期令牌 = full，临时地址 = 它自己声明的 scope。

    full  = 看 + 改（长期令牌，只在 data\\console.token 里）
    read  = 只能看（给外部审计 / 红队用的一次性地址）
    """
    got = request.match_info.get("token", "")
    if not got:
        raise RuntimeError("令牌不对")
    if hmac.compare_digest(got, _ensure_token()):
        return "full"
    for ent in _otps():
        if hmac.compare_digest(got, str(ent.get("token", ""))):
            request["lf_otp"] = ent
            try:
                with open(config.ROOT / "data" / "tmp" / "otp-access.log", "a", encoding="utf-8") as fh:
                    fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {ent.get('id')} "
                             f"{request.method} {request.path}\n")
            except Exception:
                pass
            return str(ent.get("scope") or "read")
    raise RuntimeError("令牌不对")


def _guard(request, write: bool = False) -> str:
    """只允许本机 + 有效令牌。write=True 时要求 full 权限。出错抛 RuntimeError → 403。"""
    peer = (request.remote or "")
    if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        raise RuntimeError("只允许本机访问")
    scope = _scope_of(request)
    if write and scope != "full":
        raise RuntimeError("这个地址是只读的")
    return scope


@web.middleware
async def _guard_middleware(request, handler):
    """先鉴权再进 handler：非 GET 一律要求 full 权限，只读地址连试都不许试。"""
    try:
        # 不在 /c/ 下面的路径交给路由去 404 —— 别把"没有这个页面"也说成"令牌不对"
        if request.path.startswith("/c/"):
            _guard(request, write=(request.method != "GET"))
        return await handler(request)
    except RuntimeError as exc:
        if str(exc) in ("只允许本机访问", "令牌不对", "这个地址是只读的"):
            return web.Response(status=403, text="403")
        raise


async def _guard_write(request) -> bool:
    """写接口统一入口：只读令牌来的请求一律 403。"""
    try:
        _guard(request, write=True)
        return True
    except Exception:
        return False


async def _index(request):
    try:
        _guard(request)
    except Exception:
        return web.Response(status=403, text="403")
    html = (Path(__file__).parent / "web" / "console.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def _api_menu(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "data": build_menu()})


async def _api_compat(request):
    """宿主契约体检结果（升级 AstrBot 前后各看一次）。"""
    # 注意：_guard 成功时返回的是 scope 字符串（"full"），失败才抛异常。
    # 早先写成 `err = _guard(...) ; if err: return err` → 成功时把字符串当响应返回，
    # aiohttp 会炸 AttributeError: 'str' object has no attribute 'prepare'。
    try:
        _guard(request)
    except RuntimeError:
        return web.Response(status=403, text="403")
    return web.json_response({"ok": True, "data": compat.cached(),
                              "report": compat.report(),
                              "contract": compat.CONTRACT})


def _qq_of(umo: str) -> str:
    """从 umo 里抠出 QQ 号（snowluma:FriendMessage:370611193 → 370611193）。"""
    tail = str(umo or "").split(":")[-1].strip()
    return tail if tail.isdigit() else ""


async def _who(umo: str = "", speaker: str = "") -> str:
    """这句话是谁说的：主人 / 备注里的名字 / 朋友（群里叫群友）。

    2026-11-01 修：以前不管是谁一律标"主人"，朋友的话也被写成主人说的。
    """
    owner = str(config.cfg()["owner"].get("qq") or "")
    qq = str(speaker or "").strip() or _qq_of(umo)
    if qq and owner and qq == owner:
        return "主人"
    try:
        table = await persona.addresses()
    except Exception:
        table = {}
    name = str(table.get(qq) or "").strip() if qq else ""
    if name:
        return name
    return "群友" if ":GroupMessage:" in str(umo or "") else "朋友"


async def _last_lines(umo: str, k: int = 4) -> list[str]:
    """这个会话最近 k 条记忆（给她这句思路一点上下文）。"""
    try:
        import sqlite3
        sql = ("SELECT is_self, speaker, text FROM memory WHERE archived=0 AND conversation_key=? "
               "ORDER BY created_at DESC LIMIT ?")
        with sqlite3.connect(str(config.db_path())) as cn:
            rows = cn.execute(sql, (umo, k)).fetchall()
        out = []
        for is_self, speaker, text in reversed(rows):
            t = " ".join(str(text or "").split())
            if t:
                who = "她" if is_self else await _who(umo, speaker)
                out.append(f"{who}：" + t[:160])
        return out
    except Exception:
        return []


async def build_think(n: int = 30, umo: str | None = None) -> dict[str, Any]:
    """她最近几次"怎么想的"。QQ 上发不出去，只能在这儿看。"""
    rows = think.recent(n=n, umo=umo)
    out = []
    ctx_cache: dict[str, list[str]] = {}
    who_cache: dict[str, str] = {}
    for r in rows:
        key = r.get("umo", "") or ""
        if key and key not in ctx_cache:
            ctx_cache[key] = await _last_lines(key, 4)
        if key and key not in who_cache:
            who_cache[key] = await _who(key)
        out.append({
            "at": r.get("at", 0),
            "time": time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("at", 0))),
            "umo": r.get("umo", ""),
            "where": (r.get("umo", "") or "").split(":")[-1],
            "who": who_cache.get(key, "") or "对方",
            "model": r.get("model", ""),
            "chars": r.get("chars", 0),
            "text": r.get("text", ""),
            "zh": r.get("zh", "") or "",
            "ask": r.get("ask", "") or "",
            "before": ctx_cache.get(r.get("umo", "") or "", []),
            "reply": r.get("reply", ""),
        })
    return {"rows": out, "stats": think.stats()}


async def _api_think(request):
    try:
        _guard(request)
    except RuntimeError:
        return web.Response(status=403, text="403")
    try:
        n = int(request.query.get("n", "30"))
    except Exception:
        n = 30
    umo = request.query.get("umo") or None
    return web.json_response({"ok": True, "data": await build_think(max(1, min(n, 200)), umo)})


async def _api_think_clear(request):
    try:
        _guard(request, write=True)
    except RuntimeError:
        return web.Response(status=403, text="403")
    n = think.clear()
    return web.json_response({"ok": True, "freed": n})


async def _api_summary(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "data": await build_summary()})


async def _api_set(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    body = await request.json()
    plugin = str(body.get("plugin") or "")
    path = str(body.get("path") or "")
    value = body.get("value")
    if plugin not in [x[0] for x in L1] or not path:
        return web.json_response({"ok": False, "error": "bad target"}, status=400)

    p = _plugin_config_path(plugin)
    data = _read_json(p)
    if not data:
        data = {}
    _set_path(data, path, value)
    data.setdefault("enabled", True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    caps.invalidate()
    effective = caps.on(plugin, path, value)
    log.info(f"[console] {plugin}:{path} -> {value!r}")
    return web.json_response({"ok": True, "effective": effective})


async def _api_act(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    body = await request.json()
    name = str(body.get("name") or "")
    if name == "health":
        return web.json_response({"ok": True, "data": await ops.health()})
    if name == "backup":
        return web.json_response({"ok": True, "data": await ops.backup()})
    if name == "decay":
        return web.json_response({"ok": True, "data": await memory.decay_once()})
    if name == "nightly":
        day = str(body.get("day") or "")
        return web.json_response({"ok": True, "data": await nightly.run(day=day, force=True)})
    if name == "nightly_status":
        return web.json_response({"ok": True, "data": await nightly.status()})
    return web.json_response({"ok": False, "error": "unknown action"}, status=400)


# ---------------------------------------------------------------- 本体 / 记忆


async def build_persona() -> dict:
    card = await persona.load()
    body = str((card or {}).get("body") or "")
    return {
        "character": config.cfg().get("character"),
        "name": (card or {}).get("name") or "",
        "version": (card or {}).get("version") or 0,
        "updated_at": (card or {}).get("updated_at") or 0,
        "body": body,
        "chars": len(body),
        "addresses": await persona.addresses(),
        "notes": await selfcard.list_all(),
    }


async def build_memories(q: str = "", clarity: str = "", limit: int = 60, offset: int = 0) -> dict:
    where = ["archived = 0"]
    args: list[Any] = []
    if q:
        where.append("(text LIKE ? OR title LIKE ? OR tags LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if clarity:
        where.append("clarity = ?")
        args.append(clarity)
    sql = (
        "SELECT id, platform, kind, title, text, importance, clarity, created_at, "
        "recall_count, pinned, is_self, sensitive, is_first FROM memory "
        "WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    rows = await db.fetchall(sql, tuple(args + [limit, offset]))
    total = await db.fetchone(
        "SELECT COUNT(*) AS n FROM memory WHERE " + " AND ".join(where), tuple(args)
    )
    return {
        "rows": rows or [],
        "total": (total or {}).get("n", 0),
        "stats": await memory.stats(),
        "limit": limit,
        "offset": offset,
    }


async def build_abilities() -> dict:
    """能力页：她会什么、每个能不能关、装了没生效的、货架上还有什么。"""
    h = await abilities.health()
    return {
        "master": h["master"],
        "installed": h["installed"],
        "packs": h["packs"],
        "pending_restart": h["pending_restart"],
        "issues": h["issues"],
        "packs_dir": str(abilities.packs_dir()),
        "allow_install": bool(caps.on("l1_abilities", "allow_install", False)),
        "desk": _desk_view(),
    }


def _desk_view() -> dict:
    """写作页：桌上有什么、谁在等提额。读不出就当没有，别让能力页整个 500。"""
    try:
        books = desk.list_books()
        pending = desk.pending()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[console] 读桌面失败：{exc}")
        return {"books": [], "pending": [], "dir": "", "error": str(exc)[:120]}
    return {"books": books, "pending": pending, "dir": str(desk.desk_dir()),
            "max_chars": desk.book_limit_default(),
            "full_rewrite_max": desk.FULL_REWRITE_MAX}


async def build_inject() -> dict:
    cfg = config.cfg()
    mem = cfg.get("memory") or {}
    budget = mem.get("inject_max_chars") or {}
    # 预览给的是"她自己这一侧能看到的全部"：不传 ctx，不做场景过滤。
    # 真实群里会按 privacy.isolation 再筛一遍，页面上写明了这点。
    out = {}
    for platform in ("qq", "bili"):
        text = await memory.render(platform)
        lim = int(budget.get(platform) or 0)
        out[platform] = {
            "text": text,
            "chars": len(text or ""),
            "budget": lim,
            "over": bool(lim and len(text or "") > lim),
        }
    out["persona"] = await persona.render()
    out["persona_chars"] = len(out["persona"] or "")
    return out


async def _api_persona(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    if request.method == "GET":
        return web.json_response({"ok": True, "data": await build_persona()})
    body = await request.json()
    text = str(body.get("body") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "人设不能为空"}, status=400)
    await persona.save(text)
    log.info(f"[console] persona saved, {len(text)} chars")
    return web.json_response({"ok": True, "data": await build_persona()})


async def _api_memories(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    q = str(request.query.get("q") or "").strip()
    clarity = str(request.query.get("clarity") or "").strip()
    try:
        limit = max(1, min(200, int(request.query.get("limit") or 60)))
        offset = max(0, int(request.query.get("offset") or 0))
    except ValueError:
        limit, offset = 60, 0
    return web.json_response({"ok": True, "data": await build_memories(q, clarity, limit, offset)})


async def _api_memory_act(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    body = await request.json()
    action = str(body.get("action") or "")
    mid = str(body.get("id") or "")
    if action == "pin":
        await memory.pin(mid, bool(body.get("pinned", True)))
    elif action == "forget":
        await memory.forget(mid)
    elif action == "add":
        text = str(body.get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "空的记不住"}, status=400)
        await memory.add(
            text,
            platform=str(body.get("platform") or "qq"),
            kind="manual",
            title=str(body.get("title") or ""),
            pinned=bool(body.get("pinned")),
            importance=body.get("importance"),
            reasons=["手工添加"],
        )
    else:
        return web.json_response({"ok": False, "error": "unknown action"}, status=400)
    log.info(f"[console] memory {action} {mid or ''}")
    return web.json_response({"ok": True, "data": await build_memories(
        str(body.get("q") or ""), str(body.get("clarity") or ""))})


async def _api_inject(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "data": await build_inject()})


async def _api_note(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    if request.method == "POST":
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "空的"}, status=400)
        await selfcard.add(text)
    return web.json_response({"ok": True, "data": await selfcard.list_all()})


async def _api_addr(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    if request.method == "POST":
        body = await request.json()
        qq = str(body.get("qq") or "").strip()
        label = str(body.get("label") or "").strip()
        if not qq:
            return web.json_response({"ok": False, "error": "缺 QQ 号"}, status=400)
        if label:
            await persona.set_address(qq, label)
        else:
            await persona.remove_address(qq)
    return web.json_response({"ok": True, "data": await persona.addresses()})


async def _api_abilities(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "data": await build_abilities()})


async def _api_desk(request):
    """桌面：不带 book 就列单子，带 book 就把那本读出来。"""
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    book = str(request.query.get("book") or "").strip()
    if not book:
        return web.json_response({"ok": True, "data": _desk_view()})
    res = desk.read_book(book)
    if not res.get("ok"):
        return web.json_response({"ok": False, "error": res.get("error")}, status=404)
    return web.json_response({"ok": True, "data": res})


async def build_capacity() -> dict:
    cap = await memory.capacity()
    plan = await memory.trim_plan(0.9)
    cands = await memory.evict_candidates(limit=12)
    return {"capacity": cap, "plan": plan, "candidates": cands}


CODES_PLUGIN = "astrbot_littlefish_l1_qq"


def _switch_choices() -> list[dict]:
    """把菜单里所有 bool 开关摊平，给"暗号绑到哪个开关"做下拉用。"""
    out: list[dict] = []
    for l1 in build_menu():
        for group in l1.get("groups") or []:
            for item in group.get("items") or []:
                if item.get("type") != "bool":
                    continue
                out.append(
                    {
                        "plugin": l1["plugin"],
                        "path": item["path"],
                        "label": f"{l1['code']} · {group.get('title') or ''} · {item.get('title') or item['path']}".replace(" ·  · ", " · "),
                    }
                )
    return out


async def build_codes() -> dict:
    raw = caps.raw(CODES_PLUGIN, "codes.list", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    rows = []
    for code, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        rows.append({"code": str(code), **spec})
    rows.sort(key=lambda r: r["code"])
    return {
        "enabled": caps.on(CODES_PLUGIN, "codes.enabled", False),
        "in_group": caps.on(CODES_PLUGIN, "codes.in_group", False),
        "reply_group": caps.on(CODES_PLUGIN, "codes.reply_group", False),
        "items": rows,
        "switches": _switch_choices(),
        "owner": str((config.cfg().get("owner") or {}).get("qq") or ""),
    }


async def _api_codes(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    if request.method == "GET":
        return web.json_response({"ok": True, "data": await build_codes()})

    body = await request.json()
    action = str(body.get("action") or "")
    raw = caps.raw(CODES_PLUGIN, "codes.list", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw = dict(raw)

    if action == "flags":
        caps.write(CODES_PLUGIN, "codes.enabled", bool(body.get("enabled")))
        caps.write(CODES_PLUGIN, "codes.in_group", bool(body.get("in_group")))
        caps.write(CODES_PLUGIN, "codes.reply_group", bool(body.get("reply_group")))
        return web.json_response({"ok": True, "data": await build_codes()})

    if action == "del":
        code = str(body.get("code") or "")
        raw.pop(code, None)
        caps.write(CODES_PLUGIN, "codes.list", raw)
        return web.json_response({"ok": True, "data": await build_codes()})

    if action == "add":
        code = str(body.get("code") or "").strip()
        if not code:
            return web.json_response({"ok": False, "error": "暗号不能空"}, status=400)
        if len(code) > 32:
            return web.json_response({"ok": False, "error": "暗号太长了（最多 32 位）"}, status=400)
        if code in raw:
            return web.json_response({"ok": False, "error": "这个暗号已经有了"}, status=400)

        mode = str(body.get("mode") or "on")
        note = str(body.get("note") or "").strip()
        plugin = str(body.get("plugin") or "")
        path = str(body.get("path") or "")

        if mode in ("on", "off", "toggle", "minutes"):
            if not plugin or not path:
                return web.json_response({"ok": False, "error": "要选一个开关"}, status=400)
            if mode == "on":
                spec = {"kind": "set", "plugin": plugin, "path": path, "value": True}
            elif mode == "off":
                spec = {"kind": "set", "plugin": plugin, "path": path, "value": False}
            elif mode == "toggle":
                spec = {"kind": "toggle", "plugin": plugin, "path": path}
            else:
                minutes = float(body.get("minutes") or 30)
                spec = {"kind": "on_for", "plugin": plugin, "path": path, "minutes": minutes}
        elif mode == "emit":
            topic = str(body.get("topic") or "").strip()
            if not topic:
                return web.json_response({"ok": False, "error": "要填事件名"}, status=400)
            spec = {"kind": "emit", "topic": topic}
        else:
            return web.json_response({"ok": False, "error": f"不认识的模式：{mode}"}, status=400)

        if note:
            spec["note"] = note
        raw[code] = spec
        if not caps.write(CODES_PLUGIN, "codes.list", raw):
            return web.json_response({"ok": False, "error": "写不进去"}, status=500)
        log.info(f"[console] 暗号 {code} 已加：{spec}")
        return web.json_response({"ok": True, "data": await build_codes()})

    return web.json_response({"ok": False, "error": f"不认识的 action：{action}"}, status=400)


async def _api_capacity(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "data": await build_capacity()})


async def _api_memory_bulk(request):
    """淘汰 / 合并。两个都要人点头，所以只从面板走，不给模型工具。"""
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    body = await request.json()
    action = str(body.get("action") or "")
    ids = [str(x) for x in (body.get("ids") or []) if str(x).strip()]
    if not ids:
        return web.json_response({"ok": False, "error": "没选中任何一条"}, status=400)
    if action == "evict":
        res = await memory.evict(ids, by="owner")
    elif action == "merge":
        res = await memory.merge(ids, title=str(body.get("title") or ""))
        if not res.get("ok"):
            return web.json_response({"ok": False, "error": res.get("why") or "合并失败"}, status=400)
    else:
        return web.json_response({"ok": False, "error": f"不认识的动作：{action}"}, status=400)
    return web.json_response({"ok": True, "data": res, "capacity": await build_capacity()})


ISO_KEYS = ("enabled", "private_in_group", "cross_group", "group_in_private")


# ---------------------------------------------------------------- 启动台
#
# 为什么"一键启动"只能管到地基下面那层：
# 这个页面**是 AstrBot 自己发的**。AstrBot 没跑，页面就不存在，
# 所以页面里的按钮不可能把 AstrBot 自己从无到有拉起来 —— 那是
# 桌面图标「一键启动」的活。页面里能做的是：地基起来之后，
# 把旁边几件东西（SnowLuma、她的 QQ、本进程重启）管起来。

def _snowluma_dir() -> Path:
    """SnowLuma 网关目录。可用 LITTLEFISH_SNOWLUMA_DIR 覆盖。"""
    env = os.environ.get("LITTLEFISH_SNOWLUMA_DIR")
    if env:
        return Path(env)
    return config.ROOT.parent.parent / "SnowLuma"


def _bot_qq() -> str:
    """角色的 QQ 号；没配就空字符串。"""
    return str(config.cfg().get("bot", {}).get("qq", "") or "")


def _restart_script() -> Path:
    return config.ROOT / "tools" / "restart_host.ps1"

_DETACHED = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _port_open(port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _bot_token() -> str:
    try:
        raw = json.loads((_snowluma_dir() / "config" / f"onebot_{_bot_qq()}.json").read_text(encoding="utf-8-sig"))
        return str((raw.get("networks") or {}).get("httpServers", [{}])[0].get("accessToken") or "")
    except Exception:
        return ""


async def _qq_identity() -> dict:
    """问一下 3000 端口：角色的号登着没有。"""
    if not _port_open(3000):
        return {"online": False, "why": "3000 端口没开（SnowLuma 里那个号没登录）"}
    tok = _bot_token()
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as sess:
            async with sess.get(
                "http://127.0.0.1:3000/get_login_info",
                headers={"Authorization": f"Bearer {tok}"},
            ) as r:
                data = await r.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        return {"online": False, "why": f"问不到：{exc}"}
    if data.get("status") not in ("ok", "async"):
        return {"online": False, "why": str(data.get("message") or data)[:80]}
    info = data.get("data") or {}
    return {"online": True, "user_id": str(info.get("user_id") or ""),
            "nickname": str(info.get("nickname") or ""), "why": ""}


async def build_boot() -> dict:
    qq = await _qq_identity()
    return {
        "host": {"up": True, "port_6185": _port_open(6185), "port_6197": _port_open(PORT)},
        "snowluma": {
            "up": _port_open(5099),
            "onebot": _port_open(3000) and _port_open(3001),
            "dir": str(_snowluma_dir()),
            "node": (_snowluma_dir() / "node.exe").exists(),
        },
        "qq": qq,
        "bot_qq": _bot_qq(),
    }


def _start_snowluma() -> dict:
    if _port_open(5099):
        return {"ok": True, "already": True, "why": "已经在跑了，没重复开"}
    if not (_snowluma_dir() / "node.exe").exists():
        return {"ok": False, "why": f"找不到 {_snowluma_dir() / 'node.exe'}"}
    if not (_snowluma_dir() / "index.mjs").exists():
        return {"ok": False, "why": f"找不到 {_snowluma_dir() / 'index.mjs'}"}
    logf = open(_snowluma_dir() / ".out.log", "ab")          # noqa: SIM115
    subprocess.Popen(
        [str(_snowluma_dir() / "node.exe"), "index.mjs"],
        cwd=str(_snowluma_dir()), stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        creationflags=_DETACHED, close_fds=True,
    )
    deadline = time.time() + 25
    while time.time() < deadline:
        if _port_open(5099):
            return {"ok": True, "waited": round(time.time() - (deadline - 25), 1)}
        time.sleep(0.5)
    return {"ok": False, "why": f"起了但 25 秒内 5099 没开，看 {_snowluma_dir() / '.$out.log'}"}


def _restart_host() -> dict:
    """重启 AstrBot 自己：交给一个分离出去的 PowerShell 干，它先等我们回话。"""
    if not _restart_script().exists():
        return {"ok": False, "why": f"找不到 {_restart_script()}"}
    (config.ROOT / "data" / "tmp").mkdir(parents=True, exist_ok=True)
    logf = open(config.ROOT / "data" / "tmp" / "restart_host.log", "ab")  # noqa: SIM115
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(_restart_script())],
        stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
        creationflags=_DETACHED, close_fds=True,
    )
    return {"ok": True, "why": "3 秒后重启，页面会断一下，等 20 秒刷新"}


async def _api_boot(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    if request.method == "POST":
        body = await request.json()
        action = str(body.get("action") or "")
        if action == "snowluma":
            res = _start_snowluma()
        elif action == "restart":
            res = _restart_host()
        elif action == "check":
            res = {"ok": True}
        else:
            return web.json_response({"ok": False, "error": f"不认识的动作：{action}"}, status=400)
        return web.json_response({"ok": True, "data": res, "boot": await build_boot()})
    return web.json_response({"ok": True, "data": await build_boot()})

async def _api_privacy(request):
    """场景隔离的四个开关。不是插件开关，所以单独一组接口。"""
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    if request.method == "POST":
        body = await request.json()
        key = str(body.get("key") or "")
        if key not in ISO_KEYS:
            return web.json_response({"ok": False, "error": f"没这个开关：{key}"}, status=400)
        full = json.loads(json.dumps(config.cfg()))
        full.setdefault("privacy", {}).setdefault("isolation", {})[key] = bool(body.get("value"))
        config.save(full)
    iso = dict(config.cfg()["privacy"].get("isolation") or {})
    return web.json_response({"ok": True, "data": {k: bool(iso.get(k, False)) for k in ISO_KEYS}})


async def _api_ability_act(request):
    try:
        _guard(request)
    except Exception:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    body = await request.json()
    action = str(body.get("action") or "")
    pid = str(body.get("id") or "")
    if action == "master_on":
        await abilities.set_master(True)
    elif action == "master_off":
        await abilities.set_master(False)
    elif action in ("on", "off"):
        res = abilities.set_enabled(pid, action == "on")
        if not res.get("ok"):
            return web.json_response({"ok": False, "error": res.get("error")}, status=400)
    elif action in ("install", "uninstall"):
        if not caps.on("l1_abilities", "allow_install", False):
            return web.json_response(
                {"ok": False, "error": "装卸锁着：先在 L1-8 打开 allow_install"}, status=400
            )
        res = abilities.install(pid) if action == "install" else abilities.uninstall(pid)
        if not res.get("ok"):
            return web.json_response({"ok": False, "error": res.get("error")}, status=400)
        await abilities._mark_pending(res["id"], action)
    elif action in ("desk_limit", "desk_reject", "desk_seal"):
        book = str(body.get("book") or "")
        if not book:
            return web.json_response({"ok": False, "error": "没说是哪本"}, status=400)
        if action == "desk_limit":
            try:
                limit = int(body.get("limit") or 0)
            except Exception:
                return web.json_response({"ok": False, "error": "上限得是数字"}, status=400)
            res = desk.resolve(book, limit)
        elif action == "desk_reject":
            res = desk.resolve(book, 0, reject=True)
        else:
            res = desk.seal(book)
        if not res.get("ok"):
            return web.json_response({"ok": False, "error": res.get("error")}, status=400)
    else:
        return web.json_response({"ok": False, "error": "unknown action"}, status=400)
    log.info(f"[console] ability {action} {pid or ''}")
    return web.json_response({"ok": True, "data": await build_abilities()})


# ---------- 书架（2026-11-01 加的第三个能力页） ----------
MAX_UPLOAD_BYTES = 12 * 1024 * 1024


def _books_view() -> dict:
    """书架上有什么。读不出来就报错，别让能力页整个 500。"""
    try:
        books = bookshelf.list_books()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[console] 读书架失败：{exc}")
        return {"books": [], "dir": str(bookshelf.books_dir()), "trash": 0,
                "chapter_chars": bookshelf.DEFAULT_CHAPTER_CHARS, "error": str(exc)[:120]}
    trash = bookshelf.books_dir() / bookshelf.TRASH_DIR
    n_trash = len([p for p in trash.iterdir() if p.is_dir()]) if trash.is_dir() else 0
    return {
        "books": books,
        "dir": str(bookshelf.books_dir()),
        "trash": n_trash,
        "chapter_chars": bookshelf.DEFAULT_CHAPTER_CHARS,
        "max_chars": bookshelf.MAX_UPLOAD_CHARS,
        "formats": ["txt", "md"],
    }


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "on", "yes")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def _upload(request) -> tuple[list, str, dict]:
    """从 multipart 或 JSON 里取出 ([(文件名, 正文), ...], 首个文件名, 选项)。

    支持一次传多个文件（拖一组字幕进来说「这就是一本书」）。
    """
    ctype = (request.content_type or "").lower()
    opts: dict = {}
    pairs: list = []
    if "multipart" in ctype:
        data = await request.post()
        got = []
        for key in ("file", "files", "file[]"):
            if hasattr(data, "getall"):
                got.extend(data.getall(key, []))
            else:  # pragma: no cover - 老版本 MultiDict
                one = data.get(key)
                if one is not None:
                    got.append(one)
        if not got:
            raise ValueError("没收到文件")
        for f in got:
            raw = f.file.read()
            pairs.append((getattr(f, "filename", "") or "", _decode(raw)))
        for k in ("keep_speakers", "chapter_chars", "slug", "title"):
            if k in data:
                opts[k] = data.get(k)
    elif "json" in ctype:
        body = await request.json()
        pairs.append((str(body.get("name") or ""), str(body.get("text") or "")))
        for k in ("keep_speakers", "chapter_chars", "slug", "title"):
            if k in body:
                opts[k] = body.get(k)
    else:
        pairs.append(("", _decode(await request.read())))
    name = pairs[0][0] if pairs else ""
    return pairs, name, opts


def _opt_int(opts: dict, key: str, default: int) -> int:
    try:
        return int(str(opts.get(key) or default))
    except Exception:  # noqa: BLE001
        return default


async def _api_books(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "data": _books_view()})


async def _api_books_toc(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    slug = str(request.query.get("slug") or "").strip()
    if not slug:
        return web.json_response({"ok": False, "error": "没说是哪本书"}, status=400)
    res = bookshelf.toc(slug)
    return web.json_response(res if res.get("ok") else {"ok": False, "error": res.get("error")},
                             status=200 if res.get("ok") else 404)


async def _api_books_read(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    slug = str(request.query.get("slug") or "").strip()
    if not slug:
        return web.json_response({"ok": False, "error": "没说是哪本书"}, status=400)
    try:
        n = int(request.query.get("n") or 1)
    except Exception:  # noqa: BLE001
        n = 1
    res = bookshelf.read(slug, n)
    if res.get("ok"):
        res["text"] = (res.get("text") or "")[:20000]
        res["preview"] = True
    return web.json_response(res if res.get("ok") else {"ok": False, "error": res.get("error")},
                             status=200 if res.get("ok") else 404)


async def _api_books_preview(request):
    """只看不写：传进来的东西洗完之后是啥样、会切成几章。"""
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        pairs, name, opts = await _upload(request)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"读上传失败：{exc}"}, status=400)
    if not pairs or not any((t or "").strip() for _, t in pairs):
        return web.json_response({"ok": False, "error": "文件是空的"}, status=400)
    keep = _truthy(opts.get("keep_speakers"))
    per = _opt_int(opts, "chapter_chars", bookshelf.DEFAULT_CHAPTER_CHARS)
    if len(pairs) > 1:
        res = bookshelf.preview_files(pairs, keep_speakers=keep, per=per)
    else:
        res = bookshelf.preview(pairs[0][1], keep_speakers=keep, per=per)
        if res.get("ok"):
            res["suggest_slug"] = bookshelf.suggest_slug(Path(name).stem if name else "书")
            res["suggest_title"] = (re.sub(r"\.(txt|md)$", "", name, flags=re.I) or "新书")
    if res.get("ok"):
        res["name"] = name
        res["files"] = [bookshelf.clean_name(n) for n, _ in pairs]
        res["source_chars"] = sum(len(t or "") for _, t in pairs)
    return web.json_response(res, status=200 if res.get("ok") else 400)


async def _api_books_import(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        pairs, name, opts = await _upload(request)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"读上传失败：{exc}"}, status=400)
    if not pairs or not any((t or "").strip() for _, t in pairs):
        return web.json_response({"ok": False, "error": "文件是空的"}, status=400)
    keep = _truthy(opts.get("keep_speakers"))
    per = _opt_int(opts, "chapter_chars", bookshelf.DEFAULT_CHAPTER_CHARS)
    src = name or "、".join(bookshelf.clean_name(n) for n, _ in pairs[:3])
    if len(pairs) > 1:
        title = str(opts.get("title") or "").strip() or bookshelf.clean_name(pairs[0][0]) or "新书"
        slug = str(opts.get("slug") or "").strip() or bookshelf.suggest_slug(title)
        res = bookshelf.import_files(pairs, slug, title, source=src,
                                     keep_speakers=keep, per=per)
    else:
        text = pairs[0][1]
        title = str(opts.get("title") or "").strip() or re.sub(r"\.(txt|md)$", "", name, flags=re.I) or "新书"
        slug = str(opts.get("slug") or "").strip() or bookshelf.suggest_slug(title)
        res = bookshelf.import_text(text, slug, title, source=name,
                                    keep_speakers=keep, per=per)
    if not res.get("ok"):
        return web.json_response(res, status=400)
    log.info(f"[console] 导入完成 {slug}：{res.get('chapters')} 章 {res.get('chars')} 字")
    res["shelf"] = _books_view()
    return web.json_response(res)


async def _api_books_trash(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    return web.json_response({"ok": True, "trash": bookshelf.trash_list()})


async def _api_books_trash_clear(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    res = bookshelf.trash_clear(_opt_int(body, "keep", 0))
    res["shelf"] = _books_view()
    return web.json_response(res)
async def _api_books_delete(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "要 JSON"}, status=400)
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return web.json_response({"ok": False, "error": "没说是哪本书"}, status=400)
    res = bookshelf.delete_book(slug)
    if not res.get("ok"):
        return web.json_response(res, status=404)
    res["shelf"] = _books_view()
    return web.json_response(res)


async def _api_books_rebuild(request):
    try:
        _guard(request)
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "要 JSON"}, status=400)
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return web.json_response({"ok": False, "error": "没说是哪本书"}, status=400)
    per = _opt_int({"chapter_chars": body.get("chapter_chars")}, "chapter_chars",
                   bookshelf.DEFAULT_CHAPTER_CHARS)
    res = bookshelf.rebuild_chapters(slug, per)
    if not res.get("ok"):
        return web.json_response(res, status=400)
    res["shelf"] = _books_view()
    return web.json_response(res)


def _make_app():
    app = web.Application(middlewares=[_guard_middleware], client_max_size=MAX_UPLOAD_BYTES)
    tok = "{token}"
    app.router.add_get(f"/c/{tok}/", _index)
    app.router.add_get(f"/c/{tok}/api/menu", _api_menu)
    app.router.add_get(f"/c/{tok}/api/summary", _api_summary)
    app.router.add_get(f"/c/{tok}/api/compat", _api_compat)
    app.router.add_post(f"/c/{tok}/api/set", _api_set)
    app.router.add_post(f"/c/{tok}/api/act", _api_act)
    app.router.add_get(f"/c/{tok}/api/persona", _api_persona)
    app.router.add_post(f"/c/{tok}/api/persona", _api_persona)
    app.router.add_get(f"/c/{tok}/api/memories", _api_memories)
    app.router.add_post(f"/c/{tok}/api/memory", _api_memory_act)
    app.router.add_get(f"/c/{tok}/api/inject", _api_inject)
    app.router.add_get(f"/c/{tok}/api/note", _api_note)
    app.router.add_post(f"/c/{tok}/api/note", _api_note)
    app.router.add_get(f"/c/{tok}/api/addr", _api_addr)
    app.router.add_post(f"/c/{tok}/api/addr", _api_addr)
    app.router.add_get(f"/c/{tok}/api/abilities", _api_abilities)
    app.router.add_get(f"/c/{tok}/api/desk", _api_desk)
    app.router.add_post(f"/c/{tok}/api/ability", _api_ability_act)
    app.router.add_get(f"/c/{tok}/api/privacy", _api_privacy)
    app.router.add_post(f"/c/{tok}/api/privacy", _api_privacy)
    app.router.add_get(f"/c/{tok}/api/capacity", _api_capacity)
    app.router.add_post(f"/c/{tok}/api/memory-bulk", _api_memory_bulk)
    app.router.add_get(f"/c/{tok}/api/boot", _api_boot)
    app.router.add_post(f"/c/{tok}/api/boot", _api_boot)
    app.router.add_get(f"/c/{tok}/api/codes", _api_codes)
    app.router.add_post(f"/c/{tok}/api/codes", _api_codes)
    app.router.add_get(f"/c/{tok}/api/think", _api_think)
    app.router.add_post(f"/c/{tok}/api/think-clear", _api_think_clear)
    app.router.add_get(f"/c/{tok}/api/books", _api_books)
    app.router.add_get(f"/c/{tok}/api/books/toc", _api_books_toc)
    app.router.add_get(f"/c/{tok}/api/books/read", _api_books_read)
    app.router.add_post(f"/c/{tok}/api/books/preview", _api_books_preview)
    app.router.add_post(f"/c/{tok}/api/books/import", _api_books_import)
    app.router.add_post(f"/c/{tok}/api/books/delete", _api_books_delete)
    app.router.add_post(f"/c/{tok}/api/books/rebuild", _api_books_rebuild)
    app.router.add_get(f"/c/{tok}/api/books/trash", _api_books_trash)
    app.router.add_post(f"/c/{tok}/api/books/trash-clear", _api_books_trash_clear)
    return app


async def start() -> str:
    """起管理台，返回给浏览器打开用的地址；起不来就返回空串（不拖垮宿主）。"""
    global _APP, _RUNNER, _STARTED_AT, HOST
    if _RUNNER is not None:
        return url()
    # 管理器 = 插件开关 + 备份恢复 + 人设编辑，绝不能对外。绑错了直接改回来。
    if HOST not in ("127.0.0.1", "::1", "localhost"):
        log.error(f"[console] HOST={HOST} 不是回环地址，强制改回 127.0.0.1")
        HOST = "127.0.0.1"
    token = _ensure_token()
    try:
        _APP = _make_app()
        _RUNNER = web.AppRunner(_APP, access_log=None)
        await _RUNNER.setup()
        site = web.TCPSite(_RUNNER, HOST, PORT)
        await site.start()
        _STARTED_AT = time.time()
        log.info(f"[console] 管理台 http://{HOST}:{PORT}/c/{token}/")
        return url()
    except Exception as exc:  # 端口被占之类
        log.warning(f"[console] 起不来（{exc}），管理器先不用")
        _RUNNER = None
        return ""


async def stop() -> None:
    global _APP, _RUNNER
    if _RUNNER is not None:
        try:
            await _RUNNER.cleanup()
        except Exception:
            pass
    _APP = None
    _RUNNER = None


def url() -> str:
    return f"http://{HOST}:{PORT}/c/{_ensure_token()}/"
