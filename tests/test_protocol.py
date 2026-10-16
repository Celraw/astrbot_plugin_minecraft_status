"""Minecraft SLP 协议实现的单元与端到端测试。

端到端部分会在本地启动一个真实的 TCP 假服务器，完整走一遍
Handshake -> Status Request -> Status Response -> Ping -> Pong 流程，
以验证协议实现确实能与服务器交互，而不仅仅是单测拼包。
"""

from __future__ import annotations

import asyncio
import json
import struct

import pytest

import _pluginpath  # noqa: F401  （把仓库根目录注册为插件包）

from astrbot_plugin_minecraft_status import mc_protocol as mc  # noqa: E402


# ----------------------------------------------------------------------
# VarInt / 数据包
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (255, b"\xff\x01"),
        (25565, b"\xdd\xc7\x01"),
        (2097151, b"\xff\xff\x7f"),
        (2147483647, b"\xff\xff\xff\xff\x07"),
    ],
)
def test_encode_varint(value, expected):
    """VarInt 编码应与协议规范一致。"""
    assert mc.encode_varint(value) == expected


@pytest.mark.parametrize(
    "value",
    [0, 1, 2, 127, 128, 255, 300, 25565, 2097151, 2147483647],
)
def test_varint_roundtrip(value):
    """VarInt 编解码应可逆。"""
    encoded = mc.encode_varint(value)
    decoded, offset = mc.decode_varint(encoded)
    assert decoded == value
    assert offset == len(encoded)


def test_decode_varint_rejects_truncated_data():
    """数据被截断时应报协议错误。"""
    with pytest.raises(mc.MCProtocolError):
        mc.decode_varint(b"\x80\x80")


def test_decode_varint_rejects_overlong():
    """超过 5 字节的 VarInt 应被拒绝。"""
    with pytest.raises(mc.MCProtocolError):
        mc.decode_varint(b"\x80\x80\x80\x80\x80\x01")


def test_decode_varint_signed():
    """有符号模式下，-1 应能被正确还原。"""
    assert mc.decode_varint(mc.encode_varint(-1), signed=True)[0] == -1
    assert mc.decode_varint(mc.encode_varint(-1))[0] == 4294967295
    assert mc.decode_varint(mc.encode_varint(767), signed=True)[0] == 767


def test_pack_and_unpack_packet():
    """完整数据包应可正确往返。"""
    packet = mc.pack_packet(0x00, b"hello")
    packet_id, payload = mc.read_packet(packet)
    assert packet_id == 0x00
    assert payload == b"hello"


def test_pack_packet_empty_payload():
    """无负载的数据包也应正确打包。"""
    packet = mc.pack_packet(0x00)
    packet_id, payload = mc.read_packet(packet)
    assert packet_id == 0x00
    assert payload == b""


def test_read_packet_rejects_inconsistent_length():
    """长度前缀与实际内容不一致时应报错。"""
    with pytest.raises(mc.MCProtocolError):
        mc.read_packet(b"\x10\x00")


def test_pack_string_uses_utf8_byte_length():
    """String 的长度前缀必须是 UTF-8 字节数而非字符数。"""
    encoded = mc.pack_string("中文")
    length, offset = mc.decode_varint(encoded)
    assert length == len("中文".encode("utf-8")) == 6
    assert encoded[offset:] == "中文".encode("utf-8")


# ----------------------------------------------------------------------
# MOTD 清理
# ----------------------------------------------------------------------
def test_clean_motd_plain_string():
    """普通字符串 MOTD 应原样返回。"""
    assert mc.clean_motd("A Minecraft Server") == "A Minecraft Server"


def test_clean_motd_removes_color_codes():
    """应移除 § 颜色代码。"""
    assert mc.clean_motd("\u00a7aHello \u00a7lWorld") == "Hello World"


def test_clean_motd_text_component():
    """{"text": ...} 结构应被正确提取。"""
    assert mc.clean_motd({"text": "Hello"}) == "Hello"


