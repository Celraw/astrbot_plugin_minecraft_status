"""指令解析与格式化输出的单元测试。"""

from __future__ import annotations

import pytest

import _pluginpath  # noqa: F401  （把仓库根目录注册为插件包）

from astrbot_plugin_minecraft_status import commands as cmd  # noqa: E402
from astrbot_plugin_minecraft_status import formatter as fmt  # noqa: E402
from astrbot_plugin_minecraft_status.mc_protocol import (  # noqa: E402
    PlayerInfo,
    ServerStatus,
)


# ----------------------------------------------------------------------
# 指令解析
# ----------------------------------------------------------------------
def test_parse_empty_is_query_default():
    """/mc 应查询默认服务器。"""
    parsed = cmd.parse_mc_command("")
    assert parsed.action == cmd.ACTION_QUERY_DEFAULT


def test_parse_whitespace_only_is_query_default():
    """只有空白也应查询默认服务器。"""
    assert cmd.parse_mc_command("   ").action == cmd.ACTION_QUERY_DEFAULT


def test_parse_add_with_port():
    """add 应解析出名称与带端口的地址。"""
    parsed = cmd.parse_mc_command("add 主服 mc.example.com:25565")
    assert parsed.action == cmd.ACTION_ADD
    assert parsed.name == "主服"
    assert parsed.address == "mc.example.com:25565"


def test_parse_add_without_port():
    """端口留空时应只保留主机名。"""
    parsed = cmd.parse_mc_command("add 主服 mc.example.com")
    assert parsed.action == cmd.ACTION_ADD
    assert parsed.name == "主服"
    assert parsed.address == "mc.example.com"


def test_parse_add_with_separate_port():
    """端口单独给出时应被拼接。"""
    parsed = cmd.parse_mc_command("add 主服 mc.example.com 25566")
    assert parsed.address == "mc.example.com:25566"


def test_parse_add_ipv6_separate_port():
    """IPv6 地址应加上方括号。"""
    parsed = cmd.parse_mc_command("add 主服 2001:db8::1 25566")
    assert parsed.address == "[2001:db8::1]:25566"


def test_parse_add_missing_address():
    """只给出名称时地址应为空，由调用方提示。"""
    parsed = cmd.parse_mc_command("add 主服")
    assert parsed.action == cmd.ACTION_ADD
    assert parsed.name == "主服"
    assert parsed.address == ""


def test_parse_add_no_args():
    """add 不带参数时应返回 unknown。"""
    assert cmd.parse_mc_command("add").action == cmd.ACTION_UNKNOWN


@pytest.mark.parametrize("alias", ["delete", "del", "remove", "rm"])
def test_parse_delete_aliases(alias):
    """delete 的别名都应可用。"""
    parsed = cmd.parse_mc_command(f"{alias} 主服")
    assert parsed.action == cmd.ACTION_DELETE
    assert parsed.name == "主服"


def test_parse_set():
    """set 应解析出名称。"""
    parsed = cmd.parse_mc_command("set 副服")
    assert parsed.action == cmd.ACTION_SET
    assert parsed.name == "副服"


def test_parse_set_without_name():
    """set 不带名称时 name 为空。"""
    parsed = cmd.parse_mc_command("set")
    assert parsed.action == cmd.ACTION_SET
    assert parsed.name == ""


@pytest.mark.parametrize("alias", ["list", "ls", "LIST", "Ls"])
def test_parse_list_aliases(alias):
    """list 的别名都应可用，且忽略大小写。"""
    assert cmd.parse_mc_command(alias).action == cmd.ACTION_LIST


@pytest.mark.parametrize("alias", ["help", "?", "-h", "--help"])
def test_parse_help_aliases(alias):
    """help 的别名都应可用。"""
    assert cmd.parse_mc_command(alias).action == cmd.ACTION_HELP


def test_parse_query_named_server():
    """非指令词应作为服务器名称查询。"""
    parsed = cmd.parse_mc_command("主服")
    assert parsed.action == cmd.ACTION_QUERY
    assert parsed.name == "主服"


def test_parse_query_name_with_spaces():
    """服务器名称允许包含空格。"""
    parsed = cmd.parse_mc_command("我的 主服")
    assert parsed.action == cmd.ACTION_QUERY
    assert parsed.name == "我的 主服"


def test_parse_query_address_like_name():
    """直接输入地址时应作为名称/地址查询。"""
    parsed = cmd.parse_mc_command("mc.example.com:25565")
    assert parsed.action == cmd.ACTION_QUERY
    assert parsed.name == "mc.example.com:25565"


def test_parse_case_insensitive_actions():
    """动作关键字应忽略大小写。"""
    assert cmd.parse_mc_command("ADD 主服 a.com").action == cmd.ACTION_ADD
    assert cmd.parse_mc_command("SET 主服").action == cmd.ACTION_SET


# ----------------------------------------------------------------------
# normalize_address
# ----------------------------------------------------------------------
def test_normalize_address_basic():
    """主机加端口应被拼接。"""
    assert cmd.normalize_address("mc.example.com", "25565") == "mc.example.com:25565"


