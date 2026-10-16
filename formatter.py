"""把查询结果格式化为纯文本或文转图所需的模板数据。

本模块不依赖 AstrBot，可独立测试。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .mc_protocol import ServerStatus

TEMPLATE_PATH = Path(__file__).parent / "templates" / "status.html"


def _fmt_latency(latency_ms: float | None) -> str:
    """把延迟格式化为展示文本。"""
    if latency_ms is None:
        return "未知"
    if latency_ms < 1000:
        return f"{latency_ms:.0f} ms"
    return f"{latency_ms / 1000:.2f} s"


def _truncate(text: str, limit: int = 120) -> str:
    """截断过长文本并加上省略号。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_status_text(
    status: ServerStatus,
    *,
    show_player_list: bool = True,
    title: str = "",
) -> str:
    """把查询结果格式化为纯文本消息。

    Args:
        status: 查询结果。
        show_player_list: 是否展示在线玩家名单。
        title: 服务器名称；为空时使用地址。

    Returns:
        面向用户的多行文本。
    """
    lines = [f"【{title or status.address}】"]
    lines.append(f"地址：{status.address}")

    if not status.online:
        lines.append("状态：离线 / 无法连接")
        if status.error:
            lines.append(f"原因：{status.error}")
        return "\n".join(lines)

    lines.append("状态：在线")
    if status.legacy:
        lines.append("（该服务器仅支持旧版查询协议，信息可能不完整）")

    if status.version:
        lines.append(f"版本：{status.version}")
    if status.motd:
        lines.append(f"MOTD：{status.motd}")
    lines.append(f"玩家：{status.players_text}")
    lines.append(f"延迟：{_fmt_latency(status.latency_ms)}")

    if show_player_list and status.has_player_sample:
        names = "、".join(status.players.sample[:20])
        if len(status.players.sample) > 20:
            names += f" 等 {len(status.players.sample)} 人"
        lines.append(f"在线玩家：{names}")

    return "\n".join(lines)


def format_server_list(
    servers: list[tuple[str, str, bool]],
    *,
    session_label: str = "",
) -> str:
    """把服务器列表格式化为纯文本。

    Args:
        servers: [(名称, 地址, 是否默认), ...]。
        session_label: 可选的会话说明，例如「本群」。

    Returns:
        面向用户的多行文本。
    """
    if not servers:
        return (
            "本会话还没有添加任何服务器。\n"
            "使用 /mc add <名称> <IP/域名:端口> 添加一个服务器。"
        )

    label = session_label or "本会话"
    lines = [f"{label}共 {len(servers)} 个服务器："]
    for index, (name, address, is_default) in enumerate(servers, start=1):
        mark = "（默认）" if is_default else ""
        lines.append(f"{index}. {name}{mark} — {address}")
    lines.append("使用 /mc <名称> 查询指定服务器，或直接 /mc 查询默认服务器。")
    return "\n".join(lines)

def build_render_data(
    status: ServerStatus,
    *,
    title: str = "",
    show_player_list: bool = True,
    extra_servers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造 HTML 模板需要的渲染数据。

    Args:
        status: 查询结果。
        title: 服务器名称；为空时使用地址。
        show_player_list: 是否展示在线玩家名单。
        extra_servers: 附带的服务器列表（例如 /mc list 的图片输出）。

    Returns:
        可直接传给 Jinja2 模板的字典。
    """
    return {
        "title": title or status.address,
        "address": status.address,
        "online": status.online,
        "motd": _truncate(status.motd, 400),
        "version": status.version,
        "protocol": status.protocol,
        "players_online": status.players.online,
        "players_max": status.players.max,
        "player_sample": status.players.sample if show_player_list else [],
        "show_players": show_player_list,
        "latency_ms": (
            round(status.latency_ms) if status.latency_ms is not None else None
        ),
        "icon": status.favicon,
        "error": status.error or "",
        "legacy": status.legacy,
        "extra_servers": extra_servers or [],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_list_render_data(
    servers: list[tuple[str, str, bool]],
    *,
    session_label: str = "",
) -> dict[str, Any]:
    """构造服务器列表的渲染数据（复用状态卡片模板）。

    Args:
        servers: [(名称, 地址, 是否默认), ...]。
        session_label: 会话说明，例如「本群」。

    Returns:
        可直接传给模板的字典。

    Note:
        ``is_list`` 让渲染器知道这是列表而非服务器状态，从而不显示
        「在线」徽章与玩家统计等不适用于列表的内容。
    """
    return {
        "title": f"{session_label or '本会话'}服务器列表",
        "address": f"共 {len(servers)} 个服务器",
        "online": True,
        "is_list": True,
        "motd": "使用 /mc <名称> 查询指定服务器，或直接 /mc 查询默认服务器。",
        "version": "",
        "protocol": None,
        "players_online": 0,
        "players_max": 0,
        "player_sample": [],
        "show_players": False,
        "latency_ms": None,
        "icon": None,
        "error": "",
        "legacy": False,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "extra_servers": [
            {"name": name, "address": address, "default": is_default}
            for name, address, is_default in servers
        ],
    }


def load_template() -> str:
    """读取 HTML 模板内容。

    Raises:
        FileNotFoundError: 模板文件缺失。
    """
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def format_help() -> str:
    """返回帮助文本。"""
    return (
        "Minecraft 服务器状态查询\n"
        "· /mc —— 查询本群默认服务器\n"
        "· /mc <名称> —— 查询指定服务器\n"
        "· /mc add <名称> <IP/域名:端口> —— 添加服务器（端口留空默认 25565）\n"
        "· /mc delete <名称> —— 删除服务器\n"
        "· /mc set <名称> —— 设置为默认服务器\n"
        "· /mc list —— 列出本群添加的所有服务器\n"
        "· /mc help —— 显示本帮助\n"
        "每个群聊的服务器数据相互独立。"
    )
