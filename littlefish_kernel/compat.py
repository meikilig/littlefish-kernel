"""跟 AstrBot 的边界：我们把"依赖了它什么"写成一份可执行的清单。

为什么要有这个文件
------------------
内核是我们写的，宿主是别人的。AstrBot 哪天升到 5.0，改了插件基类、事件对象、
消息链的构造方式，我们这些插件会一起坏 —— 而且很可能**坏得很难看**：
插件被静默跳过、或者能加载但每条消息都报错。

所以这里做三件事：

1. `probe()`  —— 启动时把"我们依赖的每一样东西"实际摸一遍，缺了就记下来。
2. `VERIFIED` —— 我们真正验收过的版本。版本不在这一档就亮黄灯。
3. `report()` —— 一份人话报告，管理台顶上那条横幅就是它。

这不是"隔离"，隔离是适配层（`lf_host`）的活。这是**体检 + 早报警**：
升级前跑一次，十秒内知道哪条契约破了，而不是上线后发现她不说话了。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

# 我们在这里验收过的宿主版本（改这个之前先把全量回归跑绿）
VERIFIED = ("4.29.3",)
VERIFIED_MAJOR = 4

# 我们依赖的每一样东西，以及"为什么离不开它"
CONTRACT = [
    ("插件基类 Star(context, config)", "每个插件都继承它；config 是二级开关的来源"),
    ("Context.send_message", "她主动开口（早安、拆条补发）全靠它"),
    ("Context.activate/deactivate_llm_tool", "开关关掉时要把工具从模型列表里真摘掉"),
    ("filter.command / command_group", "十几条指令：/fish /qq /lang /bili /cost /ops /book"),
    ("filter.llm_tool", "26 个模型工具，靠它注册"),
    ("filter.event_message_type / on_astrbot_loaded", "入站消息与启动钩子"),
    ("事件：stop_event/is_stopped", "闸门：拔掉一级插件=没听见，也不留记忆"),
    ("事件：get_sender_id/get_group_id/unified_msg_origin", "判主人、判群、定位会话"),
    ("事件：plain_result/chain_result", "回复的两种返回形态"),
    ("消息段 Plain/Image/Record/Reply", "引用、拆条、语音、图"),
    ("TextPart.mark_as_temp", "每轮的动态上下文走 extra_user_content_parts，不碰 system_prompt"),
    ("LLMResponse.raw_completion", "记账要拿真实 model 名"),
]


def _has(obj: Any, *names: str) -> list[str]:
    return [n for n in names if not hasattr(obj, n)]


def probe() -> dict[str, Any]:
    """把契约实际摸一遍。返回 {version, major, known, ok, missing, notes}。"""
    out: dict[str, Any] = {
        "version": "", "major": 0, "known": False,
        "ok": False, "missing": [], "notes": [],
    }
    try:
        import astrbot

        ver = str(getattr(astrbot, "__version__", "") or "")
        out["version"] = ver
        out["major"] = int(ver.split(".")[0]) if ver.split(".")[0].isdigit() else 0
        out["known"] = ver in VERIFIED or out["major"] == VERIFIED_MAJOR
    except Exception as exc:  # 连 astrbot 都 import 不了：说明不在宿主里跑
        out["notes"].append(f"import astrbot 失败：{exc}")
        return out

    try:
        from astrbot.api import message_components as mc
        from astrbot.api.event import AstrMessageEvent, filter
        from astrbot.api.star import Context, Star
        from astrbot.core.agent.message import TextPart
    except Exception as exc:
        out["missing"].append(f"导入插件 API 失败：{exc}")
        return out

    # 1) 基类签名：4.29 起是 (context, config)，早期只有 (context)
    try:
        params = list(inspect.signature(Star.__init__).parameters)
        if "config" not in params:
            out["missing"].append("Star.__init__ 不再接受 config（二级开关拿不到）")
    except Exception as exc:
        out["missing"].append(f"读 Star.__init__ 签名失败：{exc}")

    # 2) Context 的方法
    miss = _has(Context, "send_message", "activate_llm_tool", "deactivate_llm_tool")
    out["missing"] += [f"Context.{m} 没了" for m in miss]

    # 3) 装饰器
    miss = _has(filter, "command", "command_group", "llm_tool", "event_message_type", "on_astrbot_loaded")
    out["missing"] += [f"filter.{m} 没了" for m in miss]

    # 4) 事件方法
    miss = _has(AstrMessageEvent, "stop_event", "is_stopped", "get_sender_id",
                "get_group_id", "get_platform_name", "plain_result", "chain_result")
    out["missing"] += [f"AstrMessageEvent.{m} 没了" for m in miss]

    # 5) 消息段
    miss = _has(mc, "Plain", "Image", "Record", "Reply")
    out["missing"] += [f"message_components.{m} 没了" for m in miss]

    # 6) 动态上下文
    if not hasattr(TextPart, "mark_as_temp"):
        out["missing"].append("TextPart.mark_as_temp 没了（动态上下文会被塞进 prompt，缓存失效）")

    # 7) 记账字段
    try:
        from astrbot.core.provider.entities import LLMResponse

        if not hasattr(LLMResponse, "raw_completion"):
            out["notes"].append("LLMResponse.raw_completion 没了：记账拿不到 model 名（只影响账单标签）")
    except Exception as exc:
        out["notes"].append(f"探测 LLMResponse 失败：{exc}")

    # 8) 插件配置文件命名（caps 依赖）
    try:
        from littlefish_kernel import caps

        cfg = caps.data_config_dir()
        if not Path(cfg).is_dir():
            out["notes"].append(f"插件配置目录还不存在：{cfg}（第一次启动正常）")
    except Exception:
        pass

    out["ok"] = not out["missing"]
    return out


_CACHE: dict[str, Any] | None = None


def cached() -> dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        _CACHE = probe()
    return _CACHE


def reset() -> None:
    global _CACHE
    _CACHE = None


def report() -> str:
    """给人看的一句话结论。"""
    r = cached()
    if not r["version"]:
        return "宿主探测失败：" + ("；".join(r["notes"]) or "未知原因")
    if r["missing"]:
        head = f"AstrBot {r['version']} 与我们验收过的 {'/'.join(VERIFIED)} 不一致，缺了 {len(r['missing'])} 样东西"
        return head + "：" + "；".join(r["missing"][:4])
    if not r["known"]:
        return (f"AstrBot {r['version']} 没在我们的验收名单里（{'/'.join(VERIFIED)}），"
                f"但 {len(CONTRACT)} 条契约全在 —— 能跑，风险自负")
    return f"宿主 AstrBot {r['version']}，{len(CONTRACT)} 条契约全在"


def save(root: Path) -> Path:
    """落盘一份，升级前后可以 diff。"""
    p = Path(root) / "data" / "compat.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"probe": cached(), "contract": CONTRACT,
                             "verified": list(VERIFIED)},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p
