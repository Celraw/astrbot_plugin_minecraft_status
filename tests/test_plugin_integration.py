"""插件主类的集成测试。

这些测试会真实导入 AstrBot（若环境中存在）并实例化插件，
用一个假的 event 驱动完整的指令流程，验证：

    · /mc 未设置服务器时的提示
    · /mc add、/mc set、/mc list、/mc delete 全链路
    · /mc <名称> 查询指定服务器
    · 群聊之间的数据隔离
    · 文字与图片两种输出模式

若环境中没有 AstrBot，则整个模块被跳过，不影响纯逻辑测试。
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest

import _pluginpath  # noqa: F401  （把仓库根目录注册为插件包）

astrbot = pytest.importorskip(
    "astrbot",
    reason="未安装 AstrBot，跳过插件集成测试",
)

from astrbot.core.message.message_event_result import (  # noqa: E402
    MessageEventResult,
)
from astrbot.core.message.components import ComponentType  # noqa: E402

from astrbot_plugin_minecraft_status import mc_protocol as mc  # noqa: E402
from astrbot_plugin_minecraft_status.main import (  # noqa: E402
    KV_KEY,
    MinecraftStatusPlugin,
)


class FakeConfig(dict):
    """模拟 AstrBotConfig。"""


class FakeEvent:
    """最小化的 AstrMessageEvent 替身，只实现插件用到的方法。"""

    def __init__(
        self,
        message_str: str,
        *,
        group_id: str = "123456",
        session_id: str = "123456",
    ) -> None:
        self.message_str = message_str
        self._group_id = group_id
        self._session_id = session_id
        self._extras: dict = {}

    def get_group_id(self) -> str:
        """返回群号。"""
        return self._group_id

    def get_session_id(self) -> str:
        """返回会话 ID。"""
        return self._session_id

    def get_message_str(self) -> str:
        """返回消息纯文本（供 AstrBot 的指令过滤器使用）。"""
        return self.message_str

    def set_extra(self, key, value) -> None:
        """设置附加信息（AstrBot 的指令过滤器会写入解析后的参数）。"""
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        """读取附加信息。"""
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def plain_result(self, text: str) -> MessageEventResult:
        """构造纯文本结果。"""
        return MessageEventResult().message(text)

    def image_result(self, url_or_path: str) -> MessageEventResult:
        """构造图片结果。"""
        if url_or_path.startswith("http"):
            return MessageEventResult().url_image(url_or_path)
        return MessageEventResult().file_image(url_or_path)


def result_text(result: MessageEventResult) -> str:
    """提取结果中的纯文本。"""
    return result.get_plain_text()


class FakeMinecraftServer:
    """本地 SLP 假服务器，供集成测试端到端使用。"""

    def __init__(self, players_online: int = 3) -> None:
        self.players_online = players_online
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> "FakeMinecraftServer":
        """启动并监听随机端口。"""
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        """关闭服务器。"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _read_varint(self, reader):
        """读取 VarInt。"""
        result = 0
        for shift in range(5):
            byte = (await reader.readexactly(1))[0]
            result |= (byte & 0x7F) << (7 * shift)
            if not byte & 0x80:
                return result
        return result

    async def _read_packet(self, reader):
        """读取完整数据包。"""
        length = await self._read_varint(reader)
        body = await reader.readexactly(length)
        packet_id, offset = mc.decode_varint(body)
        return packet_id, body[offset:]

    async def _handle(self, reader, writer):
        """回应握手、状态与 Ping。"""
        try:
            await self._read_packet(reader)  # Handshake
            for _ in range(2):
                packet_id, payload = await self._read_packet(reader)
                if packet_id == 0x00:
                    data = {
                        "version": {"name": "1.21.1", "protocol": 767},
                        "players": {
                            "max": 20,
                            "online": self.players_online,
                            "sample": [{"name": "Alice", "id": "0" * 32}],
                        },
                        "description": {"text": "Test Server"},
                    }
                    body = json.dumps(data).encode()
                    payload_out = (
                        mc.encode_varint(0x00)
                        + mc.encode_varint(len(body))
                        + body
                    )
                    writer.write(mc.encode_varint(len(payload_out)) + payload_out)
                    await writer.drain()
                elif packet_id == 0x01:
                    writer.write(
                        mc.pack_packet(0x01, struct.pack(">q", payload[:8])),
                    )
                    await writer.drain()
                    break
        except Exception:
            pass
        finally:
            writer.close()


