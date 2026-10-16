"""本地渲染器的单元测试。

本地渲染是「图片输出」的默认引擎，必须保证：
    · 各种状态（在线/离线/列表/长文本/无玩家/超大数值）都能渲染出图片
    · 不抛异常、不产生空图、尺寸合理
    · 渲染速度满足「毫秒级」预期
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import _pluginpath  # noqa: F401  （把仓库根目录注册为插件包）

from astrbot_plugin_minecraft_status import formatter as fmt  # noqa: E402
from astrbot_plugin_minecraft_status.mc_protocol import (  # noqa: E402
    PlayerInfo,
    ServerStatus,
)
from astrbot_plugin_minecraft_status.renderer_local import (  # noqa: E402
    render_status_card,
    render_status_card_to_file,
)


def _online_status(**overrides) -> ServerStatus:
    """构造一个在线状态对象。"""
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


def _offline_status(error: str = "连接被拒绝。") -> ServerStatus:
    """构造一个离线状态对象。"""
    return ServerStatus(
        address="bad.example.com:25565",
        host="bad.example.com",
        port=25565,
        online=False,
        error=error,
    )


# ----------------------------------------------------------------------
# 基本渲染
# ----------------------------------------------------------------------
def test_render_online_card():
    """在线状态应渲染出尺寸合理的图片。"""
    image = render_status_card(fmt.build_render_data(_online_status(), title="主服"))
    assert image.width == 620
    assert image.height > 200
    assert image.mode == "RGB"


def test_render_offline_card():
    """离线状态应渲染出错误卡片。"""
    image = render_status_card(fmt.build_render_data(_offline_status(), title="主服"))
    assert image.width == 620
    assert image.height > 100


def test_render_list_card_has_no_online_badge():
    """列表卡片不应被误判为在线状态（不应显著高于纯列表所需高度）。"""
    image = render_status_card(
        fmt.build_list_render_data(
            [("主服", "a.example.com", True), ("副服", "b.example.com", False)],
            session_label="本群",
        ),
    )
    assert image.width == 620
    # 没有 MOTD / 进度条 / 玩家统计时应比较紧凑。
    assert image.height < 320


def test_render_card_deterministic_height():
    """相同数据应产生相同高度的图片（布局稳定）。"""
    data = fmt.build_render_data(_online_status(), title="主服")
    first = render_status_card(data)
    second = render_status_card(data)
    assert first.height == second.height


# ----------------------------------------------------------------------
# 边界情况
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "overrides",
    [
        {"motd": ""},
        {"motd": "x" * 2000},
        {"motd": "第一行\n第二行\n第三行\n第四行\n第五行"},
        {"players": PlayerInfo(online=0, max=0, sample=[])},
        {"players": PlayerInfo(online=200000, max=200000, sample=[])},
        {"players": PlayerInfo(online=1, max=20, sample=[f"p{i}" for i in range(50)])},
        {"version": ""},
        {"latency_ms": 65432.1},
        {"protocol": None},
        {"address": "a" * 300},
        {"legacy": True},
    ],
)
def test_render_handles_edge_cases(overrides):
    """各种极端数据都应能正常渲染且不抛异常。"""
    data = fmt.build_render_data(_online_status(**overrides), title="边界测试")
    image = render_status_card(data)
    assert image.width == 620
    assert image.height > 100


@pytest.mark.parametrize(
    "error",
    [
        "连接被拒绝。",
        "x" * 500,
        "第一行\n第二行\n第三行\n第四行\n第五行\n第六行",
        "",
    ],
)
def test_render_offline_edge_cases(error):
    """离线的各种错误文案都应能渲染。"""
    data = fmt.build_render_data(_offline_status(error))
    image = render_status_card(data)
    assert image.height > 80


def test_render_empty_list():
    """空列表也应能渲染。"""
    image = render_status_card(fmt.build_list_render_data([], session_label="本群"))
    assert image.width == 620


def test_render_many_servers_list():
    """大量服务器列表应被完整渲染（不崩溃）。"""
    servers = [(f"服务器{i}", f"s{i}.example.com:25565", i == 0) for i in range(30)]
    image = render_status_card(fmt.build_list_render_data(servers))
    assert image.height > 400


def test_render_handles_missing_keys():
    """数据字典缺少字段时应安全降级。"""
    image = render_status_card({})
    assert image.width == 620
    assert image.height > 50


def test_render_handles_cjk_font_fallback():
    """纯中文内容应正常渲染（依赖中文字体回退）。"""
    image = render_status_card(
        fmt.build_render_data(
            _online_status(motd="欢迎来到服务器", version="1.21 中文版"),
            title="我的世界服务器",
        ),
    )
    assert image.height > 200


# ----------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------
def test_render_to_file_creates_valid_jpeg(tmp_path):
    """应生成一个非空的合法 JPEG 文件。"""
    data = fmt.build_render_data(_online_status(), title="主服")
    path = render_status_card_to_file(data, output_dir=str(tmp_path))

    file = Path(path)
    assert file.is_file()
    assert file.name.endswith(".jpg")
    assert file.stat().st_size > 1000

    # 校验确实是可解析的 JPEG。
    header = file.read_bytes()[:3]
    assert header == b"\xff\xd8\xff"


def test_render_to_file_unique_names(tmp_path):
    """连续渲染应生成不同文件名，避免并发覆盖。"""
    data = fmt.build_render_data(_online_status(), title="主服")
    paths = {render_status_card_to_file(data, output_dir=str(tmp_path)) for _ in range(5)}
    assert len(paths) == 5


# ----------------------------------------------------------------------
# 性能
# ----------------------------------------------------------------------
def test_render_is_fast(tmp_path):
    """本地渲染必须是毫秒级：20 次渲染平均应远低于 100ms。"""
    data = fmt.build_render_data(
        _online_status(
            players=PlayerInfo(online=20971, max=200000, sample=["A", "B", "C"]),
            motd="Hypixel Network [1.8/26.3]\n    SB 0.27.2 MINISTERS",
        ),
        title="Hypixel",
    )
    # 预热：首次会加载并缓存字体。
    render_status_card(data)

    iterations = 20
    started = time.perf_counter()
    for _ in range(iterations):
        render_status_card(data)
    average_ms = (time.perf_counter() - started) / iterations * 1000

    assert average_ms < 100, f"本地渲染过慢：{average_ms:.1f} ms"
