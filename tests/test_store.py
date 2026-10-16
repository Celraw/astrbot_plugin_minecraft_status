"""存储层的单元测试，重点验证群聊之间的数据隔离。"""

from __future__ import annotations

import pytest

import _pluginpath  # noqa: F401  （把仓库根目录注册为插件包）

from astrbot_plugin_minecraft_status.store import (  # noqa: E402
    ServerStore,
    StoreError,
    build_session_key,
    find_key,
    validate_server_name,
)


class MemoryBackend:
    """内存持久化后端，模拟 AstrBot 的 KV 存储。"""

    def __init__(self) -> None:
        self.data: dict = {}
        self.save_count = 0

    async def load(self):
        """返回已保存的数据。"""
        return self.data

    async def save(self, data: dict) -> None:
        """保存数据并计数。"""
        self.data = data
        self.save_count += 1


@pytest.fixture
def backend() -> MemoryBackend:
    """提供一个内存后端。"""
    return MemoryBackend()


@pytest.fixture
def store(backend: MemoryBackend) -> ServerStore:
    """提供一个绑定内存后端的存储实例。"""
    return ServerStore(loader=backend.load, saver=backend.save)


# ----------------------------------------------------------------------
# 会话 key
# ----------------------------------------------------------------------
def test_build_session_key_group():
    """群聊应使用 group: 前缀。"""
    assert build_session_key("123456", "789") == "group:123456"


def test_build_session_key_private():
    """私聊应使用 private: 前缀并带上会话 ID。"""
    assert build_session_key("", "789") == "private:789"


def test_build_session_key_none():
    """缺少信息时也应返回可用的 key。"""
    assert build_session_key(None, None) == "private:"


# ----------------------------------------------------------------------
# 名称校验
# ----------------------------------------------------------------------
@pytest.mark.parametrize("name", ["主服", "main", "我的服务器", "srv-1", "A_1"])
def test_validate_server_name_accepts_valid(name):
    """常规名称应通过校验。"""
    assert validate_server_name(name) is None


@pytest.mark.parametrize("name", ["", "   ", "a b", "a/b", "a\\b"])
def test_validate_server_name_rejects_invalid(name):
    """含空白或斜杠的名称应被拒绝。"""
    assert validate_server_name(name) is not None


def test_validate_server_name_rejects_reserved():
    """保留指令名不能被用作服务器名。"""
    assert validate_server_name("add") is not None
    assert validate_server_name("list") is not None


def test_validate_server_name_rejects_too_long():
    """超长名称应被拒绝。"""
    assert validate_server_name("x" * 100) is not None


def test_find_key_is_case_insensitive():
    """查找应支持忽略大小写。"""
    assert find_key({"Main": "a"}, "main") == "Main"
    assert find_key({"Main": "a"}, "Missing") is None


# ----------------------------------------------------------------------
# 基本增删改查
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_add_and_get_server(store: ServerStore):
    """添加后应能查询到。"""
    name = await store.add_server("group:1", "主服", "mc.example.com:25565")
    assert name == "主服"
    assert await store.get_server("group:1", "主服") == ("主服", "mc.example.com:25565")


@pytest.mark.asyncio
async def test_first_server_becomes_default(store: ServerStore):
    """第一个添加的服务器应自动成为默认服务器。"""
    await store.add_server("group:1", "主服", "mc.example.com")
    assert await store.get_default("group:1") == ("主服", "mc.example.com")


@pytest.mark.asyncio
async def test_second_server_does_not_override_default(store: ServerStore):
    """后续添加的服务器不应改变默认服务器。"""
    await store.add_server("group:1", "主服", "a.example.com")
    await store.add_server("group:1", "副服", "b.example.com")
    assert await store.get_default("group:1") == ("主服", "a.example.com")


@pytest.mark.asyncio
async def test_add_duplicate_same_address_raises(store: ServerStore):
    """重复添加相同地址应报错。"""
    await store.add_server("group:1", "主服", "a.example.com")
    with pytest.raises(StoreError):
        await store.add_server("group:1", "主服", "a.example.com")


@pytest.mark.asyncio
async def test_add_duplicate_name_updates_address(store: ServerStore):
    """同名但不同地址应更新地址。"""
    await store.add_server("group:1", "主服", "a.example.com")
    await store.add_server("group:1", "主服", "b.example.com")
    assert await store.get_server("group:1", "主服") == ("主服", "b.example.com")


@pytest.mark.asyncio
async def test_add_duplicate_name_case_insensitive(store: ServerStore):
    """同名判断应忽略大小写。"""
    await store.add_server("group:1", "Main", "a.example.com")
    updated = await store.add_server("group:1", "main", "b.example.com")
    assert updated == "Main"
    servers = await store.list_servers("group:1")
    assert len(servers) == 1
    assert servers[0][1] == "b.example.com"


@pytest.mark.asyncio
async def test_add_empty_address_raises(store: ServerStore):
    """空地址应报错。"""
    with pytest.raises(StoreError):
        await store.add_server("group:1", "主服", "")


@pytest.mark.asyncio
async def test_add_invalid_name_raises(store: ServerStore):
    """非法名称应报错。"""
    with pytest.raises(StoreError):
        await store.add_server("group:1", "a b", "mc.example.com")


@pytest.mark.asyncio
async def test_add_respects_max_servers(store: ServerStore):
    """超过数量上限应报错。"""
    for index in range(3):
        await store.add_server("group:1", f"服{index}", f"s{index}.example.com")
    with pytest.raises(StoreError):
        await store.add_server(
            "group:1",
            "服9",
            "s9.example.com",
            max_servers=3,
        )