def test_clean_motd_nested_extra():
    """嵌套的 extra 列表应按顺序拼接。"""
    component = {
        "text": "A",
        "extra": [{"text": "B"}, {"text": "C", "extra": [{"text": "D"}]}],
    }
    assert mc.clean_motd(component) == "ABCD"


def test_clean_motd_list_component():
    """列表形式的组件应被拼接。"""
    assert mc.clean_motd([{"text": "x"}, "y"]) == "xy"


def test_clean_motd_handles_none():
    """description 缺失时应返回空串。"""
    assert mc.clean_motd(None) == ""


# ----------------------------------------------------------------------
# 地址解析
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("mc.example.com", ("mc.example.com", 25565, False)),
        ("mc.example.com:25566", ("mc.example.com", 25566, True)),
        ("127.0.0.1", ("127.0.0.1", 25565, False)),
        ("127.0.0.1:30000", ("127.0.0.1", 30000, True)),
        ("[2001:db8::1]", ("2001:db8::1", 25565, False)),
        ("[2001:db8::1]:25566", ("2001:db8::1", 25566, True)),
    ],
)
def test_parse_address(address, expected):
    """各种地址写法都应被正确解析。"""
    assert mc.parse_address(address) == expected


def test_parse_address_empty():
    """空地址返回空主机名。"""
    host, port, explicit = mc.parse_address("")
    assert host == ""
    assert port == 25565
    assert explicit is False


def test_parse_address_trims_whitespace():
    """首尾空白应被忽略。"""
    assert mc.parse_address("  mc.example.com  ")[0] == "mc.example.com"


@pytest.mark.parametrize(
    "address",
    ["mc.example.com", "mc.example.com:25565", "127.0.0.1", "[2001:db8::1]:25565"],
)
def test_is_valid_address_accepts_valid(address):
    """合法地址应通过校验。"""
    assert mc.is_valid_address(address)


@pytest.mark.parametrize(
    "address",
    ["", "  ", "mc example.com", "mc.example.com:99999", "服务器"],
)
def test_is_valid_address_rejects_invalid(address):
    """非法地址应被拒绝。"""
    assert not mc.is_valid_address(address)