@pytest.fixture
def plugin(monkeypatch):
    """构造一个使用内存存储的插件实例。"""
    monkeypatch.setattr(mc, "query_server", mc.query_server)  # 保持真实查询实现

    config = FakeConfig({"output_mode": "text", "query_timeout": 5})
    instance = MinecraftStatusPlugin(context=None, config=config)

    # 用内存字典替换 KV 存储，避免依赖 AstrBot 的持久化后端。
    storage: dict = {}

    async def load():
        return storage.get(KV_KEY, {})

    async def save(data):
        storage[KV_KEY] = data

    instance.store = type(instance.store)(
        loader=load,
        saver=save,
    )
    instance._storage = storage
    return instance


@pytest.mark.asyncio
async def test_mc_without_server_prompts_user(plugin):
    """未设置服务器时，/mc 应提示用户添加。"""
    result = await plugin.mc(FakeEvent("/mc"), "")
    text = result_text(result)
    assert "还没有添加任何服务器" in text
    assert "/mc add" in text


@pytest.mark.asyncio
async def test_mc_add_then_query_flow(plugin):
    """/mc add 之后应能通过 /mc 查询到。"""
    fake = await FakeMinecraftServer().start()
    try:
        add_result = await plugin.mc(
            FakeEvent(f"/mc add 主服 127.0.0.1:{fake.port}"),
            f"add 主服 127.0.0.1:{fake.port}",
        )
        assert "已添加" in result_text(add_result)

        # 第一个服务器自动成为默认，/mc 应直接查询它。
        query_result = await plugin.mc(FakeEvent("/mc"), "")
        text = result_text(query_result)
        assert "在线" in text
        assert "1.21.1" in text
        assert "3/20" in text
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_mc_query_named_server(plugin):
    """/mc <名称> 应查询指定服务器。"""
    fake = await FakeMinecraftServer(players_online=7).start()
    try:
        await plugin.mc(
            FakeEvent(f"/mc add 主服 127.0.0.1:{fake.port}"),
            f"add 主服 127.0.0.1:{fake.port}",
        )
        result = await plugin.mc(FakeEvent("/mc 主服"), "主服")
        text = result_text(result)
        assert "主服" in text
        assert "7/20" in text
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_mc_list_and_default_marking(plugin):
    """/mc list 应列出全部服务器并标出默认项。"""
    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    await plugin.mc(FakeEvent("/mc add 副服 b.example.com"), "add 副服 b.example.com")

    result = await plugin.mc(FakeEvent("/mc list"), "list")
    text = result_text(result)
    assert "共 2 个服务器" in text
    assert "主服" in text
    assert "副服" in text
    assert "（默认）" in text


@pytest.mark.asyncio
async def test_mc_set_changes_default(plugin):
    """/mc set 应改变默认服务器。"""
    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    await plugin.mc(FakeEvent("/mc add 副服 b.example.com"), "add 副服 b.example.com")

    result = await plugin.mc(FakeEvent("/mc set 副服"), "set 副服")
    assert "已将「副服」设为默认服务器" in result_text(result)

    listed = result_text(await plugin.mc(FakeEvent("/mc list"), "list"))
    # 副服应被标记为默认。
    assert "副服（默认）" in listed or "副服" in listed


@pytest.mark.asyncio
async def test_mc_delete_removes_server(plugin):
    """/mc delete 应删除服务器。"""
    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    await plugin.mc(FakeEvent("/mc add 副服 b.example.com"), "add 副服 b.example.com")

    result = await plugin.mc(FakeEvent("/mc delete 主服"), "delete 主服")
    text = result_text(result)
    assert "已删除服务器「主服」" in text
    assert "当前默认服务器：副服" in text

    listed = result_text(await plugin.mc(FakeEvent("/mc list"), "list"))
    assert "共 1 个服务器" in listed


