"""运维（K7）：体检、备份、日志尾巴、插件清单。

这一层不碰网络，只读本地状态 —— 所以它永远不会把机器人弄崩，
而它恰恰是最该在机器人崩了之后还能用的东西。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import caps, config, db, memory
from .log import log

BACKUP_KEEP = 10
# 切换前整包备份（tools/use_character.py 写的 before-switch-*，带 _switch.json）单独一池。
# 和 auto 备份混在一起数会互相挤：切十次角色，auto 备份就全没了。
SWITCH_KEEP = 5


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _disk_free(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path.parent if path.is_file() else path)).free
    except Exception:
        return 0


def _check_db(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "库文件不存在"}
    out: dict[str, Any] = {"ok": True, "bytes": path.stat().st_size}
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            out[f"bytes{suffix.replace('-', '_')}"] = side.stat().st_size
    try:
        with sqlite3.connect(str(path), timeout=5) as conn:
            out["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            out["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
            out["tables"] = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


def plugin_dirs() -> list[dict[str, Any]]:
    """列出 data/plugins 下的插件（名字 + 版本 + 有没有配置文件）。"""
    root = config.astrbot_dir() / "data" / "plugins"
    config_dir = Path(caps_dir())
    items: list[dict[str, Any]] = []
    if not root.exists():
        return items
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = d / "metadata.yaml"
        name, version = d.name, ""
        if meta.exists():
            for line in meta.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("version:"):
                    version = line.split(":", 1)[1].strip().strip('"')
        items.append({
            "dir": d.name,
            "name": name,
            "version": version,
            "has_schema": (d / "_conf_schema.json").exists(),
            "has_config": (config_dir / f"{name}_config.json").exists(),
        })
    return items


def caps_dir() -> str:
    return os.environ.get("LITTLEFISH_PLUGIN_CONFIG_DIR", caps.data_config_dir())


async def health() -> dict[str, Any]:
    cfg = config.cfg()
    dbp = Path(config.db_path())
    mem = await memory.stats()
    t = await today_usage()
    sched = None
    try:
        from . import scheduler as _sched
        s = _sched.get()
        sched = {
            "running": bool(s.running),
            "jobs": [j.id for j in s.get_jobs()] if s.running else [],
        }
    except Exception as exc:
        sched = {"running": False, "error": str(exc)}

    render_qq = await memory.render("qq")
    budget = float(cfg["memory"].get("inject_max_chars", {}).get("qq", 2200) or 0)

    issues: list[str] = []
    dbc = _check_db(dbp)
    if not dbc.get("ok"):
        issues.append(f"库异常：{dbc.get('error')}")
    elif dbc.get("integrity") != "ok":
        issues.append(f"库完整性：{dbc.get('integrity')}")
    if not await _persona_present():
        issues.append("人设卡是空的")
    if budget and len(render_qq) > budget * 1.2:
        issues.append(f"记忆注入超预算（{len(render_qq)}>{budget}）")
    if not sched or not sched.get("running"):
        issues.append("调度器没在跑（衰减不会发生）")
    if not t["n"]:
        # 凌晨刚过、或者一整天没人说话，零用量是正常的；
        # 只有"今天有人说过话、却没记到一次账"才是模型没接通的信号。
        talked = 0
        try:
            day0 = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
            row = await db.fetchone(
                "SELECT COUNT(*) AS n FROM events WHERE ts >= ? AND topic LIKE 'qq.%'", (day0,))
            talked = int(row["n"] or 0) if row else 0
        except Exception:
            talked = 0
        if talked:
            issues.append("今天有人说话但还没记过账（模型可能没接通）")

    # 睡前那一页：00:05 没跑成、开机补跑也没成功，第二天上午就该有人知道。
    # 阈值放在 06:00 之后，免得凌晨刚过就误报。
    try:
        from . import nightly as _night
        if int(time.strftime("%H")) >= 6:
            nst = await _night.status()
            if not nst.get("chat_ready"):
                issues.append("睡前回顾没接上模型（日记写不了）")
            elif nst.get("pending") and not nst.get("yesterday_done"):
                issues.append(f"昨晚（{nst.get('yesterday')}）那一页还没写")
    except Exception as exc:
        issues.append(f"睡前回顾状态查不出来：{exc}")
    # `.failed-*` 是切角色失败时留下的半成品：按设计永不自动删，也没人看，
    # 不提醒的话半年后就是一堆没人认领的目录。
    try:
        bdir = Path(config.ROOT) / "backups"
        if bdir.exists():
            failed = [d for d in bdir.iterdir() if d.is_dir() and ".failed-" in d.name]
            if failed:
                issues.append(f"有 {len(failed)} 份失败的备份（可手工清）")
    except Exception:
        pass

    return {
        "character": cfg["character"],
        "db": dbc,
        "memory": mem,
        "render_chars": {"qq": len(render_qq)},
        "inject_budget": budget,
        "scheduler": sched,
        "usage_today": t,
        "disk_free_gb": round(_disk_free(Path(config.ROOT)) / (1 << 30), 1),
        "archive_bytes": _dir_size(config.archive_dir()),
        "plugins": plugin_dirs(),
        "issues": issues,
        "ok": not issues,
    }


async def _persona_present() -> bool:
    from . import persona
    try:
        return bool((await persona.render()).strip())
    except Exception:
        return False


async def today_usage() -> dict[str, Any]:
    from . import budget
    return await budget.today()


def _verify_db(path: Path) -> dict[str, Any]:
    """备份出来的库自己查一遍：能不能读、有几条记忆。查不过就是坏备份，不能算完成。"""
    out: dict[str, Any] = {"ok": False, "quick_check": "", "rows": 0}
    if not path.exists():
        out["quick_check"] = "库文件不存在"
        return out
    try:
        c = sqlite3.connect(str(path), timeout=10)
        try:
            row = c.execute("PRAGMA quick_check").fetchone()
            out["quick_check"] = str((row or [""])[0])
            out["rows"] = int(c.execute("SELECT count(*) FROM memory").fetchone()[0])
            out["ok"] = out["quick_check"] == "ok"
        finally:
            c.close()
    except Exception as exc:            # 备份坏了就如实说，不要假装成功
        out["quick_check"] = f"{type(exc).__name__}: {exc}"
    return out


def _sync_backup(dest: Path) -> dict[str, Any]:
    """在同步线程里做：sqlite online backup + 配置与人设文本 + 全量归档。"""
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(config.db_path())
    character = config.cfg()["character"]
    target = dest / f"littlefish-{character}.db"
    verify: dict[str, Any] = {"ok": False, "quick_check": "没有库", "rows": 0}
    if src.exists():
        # 注意：`with sqlite3.connect()` 只管事务、不管关闭。连接不关，
        # Windows 上库的 -wal 会一直被占着，restore 时删不掉（真踩过）。
        s = sqlite3.connect(str(src), timeout=10)
        d = sqlite3.connect(str(target))
        try:
            s.backup(d)
        finally:
            s.close()
            d.close()
        # 备份不等于可用：自己查一遍再算数
        verify = _verify_db(target)
    copied: list[str] = []
    # 人设文本（人能读、能直接改的那份）
    for p in config.character_dir().glob("*.md"):
        shutil.copy2(p, dest / p.name)
        copied.append(p.name)
    cfgfile = Path(config.ROOT) / "config.json"
    if cfgfile.exists():
        shutil.copy2(cfgfile, dest / "config.json")
        copied.append("config.json")
    # 二级开关
    capdir = Path(caps_dir())
    if capdir.exists():
        sub = dest / "plugin-config"
        sub.mkdir(exist_ok=True)
        for p in capdir.glob("astrbot_littlefish_*_config.json"):
            shutil.copy2(p, sub / p.name)
            copied.append(f"plugin-config/{p.name}")
    # 桌上的本子（她自己写的东西，丢了找不回来）
    ddir = Path(config.ROOT) / "data" / "desk"
    if ddir.is_dir():
        (dest / "desk").mkdir(parents=True, exist_ok=True)
        for p in ddir.glob("*.md"):
            shutil.copy2(p, dest / "desk" / p.name)
            copied.append(f"desk/{p.name}")
    # 全量归档（append-only 的原始流水，是"永不丢"那一层；不备份它等于没兜底）
    archive_bytes = 0
    adir = config.archive_dir()
    if adir.exists():
        for p in adir.rglob("*.jsonl"):
            rel = p.relative_to(adir)
            t = dest / "archive" / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)
            archive_bytes += p.stat().st_size
            copied.append(f"archive/{rel.as_posix()}")
    files = []
    for p in sorted(dest.rglob("*")):
        if p.is_file() and p.name != "_done.json":
            files.append({"name": str(p.relative_to(dest)), "bytes": p.stat().st_size,
                          "sha256": _sha256(p)})
    # 完成标记：没有它的目录视为"半截备份"，轮转时不删也不当最新的用
    done = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "character": character,
        "db_quick_check": verify.get("quick_check", ""),
        "db_rows": verify.get("rows", 0),
        "db_ok": bool(verify.get("ok")),
        "archive_bytes": archive_bytes,
        "files": len(files),
    }
    (dest / "_done.json").write_text(json.dumps(done, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    return {"dir": str(dest), "files": files, "verify": verify,
            "archive_bytes": archive_bytes, "done": done}


async def backup(keep: int = BACKUP_KEEP) -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = Path(config.ROOT) / "backups" / stamp
    res = await asyncio.to_thread(_sync_backup, dest)
    if not res["verify"].get("ok"):
        # 坏备份：留着给人查，但不要让它顶掉好备份
        log.error(f"[ops] 备份校验没过：{res['verify'].get('quick_check')} -> {dest}")
    # 两族分开轮转，只删"有完成标记"的目录：
    #   auto   —— 带 _done.json 且不以 before-switch- 开头，按 keep（默认 10）
    #   switch —— 带 _switch.json 的切换前整包备份，按 SWITCH_KEEP（5）
    # .failed-*（写一半失败改名的）和真正的半截目录一律不删，也绝不按 mtime 判断新旧。
    root = Path(config.ROOT) / "backups"
    dirs = sorted([d for d in root.iterdir() if d.is_dir()], reverse=True)
    auto = [d for d in dirs if (d / "_done.json").exists() and not d.name.startswith("before-switch-")]
    switch = [d for d in dirs if (d / "_switch.json").exists()]
    marked = set(auto) | set(switch)
    removed = []
    skipped = [d.name for d in dirs
               if d not in marked and ".failed-" not in d.name]
    for d in auto[keep:]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    for d in switch[SWITCH_KEEP:]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    if skipped:
        log.warning(f"[ops] 有 {len(skipped)} 份没写完成标记的备份，没参与轮转：{skipped[:5]}")
    res["removed"] = removed
    res["skipped"] = skipped
    res["auto_kept"] = len(auto) - len(auto[keep:])
    res["switch_kept"] = len(switch) - len(switch[SWITCH_KEEP:])
    res["total_bytes"] = sum(f["bytes"] for f in res["files"])
    log.info(f"[ops] backup {res['dir']} files={len(res['files'])} "
             f"bytes={res['total_bytes']} rows={res['verify'].get('rows')} "
             f"check={res['verify'].get('quick_check')}")
    return res


async def restore_point(backup_dir: str) -> dict[str, Any]:
    """把一份备份里的库换回去。**调用方必须先停 AstrBot**（否则别的进程还开着库）。

    选这个做法（整库覆盖）而不是"从备份里挑记录合并"：合并会制造两套 id，
    比丢几小时数据更难收拾。宁可退回到一个确定的时刻。

    2026-10-30 修：本进程自己也开着库，Windows 下 -wal/-shm 删不掉
    （PermissionError: [WinError 32]）。所以先关连接再动文件，返回后调用方
    下次 connect() 会自动重开。
    """
    from . import db

    src = Path(backup_dir)
    if not src.exists():
        return {"ok": False, "error": "备份目录不存在"}
    dbs = list(src.glob("littlefish-*.db"))
    if not dbs:
        return {"ok": False, "error": "备份里没有库文件"}
    # 先验再换：拿一份坏库去覆盖好库，是唯一比"丢几小时"更糟的结局
    check = _verify_db(dbs[0])
    if not check.get("ok"):
        return {"ok": False, "error": f"这份备份自己就是坏的（{check.get('quick_check')}），不换",
                "verify": check}
    if not (src / "_done.json").exists():
        log.warning(f"[ops] {src.name} 没有 _done.json，可能是半截备份 —— 校验过了，继续恢复")
    current = Path(config.db_path())
    safety = None
    if current.exists():
        safety = current.with_suffix(".db.before-restore-" + time.strftime("%H%M%S"))
        shutil.copy2(current, safety)
    await db.close()
    for suffix in ("-wal", "-shm"):
        side = Path(str(current) + suffix)
        if side.exists():
            try:
                side.unlink()
            except PermissionError:
                return {"ok": False, "error": f"库文件被占用，先停 AstrBot：{side.name}"}
    shutil.copy2(dbs[0], current)
    # 归档也跟着回去（不然库里的"已归档"标记指向了不存在的流水）
    adir = src / "archive"
    if adir.exists():
        dst_archive = config.archive_dir()
        dst_archive.mkdir(parents=True, exist_ok=True)
        for p in adir.rglob("*.jsonl"):
            t = dst_archive / p.relative_to(adir)
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)
    # 本子：只补回本机没有的，不覆盖她后来写的
    ddir = src / "desk"
    desk_back = 0
    if ddir.is_dir():
        dst_desk = Path(config.ROOT) / "data" / "desk"
        dst_desk.mkdir(parents=True, exist_ok=True)
        for p in sorted(ddir.glob("*.md")):
            t = dst_desk / p.name
            if not t.exists():
                shutil.copy2(p, t)
                desk_back += 1
    return {"ok": True, "restored_from": str(dbs[0]), "into": str(current),
            "rows": check.get("rows"), "desk_restored": desk_back,
            "safety": str(safety) if safety else ""}


def log_tail(lines: int = 40, pattern: str = "ERROR|WARNING|Traceback|littlefish") -> list[str]:
    logs: list[Path] = []
    for name in (".out.log", ".err.log"):
        p = config.astrbot_dir() / name
        if p.exists():
            logs.append(p)
    if not logs:
        return ["没有日志文件"]
    out: list[str] = []
    import re
    regex = re.compile(pattern)
    for p in logs:
        try:
            data = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-2000:]
        except Exception:
            continue
        for line in reversed(data):
            if regex.search(line):
                out.append(f"{p.name}: {line.strip()[:200]}")
                if len(out) >= lines:
                    return out
    return out or ["最近没有告警"]