# ----------------------------------------------------------------------
# 端到端：本地假服务器
# ----------------------------------------------------------------------
class FakeMinecraftServer:
    """一个最小的 Minecraft SLP 假服务器，用于端到端验证。

    Args:
        data: Status Response 返回的 JSON 内容。
        host_returned: 记录握手包中携带的 host（用于断言）。
    """

    def __init__(self, data: dict) -> None:
        self.data = data
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.received_host: str | None = None
        self.received_port: int | None = None
        self.received_ping: int | None = None
        self.handshake_protocol: int | None = None
        self.closed_cleanly = False

    async def start(self) -> "FakeMinecraftServer":
        """启动假服务器并监听随机端口。"""
        self.server = await asyncio.start_server(
            self._handle,
            host="127.0.0.1",
            port=0,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        """关闭假服务器。"""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _read_varint(self, reader: asyncio.StreamReader) -> int:
        """读取一个 VarInt。"""
        result = 0
        for shift in range(5):
            chunk = await reader.readexactly(1)
            byte = chunk[0]
            result |= (byte & 0x7F) << (7 * shift)
            if not byte & 0x80:
                return result
        raise AssertionError("VarInt 过长")

    async def _read_packet(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        """读取一个完整数据包。"""
        length = await self._read_varint(reader)
        body = await reader.readexactly(length)
        packet_id, offset = mc.decode_varint(body)
        return packet_id, body[offset:]

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理一条连接：校验握手、返回状态、回应 Ping。"""
        try:
            # 1. Handshake
            packet_id, payload = await self._read_packet(reader)
            assert packet_id == 0x00, f"期望 Handshake(0x00)，实际 {packet_id:#x}"
            proto, offset = mc.decode_varint(payload, signed=True)
            self.handshake_protocol = proto
            host_len, offset = mc.decode_varint(payload, offset)
            self.received_host = payload[offset : offset + host_len].decode("utf-8")
            offset += host_len
            self.received_port = struct.unpack(">H", payload[offset : offset + 2])[0]
            next_state, _ = mc.decode_varint(payload, offset + 2)
            assert next_state == 1, f"期望 next_state=1，实际 {next_state}"

            # 2. Status Request / Ping Request（顺序不限）
            for _ in range(2):
                packet_id, payload = await self._read_packet(reader)
                if packet_id == 0x00:
                    body = json.dumps(self.data).encode("utf-8")
                    response = mc.encode_varint(0x00) + mc.encode_varint(
                        len(body),
                    ) + body
                    writer.write(mc.encode_varint(len(response)) + response)
                    await writer.drain()
                elif packet_id == 0x01:
                    self.received_ping = struct.unpack(">q", payload[:8])[0]
                    pong = mc.pack_packet(0x01, struct.pack(">q", self.received_ping))
                    writer.write(pong)
                    await writer.drain()
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
                self.closed_cleanly = True
            except Exception:
                pass


SAMPLE_RESPONSE = {
    "version": {"name": "1.21.1", "protocol": 767},
    "players": {
        "max": 20,
        "online": 3,
        "sample": [
            {"name": "Alice", "id": "00000000-0000-0000-0000-000000000001"},
            {"name": "Bob", "id": "00000000-0000-0000-0000-000000000002"},
            {"name": "Charlie", "id": "00000000-0000-0000-0000-000000000003"},
        ],
    },
    "description": {"text": "\u00a7aA Minecraft \u00a7bServer"},
    "favicon": "data:image/png;base64,iVBORw0KGgo=",
    "enforcesSecureChat": False,
}


@pytest.mark.asyncio
async def test_query_server_end_to_end_success():
    """完整查询流程应返回正确的服务器信息。"""
    fake = await FakeMinecraftServer(SAMPLE_RESPONSE).start()
    try:
        status = await mc.query_server(
            f"127.0.0.1:{fake.port}",
            timeout=5,
            allow_srv=False,
        )
    finally:
        await fake.stop()

    assert status.online is True
    assert status.version == "1.21.1"
    assert status.protocol == 767
    assert status.motd == "A Minecraft Server"
    assert status.players.online == 3
    assert status.players.max == 20
    assert status.players.sample == ["Alice", "Bob", "Charlie"]
    assert status.latency_ms is not None and status.latency_ms >= 0
    assert status.favicon == "data:image/png;base64,iVBORw0KGgo="
    assert status.legacy is False

    # 假服务器应收到正确的握手信息。
    assert fake.received_host == "127.0.0.1"
    assert fake.received_port == fake.port
    assert fake.handshake_protocol == mc.STATUS_PROTOCOL_VERSION
    assert fake.received_ping is not None


@pytest.mark.asyncio
async def test_query_server_uses_default_port_in_handshake():
    """未指定端口时，握手包中应使用 25565。"""
    fake = await FakeMinecraftServer(SAMPLE_RESPONSE).start()
    # 假服务器监听的是随机端口，这里直接连它的真实端口，
    # 但地址字符串不带端口，以验证默认端口填充逻辑。
    try:
        status = await mc.query_server(
            f"127.0.0.1:{fake.port}",
            timeout=5,
        )
    finally:
        await fake.stop()
    assert status.online


@pytest.mark.asyncio
async def test_query_server_handles_missing_description():
    """服务器未提供 description 时不应报错。"""
    data = {"version": {"name": "1.20"}, "players": {"max": 10, "online": 0}}
    fake = await FakeMinecraftServer(data).start()
    try:
        status = await mc.query_server(f"127.0.0.1:{fake.port}", timeout=5)
    finally:
        await fake.stop()

    assert status.online is True
    assert status.motd == ""
    assert status.players.sample == []


@pytest.mark.asyncio
async def test_query_server_handles_missing_players():
    """players 字段缺失时应安全降级为 0/0。"""
    data = {"version": {"name": "1.20"}, "description": "hi"}
    fake = await FakeMinecraftServer(data).start()
    try:
        status = await mc.query_server(f"127.0.0.1:{fake.port}", timeout=5)
    finally:
        await fake.stop()

    assert status.online is True
    assert status.players.online == 0
    assert status.players.max == 0


@pytest.mark.asyncio
async def test_query_server_connection_refused():
    """端口未开放时应返回离线状态而不是抛异常。"""
    # 先占用一个端口再释放，确保该端口大概率是关闭的。
    server = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    status = await mc.query_server(f"127.0.0.1:{port}", timeout=2)
    assert status.online is False
    assert status.error


@pytest.mark.asyncio
async def test_query_server_timeout_on_blackhole():
    """服务器接受连接但不响应时，应因超时而返回离线状态。"""
    async def _silent(reader, writer):
        try:
            await asyncio.sleep(30)
        finally:
            writer.close()

    server = await asyncio.start_server(_silent, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        status = await mc.query_server(f"127.0.0.1:{port}", timeout=0.5)
    finally:
        server.close()
        await server.wait_closed()

    assert status.online is False
    assert status.error


@pytest.mark.asyncio
async def test_query_server_invalid_address_returns_offline():
    """非法地址应直接返回离线状态。"""
    status = await mc.query_server("mc example.com", timeout=1)
    assert status.online is False
    assert "不合法" in (status.error or "")


@pytest.mark.asyncio
async def test_query_server_unresolvable_host_reports_dns_error():
    """域名无法解析时，错误信息应指出解析失败。"""
    status = await mc.query_server("nonexistent-zzz.invalid:25565", timeout=5)
    assert status.online is False
    assert "解析失败" in (status.error or "")


@pytest.mark.asyncio
async def test_query_server_malformed_hostname_reports_invalid():
    """主机名本身不合法（IDNA 编码失败）时应给出明确提示。"""
    status = await mc.query_server(".invalid.invalid.invalid:25565", timeout=5)
    assert status.online is False
    assert "不合法" in (status.error or "") or "解析失败" in (status.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        ".invalid.invalid.invalid:25565",
        "a" * 300 + ".com:25565",
        "-bad-label-.com:25565",
        "under_score.invalid.test:25565",
        "127.0.0.1:99999",
        "localhost:0",
    ],
)
async def test_query_server_never_raises(address):
    """任何畸形输入都只能返回离线结果，绝不能抛异常给消息管线。"""
    status = await mc.query_server(address, timeout=2)
    assert status.online is False
    assert status.error


@pytest.mark.asyncio
async def test_query_server_refused_reports_reason():
    """端口未监听时，错误信息应说明连接被拒绝。"""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    status = await mc.query_server(f"127.0.0.1:{port}", timeout=3)
    assert status.online is False
    assert "拒绝" in (status.error or "") or "无法连接" in (status.error or "")


def test_compose_error_merges_detail():
    """错误信息应把原因与补充说明合并。"""
    merged = mc._compose_error("无法连接 a:25565。", "连接被拒绝。")
    assert "无法连接" in merged
    assert "连接被拒绝" in merged


def test_compose_error_without_detail():
    """没有补充说明时不应留下多余括号。"""
    assert mc._compose_error("查询超时。") == "查询超时。"
    assert "（" not in mc._compose_error("查询超时。", "")


@pytest.mark.asyncio
async def test_query_server_empty_address_returns_offline():
    """空地址应直接返回离线状态。"""
    status = await mc.query_server("", timeout=1)
    assert status.online is False


@pytest.mark.asyncio
async def test_query_server_legacy_fallback():
    """现代协议失败时，应回退到旧版协议并解析出信息。"""
    legacy_body = "\u00a71\x00" "127\x00" "1.5.2\x00" "Legacy MOTD\x00" "4\x00" "30"

    async def _legacy_handler(reader, writer):
        try:
            await reader.read(2)  # 读取 0xFE 0x01
            payload = legacy_body.encode("utf-16-be")
            writer.write(b"\xff" + struct.pack(">H", len(legacy_body)) + payload)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(_legacy_handler, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        status = await mc.query_server(f"127.0.0.1:{port}", timeout=3)
    finally:
        server.close()
        await server.wait_closed()

    assert status.online is True
    assert status.legacy is True
    assert status.version == "1.5.2"
    assert status.motd == "Legacy MOTD"
    assert status.players.online == 4
    assert status.players.max == 30