# ----------------------------------------------------------------------
# 删除与默认服务器顺延
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_server(store: ServerStore):
    """删除后应查询不到。"""
    await store.add_server("group:1", "主服", "a.example.com")
    deleted = await store.delete_server("group:1", "主服")
    assert deleted == "主服"
    assert await store.get_server("group:1", "主服") is None


@pytest.mark.asyncio
async def test_delete_default_falls_back(store: ServerStore):
    """删除默认服务器后应顺延到剩余的服务器。"""
    await store.add_server("group:1", "主服", "a.example.com")
    await store.add_server("group:1", "副服", "b.example.com")
    await store.delete_server("group:1", "主服")
    assert await store.get_default("group:1") == ("副服", "b.example.com")


@pytest.mark.asyncio
async def test_delete_last_server_clears_default(store: ServerStore):
    """删除最后一个服务器后默认应为空。"""
    await store.add_server("group:1", "主服", "a.example.com")
    await store.delete_server("group:1", "主服")
    assert await store.get_default("group:1") is None
    assert await store.list_servers("group:1") == []


@pytest.mark.asyncio
async def test_delete_missing_server_raises(store: ServerStore):
    """删除不存在的服务器应报错。"""
    await store.add_server("group:1", "主服", "a.example.com")
    with pytest.raises(StoreError):
        await store.delete_server("group:1", "不存在")


@pytest.mark.asyncio
async def test_delete_on_empty_session_raises(store: ServerStore):
    """在空会话上删除应给出友好提示。"""
    with pytest.raises(StoreError):
        await store.delete_server("group:empty", "任意")


# ----------------------------------------------------------------------
# 设置默认
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_set_default(store: ServerStore):
    """设置默认应生效。"""
    await store.add_server("group:1", "主服", "a.example.com")
    await store.add_server("group:1", "副服", "b.example.com")
    name = await store.set_default("group:1", "副服")
    assert name == "副服"
    assert await store.get_default("group:1") == ("副服", "b.example.com")


@pytest.mark.asyncio
async def test_set_default_missing_raises(store: ServerStore):
    """设置不存在的服务器为默认应报错。"""
    await store.add_server("group:1", "主服", "a.example.com")
    with pytest.raises(StoreError):
        await store.set_default("group:1", "不存在")


# ----------------------------------------------------------------------
# 列表与隔离
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_servers_marks_default(store: ServerStore):
    """列表应标出默认服务器。"""
    await store.add_server("group:1", "主服", "a.example.com")
    await store.add_server("group:1", "副服", "b.example.com")
    servers = await store.list_servers("group:1")
    assert servers == [
        ("主服", "a.example.com", True),
        ("副服", "b.example.com", False),
    ]


@pytest.mark.asyncio
async def test_group_isolation(store: ServerStore):
    """不同群聊的数据必须完全隔离。"""
    await store.add_server("group:111", "甲服", "a.example.com")
    await store.add_server("group:222", "乙服", "b.example.com")

    assert await store.list_servers("group:111") == [("甲服", "a.example.com", True)]
    assert await store.list_servers("group:222") == [("乙服", "b.example.com", True)]
    assert await store.get_server("group:111", "乙服") is None
    assert await store.get_server("group:222", "甲服") is None


@pytest.mark.asyncio
async def test_private_and_group_isolated(store: ServerStore):
    """私聊与群聊的数据也应互相隔离。"""
    await store.add_server("group:1", "群服", "a.example.com")
    await store.add_server("private:1", "私服", "b.example.com")
    assert await store.get_server("group:1", "私服") is None
    assert await store.get_server("private:1", "群服") is None


@pytest.mark.asyncio
async def test_delete_in_one_group_does_not_affect_another(store: ServerStore):
    """删除操作不应影响其他群聊。"""
    await store.add_server("group:111", "甲服", "a.example.com")
    await store.add_server("group:222", "乙服", "b.example.com")
    await store.delete_server("group:111", "甲服")
    assert await store.list_servers("group:111") == []
    assert await store.list_servers("group:222") == [("乙服", "b.example.com", True)]


# ----------------------------------------------------------------------
# 持久化
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_persistence_across_instances(backend: MemoryBackend):
    """数据应能在重新创建实例后恢复。"""
    first = ServerStore(loader=backend.load, saver=backend.save)
    await first.add_server("group:1", "主服", "a.example.com")
    await first.set_default("group:1", "主服")

    second = ServerStore(loader=backend.load, saver=backend.save)
    servers = await second.list_servers("group:1")
    assert servers == [("主服", "a.example.com", True)]
    assert await second.get_default("group:1") == ("主服", "a.example.com")


@pytest.mark.asyncio
async def test_load_tolerates_corrupt_data():
    """持久化数据损坏时应安全降级为空。"""
    async def bad_load():
        return {"groups": "not-a-dict"}

    store = ServerStore(loader=bad_load, saver=None)
    assert await store.list_servers("group:1") == []


@pytest.mark.asyncio
async def test_load_ignores_invalid_default():
    """default 指向不存在的服务器时应被忽略。"""
    async def load():
        return {
            "groups": {
                "group:1": {
                    "servers": {"主服": "a.example.com"},
                    "default": "幽灵服务器",
                },
            },
        }

    store = ServerStore(loader=load, saver=None)
    assert await store.get_default("group:1") is None
    assert await store.list_servers("group:1") == [("主服", "a.example.com", False)]


@pytest.mark.asyncio
async def test_save_is_called_on_mutation(store: ServerStore, backend: MemoryBackend):
    """每次写操作都应触发持久化。"""
    before = backend.save_count
    await store.add_server("group:1", "主服", "a.example.com")
    assert backend.save_count > before

    before = backend.save_count
    await store.set_default("group:1", "主服")
    assert backend.save_count > before

    before = backend.save_count
    await store.delete_server("group:1", "主服")
    assert backend.save_count > before
