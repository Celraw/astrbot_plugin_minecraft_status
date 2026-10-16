"""群聊隔离的服务器数据存储。

本模块是纯逻辑实现：所有持久化都通过注入的 ``loader`` / ``saver`` 回调完成，
不直接依赖 AstrBot，因此可以单独进行单元测试。

数据模型（JSON 可序列化）::

    {
        "groups": {
            "group:123456": {
                "servers": {"主服": "mc.example.com:25565", "副服": "play.example.com"},
                "default": "主服"
            },
            "private:7890": {
                "servers": {}, "default": null
            }
        }
    }
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

MAX_NAME_LENGTH = 32
"""服务器名称的最大长度。"""

_NAME_PATTERN = re.compile(r"^[^\s/\\]+$")
"""名称不允许包含空白字符与路径分隔符。"""

RESERVED_NAMES = frozenset({"add", "delete", "set", "list", "help"})
"""保留子指令名，不能被用作服务器名称（避免歧义）。"""


@dataclass
class SessionData:
    """单个会话（群聊/私聊）的服务器数据。"""

    servers: dict[str, str] = field(default_factory=dict)
    """服务器名称 -> 地址。"""

    default: str | None = None
    """默认服务器名称；未设置时为 None。"""


class StoreError(Exception):
    """存储层可预期的错误（会直接提示给用户）。"""


class ServerStore:
    """管理所有会话的服务器数据，保证群聊之间相互隔离。

    Args:
        loader: 异步加载函数，返回完整数据字典；无数据时返回 None。
        saver: 异步保存函数，接收完整数据字典。
    """

    def __init__(
        self,
        loader: Callable[[], Awaitable[Any]] | None = None,
        saver: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._loader = loader
        self._saver = saver
        self._lock = asyncio.Lock()
        self._data: dict[str, SessionData] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """从持久化后端加载数据（只执行一次）。"""
        async with self._lock:
            if self._loaded:
                return
            raw: Any = None
            if self._loader is not None:
                try:
                    raw = await self._loader()
                except Exception:
                    raw = None
            self._data = self._decode(raw)
            self._loaded = True

    async def _flush(self) -> None:
        """把当前数据写回持久化后端。"""
        if self._saver is None:
            return
        await self._saver(self._encode())

    @staticmethod
    def _decode(raw: Any) -> dict[str, SessionData]:
        """把持久化的原始数据解码为内部结构（容忍脏数据）。"""
        result: dict[str, SessionData] = {}
        if not isinstance(raw, dict):
            return result
        groups = raw.get("groups")
        if not isinstance(groups, dict):
            return result

        for key, value in groups.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            servers_raw = value.get("servers")
            servers: dict[str, str] = {}
            if isinstance(servers_raw, dict):
                for name, address in servers_raw.items():
                    if isinstance(name, str) and isinstance(address, str):
                        servers[name] = address
            default = value.get("default")
            if not isinstance(default, str) or default not in servers:
                default = None
            result[key] = SessionData(servers=servers, default=default)
        return result

    def _encode(self) -> dict[str, Any]:
        """把内部结构编码为 JSON 可序列化数据。"""
        return {
            "groups": {
                key: {
                    "servers": dict(session.servers),
                    "default": session.default,
                }
                for key, session in self._data.items()
            },
        }

    # ------------------------------------------------------------------
    # 会话数据
    # ------------------------------------------------------------------
    def _session(self, session_key: str) -> SessionData:
        """获取（必要时创建）指定会话的数据。"""
        session = self._data.get(session_key)
        if session is None:
            session = SessionData()
            self._data[session_key] = session
        return session

    def get_session(self, session_key: str) -> SessionData:
        """只读地获取会话数据（不存在时返回空对象，不会创建）。"""
        return self._data.get(session_key, SessionData())

    # ------------------------------------------------------------------
    # 业务操作
    # ------------------------------------------------------------------
    async def add_server(
        self,
        session_key: str,
        name: str,
        address: str,
        *,
        max_servers: int = 10,
    ) -> str:
        """添加一个服务器。

        Args:
            session_key: 会话标识。
            name: 服务器名称。
            address: 服务器地址。
            max_servers: 当前会话允许的最大服务器数量。

        Returns:
            实际保存的名称（可能与传入的大小写不同）。

        Raises:
            StoreError: 名称非法、地址为空、重名或超出数量上限。
        """
        await self.load()
        name = (name or "").strip()
        address = (address or "").strip()

        error = validate_server_name(name)
        if error:
            raise StoreError(error)
        if not address:
            raise StoreError("缺少服务器地址。用法：/mc add <名称> <IP/域名:端口>")

        async with self._lock:
            session = self._session(session_key)
            existing = find_key(session.servers, name)
            if existing is not None:
                if session.servers[existing] == address:
                    raise StoreError(f"服务器「{existing}」已存在（地址相同）。")
                session.servers[existing] = address
                # 若被修改的是默认服务器，无需改动 default（名称未变）。
                await self._flush()
                return existing

            if len(session.servers) >= max_servers:
                raise StoreError(
                    f"本会话最多只能添加 {max_servers} 个服务器，请先删除一些。",
                )

            session.servers[name] = address
            if session.default is None:
                # 第一个添加的服务器自动成为默认服务器，方便用户直接使用 /mc。
                session.default = name
            await self._flush()
            return name

    async def delete_server(self, session_key: str, name: str) -> str:
        """删除一个服务器。

        Returns:
            被删除的名称。

        Raises:
            StoreError: 名称非法或不存在。
        """
        await self.load()
        name = (name or "").strip()
        if not name:
            raise StoreError("请指定要删除的服务器名称。用法：/mc delete <名称>")

        async with self._lock:
            session = self._data.get(session_key)
            if session is None or not session.servers:
                raise StoreError("本会话还没有添加任何服务器。")

            actual = find_key(session.servers, name)
            if actual is None:
                raise StoreError(f"未找到服务器「{name}」。")

            del session.servers[actual]
            if session.default == actual:
                # 默认服务器被删除时，自动顺延到剩余的第一个服务器。
                session.default = next(iter(session.servers), None)
            await self._flush()
            return actual

    async def set_default(self, session_key: str, name: str) -> str:
        """设置默认服务器。

        Returns:
            被设为默认的名称。

        Raises:
            StoreError: 名称非法或不存在。
        """
        await self.load()
        name = (name or "").strip()
        if not name:
            raise StoreError("请指定服务器名称。用法：/mc set <名称>")

        async with self._lock:
            session = self._data.get(session_key)
            if session is None or not session.servers:
                raise StoreError("本会话还没有添加任何服务器，请先用 /mc add 添加。")

            actual = find_key(session.servers, name)
            if actual is None:
                raise StoreError(f"未找到服务器「{name}」。")

            session.default = actual
            await self._flush()
            return actual

    async def list_servers(self, session_key: str) -> list[tuple[str, str, bool]]:
        """列出本会话的所有服务器。

        Returns:
            [(名称, 地址, 是否为默认), ...]，按添加顺序排列。
        """
        await self.load()
        session = self.get_session(session_key)
        return [
            (name, address, name == session.default)
            for name, address in session.servers.items()
        ]

    async def get_default(self, session_key: str) -> tuple[str, str] | None:
        """获取默认服务器的 (名称, 地址)；未设置时返回 None。"""
        await self.load()
        session = self.get_session(session_key)
        if session.default and session.default in session.servers:
            return session.default, session.servers[session.default]
        return None

    async def get_server(self, session_key: str, name: str) -> tuple[str, str] | None:
        """按名称获取服务器 (名称, 地址)；不存在时返回 None。"""
        await self.load()
        session = self.get_session(session_key)
        actual = find_key(session.servers, name)
        if actual is None:
            return None
        return actual, session.servers[actual]


def find_key(mapping: dict[str, str], name: str) -> str | None:
    """在映射中查找名称，先精确匹配再忽略大小写匹配。"""
    if name in mapping:
        return name
    lowered = name.lower()
    for key in mapping:
        if key.lower() == lowered:
            return key
    return None


def validate_server_name(name: str) -> str | None:
    """校验服务器名称，合法时返回 None，否则返回错误提示。"""
    if not name:
        return "服务器名称不能为空。"
    if len(name) > MAX_NAME_LENGTH:
        return f"服务器名称过长（最多 {MAX_NAME_LENGTH} 个字符）。"
    if not _NAME_PATTERN.match(name):
        return "服务器名称不能包含空格、斜杠或反斜杠。"
    if name.lower() in RESERVED_NAMES:
        return f"「{name}」是保留指令名，请换一个名称。"
    return None


def build_session_key(group_id: str | None, session_id: str | None) -> str:
    """构造会话标识，确保不同群聊之间的数据彼此隔离。

    Args:
        group_id: 群号；群聊消息时非空。
        session_id: 会话 ID（私聊时为对方用户 ID）。

    Returns:
        形如 ``group:123456`` 或 ``private:7890`` 的字符串。
    """
    if group_id and str(group_id).strip():
        return f"group:{str(group_id).strip()}"
    return f"private:{str(session_id or '').strip()}"
