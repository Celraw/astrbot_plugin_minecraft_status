"""Minecraft Java 版服务器状态查询（Server List Ping 协议实现）。

本模块只依赖 Python 标准库，不导入任何 AstrBot 内部模块，因此可以脱离
AstrBot 环境独立测试。

协议参考：https://minecraft.wiki/w/Java_Edition_protocol/Server_List_Ping
- 现代协议（1.7+）：Handshake(0x00) -> Status Request(0x00) -> Status Response(0x00) -> Ping(0x01) -> Pong(0x01)
- 旧版协议（<=1.6）：直接发送 0xFE 0x01，服务器返回 0xFF 踢出包
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any

DEFAULT_PORT = 25565
"""Minecraft Java 版服务器默认端口。"""

STATUS_PROTOCOL_VERSION = -1
"""握手包中的协议版本，约定 -1 表示仅用于查询状态。"""

_MAX_PACKET_LENGTH = 2 * 1024 * 1024
"""单个数据包长度上限，避免恶意服务器导致内存爆炸。"""

_MAX_VARINT_BYTES = 5
"""VarInt 最多 5 个字节。"""

_COLOR_CODE_PATTERN = re.compile("\u00a7.")

_SRV_PREFIX = "_minecraft._tcp."


class MCPingError(Exception):
    """查询 Minecraft 服务器时的基础异常。

    Attributes:
        reason: 面向用户的中文原因描述。
    """

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class MCTimeoutError(MCPingError):
    """连接或读取超时。"""


class MCConnectionError(MCPingError):
    """无法建立连接（域名解析失败、端口未开放等）。"""


class MCProtocolError(MCPingError):
    """返回的数据不符合 Server List Ping 协议。"""


@dataclass
class PlayerInfo:
    """玩家数量信息。"""

    online: int = 0
    max: int = 0
    sample: list[str] = field(default_factory=list)


@dataclass
class ServerStatus:
    """一次查询的完整结果。"""

    address: str
    """用户输入的原始地址（用于展示）。"""

    host: str
    """实际连接使用的主机名。"""

    port: int
    """实际连接使用的端口。"""

    online: bool
    """服务器是否响应了查询。"""

    latency_ms: float | None = None
    """往返延迟（毫秒）。"""

    version: str = ""
    """服务器版本名称，例如 1.21.1。"""

    protocol: int | None = None
    """服务器协议号。"""

    motd: str = ""
    """清理过颜色代码的 MOTD。"""

    motd_raw: Any = None
    """服务器返回的原始 description 字段。"""

    players: PlayerInfo = field(default_factory=PlayerInfo)

    favicon: str | None = None
    """服务器图标，形如 data:image/png;base64,xxx。"""

    legacy: bool = False
    """是否通过旧版（<=1.6）协议获得的结果。"""

    error: str | None = None
    """查询失败时的原因描述。"""

    @property
    def players_text(self) -> str:
        """玩家数量的展示文本。"""
        return f"{self.players.online}/{self.players.max}"

    @property
    def has_player_sample(self) -> bool:
        """是否存在可展示的在线玩家名单。"""
        return bool(self.players.sample)


def encode_varint(value: int) -> bytes:
    """将一个整数编码为 Minecraft 的 VarInt。"""
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(
    data: bytes,
    offset: int = 0,
    *,
    signed: bool = False,
) -> tuple[int, int]:
    """从字节串中解码一个 VarInt。

    Args:
        data: 原始字节串。
        offset: 起始偏移。
        signed: 是否按有符号 32 位整数解释（Minecraft 协议中 VarInt 是有符号的，
            握手包里的协议版本 -1 就依赖这一点）。默认无符号，适合长度前缀等场景。

    Returns:
        (值, 新的偏移)。

    Raises:
        MCProtocolError: 数据不足或 VarInt 过长。
    """
    result = 0
    for num_read in range(_MAX_VARINT_BYTES):
        if offset >= len(data):
            raise MCProtocolError("返回数据不完整（VarInt 被截断）。")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << (7 * num_read)
        if not byte & 0x80:
            if signed and result >= 1 << 31:
                result -= 1 << 32
            return result, offset
    raise MCProtocolError("VarInt 长度超过 5 个字节。")


def pack_string(text: str) -> bytes:
    """将字符串编码为 Minecraft 的 String（VarInt 长度 + UTF-8 数据）。"""
    encoded = text.encode("utf-8")
    return encode_varint(len(encoded)) + encoded


def pack_packet(packet_id: int, payload: bytes = b"") -> bytes:
    """封装一个完整的 Minecraft 数据包（长度 + 包 ID + 负载）。"""
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


def unpack_packet(data: bytes) -> tuple[int, bytes]:
    """解析一个**包体**（不含长度前缀），返回 (包 ID, 负载)。

    Note:
        ``data`` 必须是已经去掉长度前缀的包体。若要解析完整的线上数据包
        （含长度前缀），请使用 :func:`read_packet`。

    Raises:
        MCProtocolError: 数据不完整。
    """
    packet_id, offset = decode_varint(data)
    return packet_id, data[offset:]


def read_packet(data: bytes) -> tuple[int, bytes]:
    """解析一个完整的线上数据包（含长度前缀）。

    Args:
        data: ``pack_packet`` 产出的完整字节串。

    Returns:
        (包 ID, 负载)。

    Raises:
        MCProtocolError: 长度前缀或包体不合法。
    """
    length, offset = decode_varint(data)
    body = data[offset : offset + length]
    if len(body) < length:
        raise MCProtocolError("数据包长度前缀与实际内容不符。")
    return unpack_packet(body)


def clean_motd(description: Any) -> str:
    """把服务器返回的 description 字段转换为纯文本 MOTD。

    支持字符串、{"text": ..., "extra": [...]} 以及列表等富文本结构，
    并会去除 Minecraft 的 `§` 颜色/格式代码。

    Args:
        description: 服务器返回的原始 description 字段。

    Returns:
        清理后的纯文本 MOTD。
    """
    return _COLOR_CODE_PATTERN.sub("", _flatten_text(description)).strip()


def _flatten_text(component: Any) -> str:
    """递归展开 Minecraft 的文本组件。"""
    if component is None:
        return ""
    if isinstance(component, str):
        return component
    if isinstance(component, bool):
        return ""
    if isinstance(component, int | float):
        return str(component)
    if isinstance(component, list):
        return "".join(_flatten_text(item) for item in component)
    if isinstance(component, dict):
        parts: list[str] = []
        text = component.get("text")
        if isinstance(text, str):
            parts.append(text)
        elif text is not None:
            parts.append(_flatten_text(text))
        translate = component.get("translate")
        if isinstance(translate, str):
            parts.append(translate)
        extra = component.get("extra")
        if extra is not None:
            parts.append(_flatten_text(extra))
        return "".join(parts)
    return str(component)


def parse_address(address: str, default_port: int = DEFAULT_PORT) -> tuple[str, int, bool]:
    """解析用户输入的服务器地址。

    支持的写法：
        - ``mc.example.com``            -> ("mc.example.com", 25565, False)
        - ``mc.example.com:30411``      -> ("mc.example.com", 30411, True)
        - ``[2001:db8::1]:25566``       -> ("2001:db8::1", 25566, True)
        - ``[2001:db8::1]``             -> ("2001:db8::1", 25565, False)

    Args:
        address: 用户输入的地址。
        default_port: 未指定端口时使用的默认端口。

    Returns:
        (主机名, 端口, 是否显式指定了端口)。
    """
    addr = (address or "").strip()
    if not addr:
        return "", default_port, False

    if addr.startswith("["):
        end = addr.find("]")
        if end != -1:
            host = addr[1:end]
            rest = addr[end + 1 :]
            if rest.startswith(":") and rest[1:].isdigit():
                return host, int(rest[1:]), True
            return host, default_port, False

    # 只有一个冒号时，才把冒号后面当作端口（IPv6 地址会有多个冒号）。
    if addr.count(":") == 1:
        host, _, port_text = addr.partition(":")
        if port_text.isdigit():
            return host, int(port_text), True

    return addr, default_port, False


def is_valid_address(address: str) -> bool:
    """判断用户输入的地址是否合法。"""
    host, port, _ = parse_address(address)
    if not host:
        return False
    if not 0 < port < 65536:
        return False
    if any(ch.isspace() for ch in host):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._\-:]+", host))


async def resolve_srv(host: str) -> tuple[str, int] | None:
    """查询 ``_minecraft._tcp.<host>`` 的 SRV 记录。

    需要安装 dnspython；未安装或查询失败时返回 None（由调用方回退到直连）。

    Args:
        host: 域名。

    Returns:
        (目标主机, 端口)，未找到记录时返回 None。
    """
    try:
        import dns.asyncresolver  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        answers = await dns.asyncresolver.resolve(_SRV_PREFIX + host, "SRV")
    except Exception:
        return None

    for answer in answers:
        target = str(getattr(answer, "target", "")).rstrip(".")
        port = int(getattr(answer, "port", 0))
        if target and port:
            return target, port
    return None


async def _read_varint_from_stream(reader: asyncio.StreamReader) -> int:
    """从流中读取一个 VarInt。"""
    result = 0
    for num_read in range(_MAX_VARINT_BYTES):
        chunk = await reader.readexactly(1)
        byte = chunk[0]
        result |= (byte & 0x7F) << (7 * num_read)
        if not byte & 0x80:
            return result
    raise MCProtocolError("VarInt 长度超过 5 个字节。")


async def _read_packet_from_stream(reader: asyncio.StreamReader) -> bytes:
    """从流中读取一个完整的数据包（返回未解析的包体）。"""
    length = await _read_varint_from_stream(reader)
    if length <= 0 or length > _MAX_PACKET_LENGTH:
        raise MCProtocolError(f"数据包长度非法：{length}。")
    return await reader.readexactly(length)


def _build_handshake(host: str, port: int) -> bytes:
    """构造 Handshake 包（next state = 1，即 status）。"""
    payload = (
        encode_varint(STATUS_PROTOCOL_VERSION)
        + pack_string(host)
        + struct.pack(">H", port)
        + encode_varint(1)
    )
    return pack_packet(0x00, payload)


def _build_status_request() -> bytes:
    """构造 Status Request 包。"""
    return pack_packet(0x00)


def _build_ping_request() -> tuple[bytes, int]:
    """构造 Ping 包，返回 (字节数据, 载荷值)。"""
    payload = int(time.time() * 1000) & 0x7FFFFFFFFFFFFFFF
    return pack_packet(0x01, struct.pack(">q", payload)), payload


def _parse_status_response(payload: bytes) -> dict[str, Any]:
    """解析 Status Response 包体，返回 JSON 对象。"""
    length, offset = decode_varint(payload)
    raw = payload[offset : offset + length]
    if len(raw) < length:
        raise MCProtocolError("Status Response 字符串长度不足。")
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise MCProtocolError(f"Status Response 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise MCProtocolError("Status Response 不是一个 JSON 对象。")
    return data


def _extract_player_info(data: dict[str, Any]) -> PlayerInfo:
    """从 JSON 响应中提取玩家信息。"""
    players = data.get("players")
    if not isinstance(players, dict):
        return PlayerInfo()

    def _as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    sample: list[str] = []
    raw_sample = players.get("sample")
    if isinstance(raw_sample, list):
        for item in raw_sample:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    sample.append(clean_motd(name))
            elif isinstance(item, str) and item:
                sample.append(clean_motd(item))

    return PlayerInfo(
        online=_as_int(players.get("online", 0)),
        max=_as_int(players.get("max", 0)),
        sample=sample,
    )


def _extract_version(data: dict[str, Any]) -> tuple[str, int | None]:
    """从 JSON 响应中提取版本信息。"""
    version = data.get("version")
    if not isinstance(version, dict):
        return "", None
    name = version.get("name")
    protocol = version.get("protocol")
    try:
        protocol_int: int | None = int(protocol)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        protocol_int = None
    return (name if isinstance(name, str) else ""), protocol_int


async def _query_modern(
    host: str,
    port: int,
    timeout: float,
) -> tuple[dict[str, Any], float]:
    """执行现代（1.7+）Server List Ping 查询。

    Returns:
        (JSON 响应, 往返延迟毫秒)。

    Raises:
        MCPingError: 查询失败。
    """
    try:
        connection = asyncio.open_connection(host, port)
        try:
            reader, writer = await asyncio.wait_for(connection, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MCTimeoutError(
                f"连接 {host}:{port} 超时（{timeout:g} 秒）。",
                detail="服务器可能未启动、端口未放行或被防火墙拦截。",
            ) from exc
    except MCPingError:
        raise
    except OSError as exc:
        raise MCConnectionError(
            f"无法连接 {host}:{port}。",
            detail=_describe_os_error(exc),
        ) from exc
    except (UnicodeError, ValueError) as exc:
        # 主机名不符合 IDNA 规范（例如标签过长、含非法字符）时，
        # asyncio 会在 DNS 之前抛出 UnicodeError/ValueError。
        raise MCConnectionError(
            f"无法连接 {host}:{port}。",
            detail=f"主机名不合法或无法解析：{exc}",
        ) from exc

    started = time.perf_counter()
    try:
        ping_packet, _ = _build_ping_request()
        # 注意：Notchian 服务器在收到 Ping 请求前可能不会返回状态响应，
        # 因此这里把三个包一次性发出，再依次读取响应。
        writer.write(_build_handshake(host, port))
        writer.write(_build_status_request())
        writer.write(ping_packet)
        await writer.drain()

        data: dict[str, Any] | None = None
        latency_ms: float | None = None

        for _ in range(3):
            try:
                raw_packet = await asyncio.wait_for(
                    _read_packet_from_stream(reader),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                if data is not None:
                    break
                raise MCTimeoutError(
                    f"{host}:{port} 未在 {timeout:g} 秒内返回状态。",
                    detail="服务器可能启用了 enable-status=false 或正在启动。",
                ) from exc
            except (asyncio.IncompleteReadError, ConnectionError) as exc:
                if data is not None:
                    break
                raise MCConnectionError(
                    f"{host}:{port} 在返回数据前关闭了连接。",
                    detail="该端口可能不是 Minecraft Java 版服务器。",
                ) from exc

            packet_id, payload = unpack_packet(raw_packet)
            if packet_id == 0x00:
                data = _parse_status_response(payload)
                latency_ms = (time.perf_counter() - started) * 1000
            elif packet_id == 0x01:
                if latency_ms is None:
                    latency_ms = (time.perf_counter() - started) * 1000
                if data is not None:
                    break
            else:
                raise MCProtocolError(f"收到未知的数据包 ID：0x{packet_id:02X}。")

        if data is None:
            if latency_ms is None:
                latency_ms = (time.perf_counter() - started) * 1000
            raise MCProtocolError("服务器未返回状态响应。")

        return data, latency_ms
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _query_legacy(host: str, port: int, timeout: float) -> ServerStatus:
    """使用旧版（<=1.6）协议查询，返回无法提供延迟与图标的降级结果。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise MCTimeoutError(f"连接 {host}:{port} 超时。") from exc
    except OSError as exc:
        raise MCConnectionError(
            f"无法连接 {host}:{port}。",
            detail=_describe_os_error(exc),
        ) from exc
    except (UnicodeError, ValueError) as exc:
        raise MCConnectionError(
            f"无法连接 {host}:{port}。",
            detail=f"主机名不合法或无法解析：{exc}",
        ) from exc

    started = time.perf_counter()
    try:
        # 0xFE 0x01：旧版服务器列表查询。
        writer.write(b"\xfe\x01")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MCTimeoutError(f"{host}:{port} 未返回旧版协议响应。") from exc
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise MCConnectionError(f"{host}:{port} 在返回数据前关闭了连接。") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    latency_ms = (time.perf_counter() - started) * 1000
    if not raw or raw[0] != 0xFF:
        raise MCProtocolError("旧版协议响应格式不正确。")

    try:
        # 0xFF + 2 字节长度 + UTF-16BE 字符串
        text = raw[3:].decode("utf-16-be", errors="replace")
    except Exception as exc:
        raise MCProtocolError(f"旧版协议响应无法解码：{exc}") from exc

    text = text.lstrip("\ufeff")
    segments = text.split("\x00")
    # 结构：§1\x00<协议号>\x00<版本>\x00<MOTD>\x00<在线>\x00<最大人数>
    segments = [seg for seg in segments if seg != ""]
    if segments and segments[0].startswith("\u00a71"):
        segments = segments[1:]
    if len(segments) < 5:
        raise MCProtocolError("旧版协议响应字段不足。")

    protocol_text, version, motd, online_text, max_text = segments[:5]

    def _as_int(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return ServerStatus(
        address=f"{host}:{port}",
        host=host,
        port=port,
        online=True,
        latency_ms=latency_ms,
        version=version,
        protocol=_as_int(protocol_text) or None,
        motd=clean_motd(motd),
        motd_raw=motd,
        players=PlayerInfo(online=_as_int(online_text), max=_as_int(max_text)),
        legacy=True,
    )


def _describe_os_error(exc: OSError) -> str:
    """把底层网络异常翻译成中文说明。"""
    import errno
    import socket

    # DNS 解析失败：主机名不存在或拼写错误。
    if isinstance(exc, socket.gaierror) or getattr(exc, "errno", None) in (
        getattr(socket, "EAI_NONAME", None),
        getattr(socket, "EAI_FAIL", None),
    ):
        return "域名解析失败，请检查地址是否拼写正确。"

    code = getattr(exc, "errno", None)
    if code in (errno.ECONNREFUSED,):
        return "连接被拒绝，该端口没有服务在监听。"
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return "网络不可达，请检查网络或服务器地址。"
    if code in (errno.ETIMEDOUT,):
        return "连接超时。"
    if code in (errno.EHOSTDOWN,):
        return "目标主机已关闭。"
    if getattr(exc, "strerror", None):
        return str(exc.strerror)
    return str(exc)


def _compose_error(reason: str, detail: str = "") -> str:
    """把原因与补充说明合并为一条用户可读的错误信息。"""
    reason = (reason or "").strip().rstrip("。")
    detail = (detail or "").strip()
    if not detail or detail in reason:
        return f"{reason}。" if reason else "查询失败。"
    return f"{reason}（{detail}）"


async def query_server(
    address: str,
    *,
    timeout: float = 5.0,
    allow_srv: bool = False,
    default_port: int = DEFAULT_PORT,
) -> ServerStatus:
    """查询一个 Minecraft Java 版服务器的状态。

    即使查询失败也会返回一个 ServerStatus（``online=False`` 且带 ``error``），
    方便调用方直接渲染，无需处理异常。

    Args:
        address: 服务器地址，形如 ``host`` 或 ``host:port``。
        timeout: 单次网络操作的超时时间（秒）。
        allow_srv: 未显式指定端口时，是否尝试解析 SRV 记录。
        default_port: 默认端口。

    Returns:
        查询结果。
    """
    display = (address or "").strip()
    host, port, explicit_port = parse_address(display, default_port)

    if not host:
        return ServerStatus(
            address=display,
            host="",
            port=port,
            online=False,
            error="未提供服务器地址。",
        )

    if not is_valid_address(display):
        return ServerStatus(
            address=display,
            host=host,
            port=port,
            online=False,
            error=f"地址「{display}」格式不合法。",
        )

    connect_host, connect_port = host, port
    if allow_srv and not explicit_port and not _looks_like_ip(host):
        srv = await resolve_srv(host)
        if srv:
            connect_host, connect_port = srv

    try:
        return await _query_and_build(
            display, connect_host, connect_port, timeout,
        )
    except asyncio.CancelledError:
        # 插件被卸载时不应吞掉取消信号。
        raise
    except MCPingError as exc:
        return ServerStatus(
            address=display,
            host=connect_host,
            port=connect_port,
            online=False,
            error=_compose_error(exc.reason, exc.detail),
        )
    except Exception as exc:  # noqa: BLE001
        # 兜底：查询过程绝不应该把异常抛给消息管线。
        return ServerStatus(
            address=display,
            host=connect_host,
            port=connect_port,
            online=False,
            error=_compose_error("查询失败", f"{type(exc).__name__}: {exc}"),
        )


async def _query_and_build(
    display: str,
    connect_host: str,
    connect_port: int,
    timeout: float,
) -> ServerStatus:
    """执行查询并把成功的响应组装为 ServerStatus。

    Raises:
        MCPingError: 现代与旧版协议均失败。
    """
    try:
        data, latency_ms = await _query_modern(connect_host, connect_port, timeout)
    except MCPingError as modern_error:
        # 现代协议失败时，尝试旧版协议（部分 1.6 及以下服务器）。
        try:
            legacy_status = await _query_legacy(connect_host, connect_port, timeout)
        except MCPingError:
            raise modern_error from None
        legacy_status.address = display
        legacy_status.host = connect_host
        legacy_status.port = connect_port
        return legacy_status

    version, protocol = _extract_version(data)
    motd = clean_motd(data.get("description"))
    favicon = data.get("favicon")
    if not isinstance(favicon, str) or "base64," not in favicon:
        favicon = None

    return ServerStatus(
        address=display,
        host=connect_host,
        port=connect_port,
        online=True,
        latency_ms=latency_ms,
        version=version,
        protocol=protocol,
        motd=motd,
        motd_raw=data.get("description"),
        players=_extract_player_info(data),
        favicon=favicon,
    )


def _looks_like_ip(host: str) -> bool:
    """判断主机名是否是一个 IP 地址（IP 地址不应做 SRV 查询）。"""
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