def test_normalize_address_no_port():
    """没有端口时只返回主机。"""
    assert cmd.normalize_address("mc.example.com") == "mc.example.com"


def test_normalize_address_ipv6_brackets():
    """IPv6 应加方括号。"""
    assert cmd.normalize_address("2001:db8::1", "25565") == "[2001:db8::1]:25565"


def test_normalize_address_keeps_existing_brackets():
    """已带方括号的 IPv6 不应重复添加。"""
    assert cmd.normalize_address("[2001:db8::1]", "25565") == "[2001:db8::1]:25565"


def test_normalize_address_invalid_port_kept_as_text():
    """非数字端口应原样保留，交由后续校验拒绝。"""
    result = cmd.normalize_address("mc.example.com", "abc")
    assert "mc.example.com" in result
    assert "abc" in result


def test_normalize_address_empty_host():
    """空主机应返回空串。"""
    assert cmd.normalize_address("", "25565") == ""


# ----------------------------------------------------------------------
# 状态文本格式化
# ----------------------------------------------------------------------
def _online_status(**overrides) -> ServerStatus:
    """构造一个在线的状态对象。"""
    defaults = {
        "address": "mc.example.com:25565",
        "host": "mc.example.com",
        "port": 25565,
        "online": True,
        "latency_ms": 42.4,
        "version": "1.21.1",
        "protocol": 767,
        "motd": "A Minecraft Server",
        "players": PlayerInfo(online=3, max=20, sample=["Alice", "Bob"]),
    }
    defaults.update(overrides)
    return ServerStatus(**defaults)


def test_format_status_text_online():
    """在线状态文本应包含关键信息。"""
    text = fmt.format_status_text(_online_status(), title="主服")
    assert "主服" in text
    assert "在线" in text
    assert "1.21.1" in text
    assert "A Minecraft Server" in text
    assert "3/20" in text
    assert "Alice" in text


def test_format_status_text_hides_player_list():
    """关闭玩家名单时不应出现玩家名。"""
    text = fmt.format_status_text(
        _online_status(),
        show_player_list=False,
        title="主服",
    )
    assert "Alice" not in text
    assert "3/20" in text


def test_format_status_text_offline():
    """离线状态文本应包含失败原因。"""
    status = ServerStatus(
        address="mc.example.com:25565",
        host="mc.example.com",
        port=25565,
        online=False,
        error="连接被拒绝。",
    )
    text = fmt.format_status_text(status, title="主服")
    assert "离线" in text
    assert "连接被拒绝" in text


def test_format_status_text_latency_seconds():
    """超过一秒的延迟应以秒为单位展示。"""
    text = fmt.format_status_text(_online_status(latency_ms=2500), title="主服")
    assert "2.50 s" in text


def test_format_server_list_empty():
    """空列表应给出添加指引。"""
    text = fmt.format_server_list([])
    assert "/mc add" in text


def test_format_server_list_marks_default():
    """列表应标出默认服务器。"""
    text = fmt.format_server_list(
        [("主服", "a.example.com", True), ("副服", "b.example.com", False)],
    )
    assert "共 2 个服务器" in text
    assert "（默认）" in text
    assert "a.example.com" in text


# ----------------------------------------------------------------------
# 渲染数据
# ----------------------------------------------------------------------
def test_build_render_data_online():
    """渲染数据应包含模板需要的全部字段。"""
    data = fmt.build_render_data(_online_status(), title="主服")
    assert data["title"] == "主服"
    assert data["online"] is True
    assert data["players_online"] == 3
    assert data["players_max"] == 20
    assert data["player_sample"] == ["Alice", "Bob"]
    assert data["latency_ms"] == 42
    assert "timestamp" in data


def test_build_render_data_offline():
    """离线时也应能构造完整的渲染数据。"""
    status = ServerStatus(
        address="a:25565",
        host="a",
        port=25565,
        online=False,
        error="超时",
    )
    data = fmt.build_render_data(status, title="主服")
    assert data["online"] is False
    assert data["error"] == "超时"


def test_build_render_data_truncates_long_motd():
    """过长的 MOTD 应被截断，避免撑爆图片。"""
    data = fmt.build_render_data(_online_status(motd="x" * 1000), title="主服")
    assert len(data["motd"]) <= 400


def test_build_list_render_data():
    """列表渲染数据应把服务器放进 extra_servers。"""
    data = fmt.build_list_render_data(
        [("主服", "a.example.com", True), ("副服", "b.example.com", False)],
        session_label="本群",
    )
    assert len(data["extra_servers"]) == 2
    assert data["extra_servers"][0]["default"] is True
    assert "本群" in data["title"]


def test_load_template_reads_file():
    """模板文件应能被读取且包含关键占位符。"""
    template = fmt.load_template()
    assert "{{ title }}" in template
    assert "{{ address }}" in template
    assert "motd" in template
    assert "players_online" in template


def test_format_help_lists_all_commands():
    """帮助文本应覆盖全部子指令。"""
    text = fmt.format_help()
    for keyword in ["/mc add", "/mc delete", "/mc set", "/mc list", "/mc help"]:
        assert keyword in text