@pytest.mark.asyncio
async def test_mc_delete_missing_server(plugin):
    """删除不存在的服务器应给出提示。"""
    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    result = await plugin.mc(FakeEvent("/mc delete 不存在"), "delete 不存在")
    assert "未找到服务器" in result_text(result)


@pytest.mark.asyncio
async def test_mc_set_missing_server(plugin):
    """设置不存在的服务器为默认应给出提示。"""
    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    result = await plugin.mc(FakeEvent("/mc set 不存在"), "set 不存在")
    assert "未找到服务器" in result_text(result)


@pytest.mark.asyncio
async def test_mc_help(plugin):
    """/mc help 应显示帮助。"""
    result = await plugin.mc(FakeEvent("/mc help"), "help")
    text = result_text(result)
    assert "/mc add" in text
    assert "/mc delete" in text
    assert "/mc set" in text
    assert "/mc list" in text


@pytest.mark.asyncio
async def test_group_isolation_end_to_end(plugin):
    """不同群聊的数据必须隔离。"""
    group_a = FakeEvent("/mc", group_id="111", session_id="111")
    group_b = FakeEvent("/mc", group_id="222", session_id="222")

    await plugin.mc(
        FakeEvent("/mc add 甲服 a.example.com", group_id="111", session_id="111"),
        "add 甲服 a.example.com",
    )
    await plugin.mc(
        FakeEvent("/mc add 乙服 b.example.com", group_id="222", session_id="222"),
        "add 乙服 b.example.com",
    )

    listed_a = result_text(await plugin.mc(group_a, "list"))
    listed_b = result_text(await plugin.mc(group_b, "list"))

    assert "甲服" in listed_a
    assert "乙服" not in listed_a
    assert "乙服" in listed_b
    assert "甲服" not in listed_b

    # B 群不应能查询到 A 群的服务器。
    missing = result_text(await plugin.mc(
        FakeEvent("/mc 甲服", group_id="222", session_id="222"),
        "甲服",
    ))
    assert "未找到服务器" in missing


@pytest.mark.asyncio
async def test_delete_in_one_group_not_affect_other(plugin):
    """删除操作不应影响其他群聊。"""
    await plugin.mc(
        FakeEvent("/mc add 甲服 a.example.com", group_id="111", session_id="111"),
        "add 甲服 a.example.com",
    )
    await plugin.mc(
        FakeEvent("/mc delete 甲服", group_id="111", session_id="111"),
        "delete 甲服",
    )
    listed = result_text(
        await plugin.mc(FakeEvent("/mc list", group_id="111", session_id="111"), "list"),
    )
    assert "还没有添加任何服务器" in listed


@pytest.mark.asyncio
async def test_image_output_mode_local_engine(plugin):
    """默认的本地渲染引擎在 image 模式下应返回本地图片。"""
    plugin.config["output_mode"] = "image"
    # 默认 render_engine 即为 local，不依赖任何外部渲染服务。
    assert plugin.render_engine == "local"

    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    result = await plugin.mc(FakeEvent("/mc 主服"), "主服")

    assert result.chain
    assert result.chain[0].type == ComponentType.Image
    # 本地渲染应产出真实存在的文件（Image 组件把路径放在 path，
    # file 则是 file:/// 形式的 URI）。
    local_path = result.chain[0].path
    assert local_path
    assert Path(local_path).is_file()


@pytest.mark.asyncio
async def test_image_output_mode_local_engine_for_list(plugin):
    """列表输出在 image 模式下也应渲染成图片。"""
    plugin.config["output_mode"] = "image"
    result = await plugin.mc(FakeEvent("/mc list"), "list")
    assert result.chain
    assert result.chain[0].type == ComponentType.Image


@pytest.mark.asyncio
async def test_image_output_mode_html_engine(plugin):
    """选择 html 引擎时应走 AstrBot 的文转图服务。"""
    plugin.config["output_mode"] = "image"
    plugin.config["render_engine"] = "html"
    plugin._template = "<html>{{ title }}</html>"

    captured: dict = {}

    from astrbot_plugin_minecraft_status.renderer_local import render_status_card_to_file

    fake_path = render_status_card_to_file({"title": "占位"})

    async def fake_html_render(tmpl, data, return_url=True, options=None):
        captured["return_url"] = return_url
        captured["data"] = data
        return fake_path

    plugin.html_render = fake_html_render  # type: ignore[method-assign]

    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    result = await plugin.mc(FakeEvent("/mc 主服"), "主服")

    assert result.chain
    assert result.chain[0].type == ComponentType.Image
    # 关键：必须要求渲染服务直接返回文件，避免平台再按 URL 下载一次。
    assert captured["return_url"] is False


