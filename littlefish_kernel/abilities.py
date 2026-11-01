"""能力（abilities）——她会的新东西：听歌、看书、以后可能还有一整个虚拟社会。

这一层的规矩（和七个一级同一套）：

1. **一个能力 = 一个独立的 AstrBot 插件**，目录名 `astrbot_ability_<id>`，
   带一份 `ability.json`（人看的说明书）+ 自己的 `main.py`。
   宿主（L1-8）**不 import 任何能力**，只读写它们的开关文件。
2. **拔掉一个能力，她就不会这件事**：工具从模型工具面摘掉、提示词里不提、
   指令直接拒。三处都得断，只断一处就是旧系统那种"关了还在跑"。
3. **总闸关掉 = 全部能力静音**，但开关状态会被记下来，重新打开时原样恢复。
4. 装/卸能力是**在内核里做的**（不是 L1-8 自己做的）：装能力 = 往插件目录里放
   可执行代码，这个权限不能绑在一个可以被热插拔的插件上。所以：
   管理器（6197）能装卸，`/ability` 也能，但两者都走这里同一个实现。

目录约定：

    <ROOT>/abilities/<pack>/            待装的能力包（源码，含 ability.json）
    <AstrBot>/data/plugins/astrbot_ability_<id>/  已装的能力
    <ROOT>/abilities/_removed/<时间戳>/     卸下来的（不直接删，能装回去）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import config, db
from .log import log

PREFIX = "astrbot_ability_"
MASTER_PLUGIN = "astrbot_littlefish_l1_abilities"
KV_SAVED = "abilities.saved"
KV_PENDING = "abilities.pending_restart"


# ---------------------------------------------------------------- 路径

def plugins_dir() -> Path:
    """AstrBot 的插件目录。可用 LITTLEFISH_PLUGINS_DIR 覆盖（测试用）。"""
    env = os.environ.get("LITTLEFISH_PLUGINS_DIR")
    if env:
        return Path(env)
    from .config import astrbot_dir
    return astrbot_dir() / "data" / "plugins"


def config_dir() -> Path:
    env = os.environ.get("LITTLEFISH_PLUGIN_CONFIG_DIR")
    if env:
        return Path(env)
    from .config import astrbot_dir
    return astrbot_dir() / "data" / "config"


def packs_dir() -> Path:
    return config.ROOT / "abilities"


def removed_dir() -> Path:
    return packs_dir() / "_removed"


def _manifest_of(folder: Path) -> dict[str, Any] | None:
    f = folder / "ability.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:  # 坏 json 不该炸掉整个列表
        log.warning(f"[abilities] {f} 读不懂：{exc}")
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return data


def _safe_id(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_\-]", "", str(raw or "")).strip("_-").lower()
    return s[:40]


# ---------------------------------------------------------------- 状态文件

def _cfg_file(plugin: str) -> Path:
    return config_dir() / f"{plugin}_config.json"


def _read_cfg(plugin: str) -> dict[str, Any]:
    f = _cfg_file(plugin)
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _write_cfg(plugin: str, key: str, value: Any) -> None:
    f = _cfg_file(plugin)
    data = _read_cfg(plugin)
    data[key] = value
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if f.exists():
        try:
            os.replace(tmp, f)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            raise
    else:
        tmp.replace(f)


def enabled_of(plugin: str, default: bool = True) -> bool:
    v = _read_cfg(plugin).get("enabled", default)
    return bool(v)


# ---------------------------------------------------------------- 列表

def scan() -> dict[str, list[dict[str, Any]]]:
    """返回 {"installed": [...], "packs": [...]}。两个列表都带 id/name/desc。"""
    installed: list[dict[str, Any]] = []
    for folder in sorted(plugins_dir().glob(PREFIX + "*")):
        if not folder.is_dir():
            continue
        man = _manifest_of(folder) or {}
        pid = _safe_id(man.get("id") or folder.name[len(PREFIX):])
        plugin = folder.name
        installed.append({
            "id": pid,
            "plugin": plugin,
            "name": man.get("name") or pid,
            "version": man.get("version") or "",
            "desc": man.get("desc") or "",
            "tools": list(man.get("tools") or []),
            "needs": man.get("needs") or {},
            "enabled": enabled_of(plugin),
            "broken": bool(man.get("_broken")) or not (folder / "main.py").is_file(),
            "dir": str(folder),
        })

    have = {x["id"] for x in installed}
    packs: list[dict[str, Any]] = []
    if packs_dir().is_dir():
        for folder in sorted(packs_dir().iterdir()):
            if not folder.is_dir() or folder.name.startswith("_"):
                continue
            man = _manifest_of(folder)
            if not man:
                continue
            pid = _safe_id(man.get("id") or folder.name)
            if pid in have:
                continue
            packs.append({
                "id": pid,
                "name": man.get("name") or pid,
                "version": man.get("version") or "",
                "desc": man.get("desc") or "",
                "dir": str(folder),
            })
    return {"installed": installed, "packs": packs}


def get(pid: str) -> dict[str, Any] | None:
    pid = _safe_id(pid)
    for row in scan()["installed"]:
        if row["id"] == pid:
            return row
    return None


# ---------------------------------------------------------------- 总闸

def master() -> bool:
    """总闸就是 L1-8 自己的 enabled（唯一的真相源，不另存一份）。"""
    return enabled_of(MASTER_PLUGIN, True)


async def set_master(on: bool) -> dict[str, Any]:
    """总闸。关：先记下每个能力的开关，再全部写 false；开：按记录恢复。"""
    on = bool(on)
    rows = scan()["installed"]
    if on:
        saved = json.loads((await db.kv_get(KV_SAVED, "{}")) or "{}")
        for row in rows:
            want = bool(saved.get(row["id"], row.get("enabled", True)))
            _write_cfg(row["plugin"], "enabled", want)
        _write_cfg(MASTER_PLUGIN, "enabled", True)
        await db.kv_del(KV_SAVED)
        log.info(f"[abilities] master on, restored {len(rows)}")
        return {"master": True, "restored": len(rows)}

    state = {row["id"]: bool(row.get("enabled", True)) for row in rows}
    await db.kv_set(KV_SAVED, json.dumps(state, ensure_ascii=False))
    for row in rows:
        _write_cfg(row["plugin"], "enabled", False)
    _write_cfg(MASTER_PLUGIN, "enabled", False)
    log.info(f"[abilities] master off, silenced {len(rows)}")
    return {"master": False, "silenced": len(rows), "saved": state}


# ---------------------------------------------------------------- 单个开关

def set_enabled(pid: str, on: bool) -> dict[str, Any]:
    row = get(pid)
    if not row:
        return {"ok": False, "error": f"没有这个能力：{pid}"}
    _write_cfg(row["plugin"], "enabled", bool(on))
    return {"ok": True, "id": row["id"], "enabled": bool(on), "plugin": row["plugin"]}


async def _mark_pending(pid: str, why: str) -> list[str]:
    cur = json.loads((await db.kv_get(KV_PENDING, "[]")) or "[]")
    item = {"id": pid, "why": why, "at": int(time.time())}
    cur = [c for c in cur if c.get("id") != pid] + [item]
    await db.kv_set(KV_PENDING, json.dumps(cur, ensure_ascii=False))
    return [c["id"] for c in cur]


async def pending_restart() -> list[dict[str, Any]]:
    return json.loads((await db.kv_get(KV_PENDING, "[]")) or "[]")


async def clear_pending() -> None:
    await db.kv_del(KV_PENDING)


# ---------------------------------------------------------------- 装 / 卸

def install(pack_id: str) -> dict[str, Any]:
    """从 abilities/<pack> 装到插件目录。装完要重启 AstrBot 才生效（插件是启动时加载的）。"""
    pack_id = _safe_id(pack_id)
    src = packs_dir() / pack_id
    if not src.is_dir():
        # 也允许传一个与 id 不同的目录名
        for folder in packs_dir().glob("*"):
            if folder.is_dir() and _safe_id(folder.name) == pack_id:
                src = folder
                break
    if not src.is_dir():
        return {"ok": False, "error": f"没找到能力包：{pack_id}"}
    man = _manifest_of(src)
    if not man:
        return {"ok": False, "error": "这个包缺 ability.json（或者格式不对）"}
    pid = _safe_id(man.get("id"))
    dest = plugins_dir() / (PREFIX + pid)
    if dest.exists():
        return {"ok": True, "id": pid, "already": True, "dir": str(dest)}
    plugins_dir().mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    log.info(f"[abilities] installed {pid} -> {dest}")
    return {"ok": True, "id": pid, "dir": str(dest), "need_restart": True}


def uninstall(pid: str) -> dict[str, Any]:
    """卸下来：挪到 abilities/_removed/<时间戳>/，不直接删（后悔能装回去）。"""
    row = get(pid)
    if not row:
        return {"ok": False, "error": f"没有这个能力：{pid}"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = removed_dir() / stamp
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / Path(row["dir"]).name
    shutil.move(row["dir"], str(target))
    log.info(f"[abilities] uninstalled {pid} -> {target}")
    return {"ok": True, "id": row["id"], "moved_to": str(target), "need_restart": True}


# ---------------------------------------------------------------- 体检

async def health() -> dict[str, Any]:
    data = scan()
    issues: list[str] = []
    for row in data["installed"]:
        if row["broken"]:
            issues.append(f"能力「{row['name']}」缺 main.py 或 ability.json 坏了")
        cfg = _read_cfg(row["plugin"])
        if cfg and "enabled" not in cfg:
            issues.append(f"能力「{row['name']}」的开关文件里没有 enabled（会用默认开）")
    if not master():
        issues.append("能力总闸是关着的（所有能力静音）")
    pend = await pending_restart()
    if pend:
        issues.append("有装卸还没生效，要重启 AstrBot：" + "、".join(p["id"] for p in pend))
    return {
        "master": master(),
        "installed": data["installed"],
        "packs": data["packs"],
        "pending_restart": pend,
        "issues": issues,
        "ok": not issues,
    }
