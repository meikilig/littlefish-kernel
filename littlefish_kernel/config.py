"""全局配置：一个根目录，一份真相。

所有路径都从 ROOT 派生，不允许散落在其他盘符。
配置本体存放在 <ROOT>/config.json，缺失时自动写入默认值。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 根目录可以用环境变量覆盖（测试/搬家时用）；没设就用包外面同级目录下的 littlefish/
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "littlefish"
ROOT = Path(os.environ.get("LITTLEFISH_ROOT", str(_DEFAULT_ROOT))).resolve()

DEFAULT_CONFIG: dict[str, Any] = {
    "character": "test",          # 当前角色目录名，切换角色只改这里
    "owner": {
        "qq": "",                 # 主人 QQ 号，空 = 不特殊对待
        "label": "主人",
    },
    "platforms": {
        "qq": {"enabled": True},
        "bili": {"enabled": True},
    },
    "memory": {
        "inject_max_chars": {"qq": 2200, "bili": 2000},
        "warn_chars": 60000,
        "max_chars": 0,           # 0 = 不限
        "max_records": 600,
        "daily_hour": 4,          # 每天几点跑衰减
        "decay_per_day": 1,       # 每天最多降几档
        "decay_min_age_days": 1,  # 今天刚记的事今天不降档
        "fuzzy_after_days": 7,
        "forget_after_days": 60,
        "min_owner_importance": 65,
        "first_window_days": 30,
        "first_slots": 3,          # 「最初的事」最多留几条
        "maintain_interval_min": 10,
    },
    "privacy": {
        "include_qq_in_bili": False,   # 默认不把 QQ 私事带进 B站 提示词
        "redact": True,
        # 场景隔离：她记得的事，只在她记得的那个场景里提。
        "isolation": {
            "enabled": True,
            "private_in_group": False,   # 私聊里的记忆，默认不在群里提
            "cross_group": False,        # A 群的事，默认不在 B 群提
            "group_in_private": False,   # 群里的记忆，默认不在私聊里提
        },
    },
    "budget": {
        "enabled": True,
        "currency": "CNY",
        "prices": {
            # 每百万 token 的价格，按需改；单位：currency
            "default": {"input": 2.0, "cached": 0.5, "output": 8.0}
        },
        "daily_limit": 0,          # 0 = 不限制
        "warn_ratio": 0.8,
        "per_feature": {           # 单个一级功能每天最多花多少，0 = 不限
            "llm": 0,
            "bili": 0,
            "voice": 0,
        },
        "hard_stop": True,         # 超了是直接停，还是只报警
    },
    "self_note": {"max_entries": 40},
    # 睡前那一页（L1-10）：00:05 写昨天；主人关电脑没写成，下次开机补（最多 3 天）
    "nightly": {"enabled": True, "hour": 0, "minute": 5, "catchup_delay_sec": 180},
    # 身体（能力-疲劳与作息）：精神值 -100..100，读时按时间差推演。
    # P0 只记账：tick 只落盘，注入先只写日志（enabled=false 可整体关掉）。
    "vitals": {
        "enabled": True,
        "awake_rate": 3.0,          # 醒着每小时掉多少
        "oversleep_penalty": 0.8,   # 该睡没睡，额外每小时再掉多少
        "onset_at": 60.0,           # 窗口内低于这个值就想睡
        "wake_at": 100.0,           # 睡到这么多自然醒
        "drowsy_at": 0.0,           # 低于这个值算「发困」（主人 2026-11-01 定：先设 0）
        "lock_at": -30.0,           # 低于这个值进睡眠锁（透支）
        "unlock_at": -10.0,         # 回到这个值才解锁（迟滞 20 点）
        "quiet_minutes": 10,        # 最近这么多分钟没人说话才会自动睡
        "resleep_minutes": 25,      # 被叫醒后这么久内不再睡回去
        "wake_penalty": 3.0,        # 每次被叫醒额外扣多少
        "short_sleep_minutes": 90,  # 短于这个时长的那段算浅睡
        "short_sleep_rate": 2.0,    # 浅睡每小时算回多少
        "max_dt_hours": 36,         # 单次推演最多按这么多小时算（防时间跳变）
        "tick_minutes": 5,          # 每几分钟落一次盘
        "history": True,            # tick 时顺手记一条 vitals.tick（看趋势用；一天 288 条）
        # 超速疲劳：当天花的钱超了这条线，精神就掉得比平常快。
        # 这是**独立**的口子，不去动 budget 的闸门（那边 daily_limit 一旦有数会直接拦回复）。
        "overload_yuan": 0.0,       # 每天花超这个数（元）就开始超速；0 = 关
        "overload_per_hour": 6.0,   # 超线后额外每小时掉多少点（醒着 3 + 6 = 9/h）
        # 睡眠窗口：先只有两段，想加就往后追加一条
        # {"name","start","end","rate","recovery_cap"}
        "windows": [
            {"name": "夜里长睡", "start": "23:30", "end": "07:30",
             "rate": 6.0, "recovery_cap": 50},
            {"name": "午睡", "start": "13:00", "end": "13:40",
             "rate": 6.0, "recovery_cap": 10},
        ],
    },
}


def astrbot_dir() -> Path:
    """AstrBot 的安装目录。

    优先级：环境变量 LITTLEFISH_ASTRBOT_DIR > <ROOT> 同级目录下的 astrbot/ >
    用户目录下的 astrbot/。内核本身不假设装在哪个盘，找不到就让调用方自己处理。
    """
    env = os.environ.get("LITTLEFISH_ASTRBOT_DIR")
    if env:
        return Path(env)
    for cand in (ROOT.parent / "astrbot", Path.home() / "astrbot"):
        if cand.is_dir():
            return cand
    return ROOT.parent / "astrbot"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs() -> None:
    for sub in ("", "data", "data/archive", "data/persona", "backups", "logs"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    path = ROOT / "config.json"
    if not path.exists():
        path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw)


_cfg: dict[str, Any] | None = None


def cfg() -> dict[str, Any]:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def reload() -> dict[str, Any]:
    global _cfg
    _cfg = load_config()
    return _cfg


def save(full: dict[str, Any]) -> dict[str, Any]:
    """把整份配置写回 config.json（临时文件 + 原子替换），再刷新内存里的那份。"""
    ensure_dirs()
    path = ROOT / "config.json"
    tmp = path.with_name(f"config.json.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return reload()


def data_dir() -> Path:
    return ROOT / "data"


def archive_dir() -> Path:
    """全量归档也按角色分家：换角色 = 换一整个包，不会串味。"""
    d = character_dir() / "archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def character_dir(name: str | None = None) -> Path:
    name = name or str(cfg()["character"])
    d = ROOT / "data" / "persona" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    """每个角色一个库：切角色只改 config.character，记忆/人格/自我卡整包隔离。"""
    return character_dir() / "littlefish.db"