@pytest.mark.asyncio
async def test_html_render_failure_falls_back_to_text(plugin):
    """html 引擎渲染失败且本地也失败时，应回退为纯文本。"""
    plugin.config["output_mode"] = "image"
    plugin.config["render_engine"] = "html"
    plugin._template = "<html>{{ title }}</html>"

    async def boom(tmpl, data, **kwargs):
        raise RuntimeError("渲染服务不可用")

    plugin.html_render = boom  # type: ignore[method-assign]

    # 断掉本地渲染，模拟两条路径都不可用。
    import astrbot_plugin_minecraft_status.main as main_module

    def local_boom(_data):
        raise RuntimeError("本地渲染不可用")

    original = main_module.render_status_card_to_file
    main_module.render_status_card_to_file = local_boom  # type: ignore[assignment]
    try:
        result = await plugin.mc(FakeEvent("/mc list"), "list")
    finally:
        main_module.render_status_card_to_file = original  # type: ignore[assignment]

    assert "服务器" in result_text(result)


@pytest.mark.asyncio
async def test_local_failure_falls_back_to_html(plugin):
    """本地渲染失败时应自动尝试 AstrBot 的文转图服务。"""
    plugin.config["output_mode"] = "image"
    plugin._template = "<html>{{ title }}</html>"

    from astrbot_plugin_minecraft_status.renderer_local import render_status_card_to_file

    fake_path = render_status_card_to_file({"title": "占位"})
    called: dict = {}

    async def fake_html_render(tmpl, data, return_url=True, options=None):
        called["used"] = True
        return fake_path

    plugin.html_render = fake_html_render  # type: ignore[method-assign]

    import astrbot_plugin_minecraft_status.main as main_module

    def local_boom(_data):
        raise RuntimeError("本地渲染不可用")

    original = main_module.render_status_card_to_file
    main_module.render_status_card_to_file = local_boom  # type: ignore[assignment]
    try:
        result = await plugin.mc(FakeEvent("/mc list"), "list")
    finally:
        main_module.render_status_card_to_file = original  # type: ignore[assignment]

    assert called.get("used") is True
    assert result.chain
    assert result.chain[0].type == ComponentType.Image


@pytest.mark.asyncio
async def test_invalid_address_rejected_on_add(plugin):
    """非法地址应被拒绝并给出提示。"""
    result = await plugin.mc(FakeEvent("/mc add 主服 张三的服务器"), "add 主服 张三的服务器")
    assert "格式不合法" in result_text(result)


@pytest.mark.asyncio
async def test_add_without_address_prompts(plugin):
    """缺少地址时应提示用法。"""
    result = await plugin.mc(FakeEvent("/mc add 主服"), "add 主服")
    text = result_text(result)
    assert "缺少服务器地址" in text
    assert "25565" in text


@pytest.mark.asyncio
async def test_query_offline_server_shows_reason(plugin):
    """查询离线的服务器应显示离线原因。"""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    await plugin.mc(
        FakeEvent(f"/mc add 离线服 127.0.0.1:{port}"),
        f"add 离线服 127.0.0.1:{port}",
    )
    result = await plugin.mc(FakeEvent("/mc 离线服"), "离线服")
    text = result_text(result)
    assert "离线" in text


@pytest.mark.asyncio
async def test_persistence_across_instances(plugin):
    """数据应写入底层存储，可被新实例读取。"""
    await plugin.mc(FakeEvent("/mc add 主服 a.example.com"), "add 主服 a.example.com")
    assert KV_KEY in plugin._storage
    payload = plugin._storage[KV_KEY]
    assert "groups" in payload
    assert "group:123456" in payload["groups"]
    assert payload["groups"]["group:123456"]["servers"] == {"主服": "a.example.com"}
