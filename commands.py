"""``/mc`` 指令的解析逻辑。

本模块是纯函数实现，不依赖 AstrBot，可独立测试。

支持的指令形式::

    /mc                                  -> 查询默认服务器
    /mc <名称>                            -> 查询指定服务器
    /mc add <名称> <地址>                  -> 添加服务器
    /mc add <名称> <域名> <端口>           -> 添加服务器（端口单独给出）
    /mc delete <名称>                     -> 删除服务器
    /mc remove <名称>                     -> 删除服务器（别名）
    /mc set <名称>                        -> 设置默认服务器
    /mc list                              -> 列出所有服务器
    /mc ls                                -> 列出所有服务器（别名）
    /mc help                              -> 显示帮助
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION_QUERY_DEFAULT = "query_default"
ACTION_QUERY = "query"
ACTION_ADD = "add"
ACTION_DELETE = "delete"
ACTION_SET = "set"
ACTION_LIST = "list"
ACTION_HELP = "help"
ACTION_UNKNOWN = "unknown"

DELETE_ALIASES = frozenset({"delete", "del", "remove", "rm"})
LIST_ALIASES = frozenset({"list", "ls"})
HELP_ALIASES = frozenset({"help", "?", "-h", "--help"})


@dataclass
class ParsedCommand:
    """一条 ``/mc`` 指令的解析结果。

    Attributes:
        action: 动作类型，取值见 ``ACTION_*`` 常量。
        name: 服务器名称（add/delete/set/query 时可能非空）。
        address: 服务器地址（仅 add 时非空）。
        raw_args: 去除指令名后的原始参数文本。
    """

    action: str
    name: str = ""
    address: str = ""
    raw_args: str = ""


def split_args(text: str) -> list[str]:
    """按空白字符切分参数。"""
    return [part for part in (text or "").split() if part]


def normalize_address(host: str, port: str | None = None) -> str:
    """把主机与可选端口组合成规范地址字符串。

    - 端口为空时只返回主机（后续由协议层补默认 25565）。
    - IPv6 地址会加上方括号。

    Args:
        host: 主机名或 IP。
        port: 端口文本，可为空。

    Returns:
        组合后的地址字符串。
    """
    host = (host or "").strip()
    if not host:
        return ""

    port = (port or "").strip()
    if port and not port.isdigit():
        # 端口不是数字，说明这其实是地址的一部分，交给调用方处理。
        return f"{host} {port}".strip()

    needs_brackets = host.count(":") >= 2 and not host.startswith("[")
    if needs_brackets:
        host = f"[{host}]"

    return f"{host}:{port}" if port else host


def parse_mc_command(args: str) -> ParsedCommand:
    """解析 ``/mc`` 之后的参数文本。

    Args:
        args: 指令名之后的内容，例如 ``"add 主服 mc.example.com:25565"``。

    Returns:
        解析结果。无法识别时 ``action`` 为 ``ACTION_UNKNOWN``。
    """
    raw = (args or "").strip()
    parts = split_args(raw)

    if not parts:
        return ParsedCommand(action=ACTION_QUERY_DEFAULT, raw_args=raw)

    head = parts[0]
    head_lower = head.lower()

    if head_lower in HELP_ALIASES:
        return ParsedCommand(action=ACTION_HELP, raw_args=raw)

    if head_lower in LIST_ALIASES:
        return ParsedCommand(action=ACTION_LIST, raw_args=raw)

    if head_lower == "add":
        rest = parts[1:]
        if not rest:
            return ParsedCommand(
                action=ACTION_UNKNOWN,
                raw_args=raw,
            )
        if len(rest) == 1:
            # 只给了名称，缺少地址。
            return ParsedCommand(
                action=ACTION_ADD,
                name=rest[0],
                address="",
                raw_args=raw,
            )
        if len(rest) == 2:
            return ParsedCommand(
                action=ACTION_ADD,
                name=rest[0],
                address=normalize_address(rest[1]),
                raw_args=raw,
            )
        # 三个及以上：<名称> <主机> <端口>，或地址本身含空格（取第一个为名称，
        # 其余按「主机 + 端口」组合，多余的忽略）。
        return ParsedCommand(
            action=ACTION_ADD,
            name=rest[0],
            address=normalize_address(rest[1], rest[2]),
            raw_args=raw,
        )

    if head_lower in DELETE_ALIASES:
        rest = parts[1:]
        return ParsedCommand(
            action=ACTION_DELETE,
            name=rest[0] if rest else "",
            raw_args=raw,
        )

    if head_lower == "set":
        rest = parts[1:]
        return ParsedCommand(
            action=ACTION_SET,
            name=rest[0] if rest else "",
            raw_args=raw,
        )

    # 其余情况统一按「查询指定服务器」处理，允许名称中包含空格。
    return ParsedCommand(action=ACTION_QUERY, name=raw, raw_args=raw)
