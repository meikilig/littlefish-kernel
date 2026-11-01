"""二级开关的唯一真相源。

旧系统最大的病：一个开关写在配置里，代码里只在一处判断它，于是"关了还在跑"。
这个模块让四个地方读同一个值：

1. 界面      -> AstrBot 用插件的 `_conf_schema.json` 自动生成，不用我们写
2. 工具面    -> 插件注册工具前问 `caps.on(...)`，关了根本不注册
3. 提示词    -> 渲染行为说明前问 `caps.on(...)`，关了这句就不出现
4. 代码闸门  -> 真被调到时问 `caps.on(...)`，关了直接拒绝

读的顺序：配置文件优先（改了立刻生效，不用重启），其次插件实例里注入的字典。
这样"关掉 L1-1，QQ 立刻不说话；打开，恢复，中间不用重启"是物理成立的。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# AstrBot 的插件配置目录；可用环境变量覆盖（测试用）。
# 注意：这里每次现读环境变量 —— 以前是 import 时读一次，结果测试里设了
# LITTLEFISH_PLUGIN_CONFIG_DIR 也不生效，读的还是线上开关（踩过一次）。
def _default_config_dir() -> str:
    from .config import astrbot_dir
    return str(astrbot_dir() / "data" / "config")


def data_config_dir() -> str:
    return os.environ.get("LITTLEFISH_PLUGIN_CONFIG_DIR", _default_config_dir())


def __getattr__(name):  # 兼容老代码里 caps.DATA_CONFIG_DIR 的写法
    if name == "DATA_CONFIG_DIR":
        return data_config_dir()
    raise AttributeError(name)


_lock = threading.Lock()
_injected: dict[str, dict] = {}          # plugin -> 注入的配置字典
_file_cache: dict[str, tuple[float, dict]] = {}  # plugin -> (mtime, 配置)


def _candidates(plugin: str) -> list[str]:
    """找出这个插件可能对应的配置文件名。

    AstrBot 用「插件目录名_config.json」（如 `astrbot_littlefish_l1_ops_config.json`），
    我们代码里习惯写短名（`l1_ops`）。这里踩过一次真坑：只认短名时，
    面板上把开关关掉，代码里读到的还是旧默认值 —— 就是旧系统那种"关了还在跑"。
    所以按"完全同名 → 以 `_<短名>_config.json` 结尾"的顺序找。
    """
    exact = os.path.join(data_config_dir(), f"{plugin}_config.json")
    out = [exact]
    suffix = f"_{plugin}_config.json"
    try:
        for name in sorted(os.listdir(data_config_dir())):
            if name.endswith(suffix):
                out.append(os.path.join(data_config_dir(), name))
    except OSError:
        pass
    return out


def config_path(plugin: str) -> str:
    for path in _candidates(plugin):
        if os.path.exists(path):
            return path
    return _candidates(plugin)[0]


def install(plugin: str, config: dict | None) -> None:
    """插件实例化时把 AstrBot 注入的配置交进来，作为文件不存在时的兜底。"""
    with _lock:
        _injected[plugin] = dict(config or {})


def _load(plugin: str) -> dict:
    """文件优先，带 mtime 缓存；文件坏了不让整个机器人挂掉。"""
    path = config_path(plugin)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        with _lock:
            return dict(_injected.get(plugin) or {})

    with _lock:
        cached = _file_cache.get(plugin)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        with _lock:
            return dict(_injected.get(plugin) or {})

    with _lock:
        _file_cache[plugin] = (mtime, data)
    return data


def raw(plugin: str, path: str, default: Any = None) -> Any:
    """读任意一个配置项，`path` 用点号表示层级，如 `tools.sticker`。"""
    node: Any = _load(plugin)
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def on(plugin: str, path: str, default: bool = True) -> bool:
    """开关是否打开。非 bool 值按 Python 真值处理，None 用 default。"""
    value = raw(plugin, path, None)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(value)


def enabled_items(plugin: str, group: str = "") -> list[str]:
    """列出一个分组下所有打开的开关名（给提示词渲染和工具面用）。"""
    node = raw(plugin, group, {}) if group else _load(plugin)
    if not isinstance(node, dict):
        return []
    return [k for k, v in node.items() if bool(v)]


def snapshot() -> dict:
    """诊断用：当前所有已安装插件的开关原文。"""
    with _lock:
        plugins = sorted(set(_injected) | {
            f[:-12] for f in os.listdir(data_config_dir())
            if f.endswith("_config.json")
        } if os.path.isdir(data_config_dir()) else set(_injected))
    return {p: _load(p) for p in plugins}


def invalidate(plugin: str | None = None) -> None:
    """测试用：丢掉缓存，强制重读。"""
    with _lock:
        if plugin:
            _file_cache.pop(plugin, None)
        else:
            _file_cache.clear()


def set_path(data: dict, path: str, value: Any) -> dict:
    """按点号路径写一份嵌套 dict，返回被改的那份（原地改）。"""
    parts = [p for p in path.split(".") if p]
    if not parts:
        return data
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
    return data


def write(plugin: str, path: str, value: Any, *, keep_enabled: bool = True) -> bool:
    """写一个开关到磁盘（原子替换），并让缓存失效。返回是否写成功。

    写的是 AstrBot 真正在用的那份 `<插件目录名>_config.json`，
    所以改完立刻生效，不用重启。
    """
    import json
    import pathlib
    import tempfile

    try:
        p = pathlib.Path(config_path(plugin))
        data: dict = {}
        if p.exists():
            with open(p, encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        set_path(data, path, value)
        if keep_enabled:
            data.setdefault("enabled", True)

        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".caps-", suffix=".json")
        os.close(fd)
        tmp = pathlib.Path(tmp_name)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, p)
        invalidate(plugin)
        return True
    except Exception:  # noqa: BLE001
        return False


def _age(plugin: str) -> float:
    try:
        return time.time() - os.path.getmtime(config_path(plugin))
    except OSError:
        return -1.0
