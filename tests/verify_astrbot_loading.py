"""在真实 AstrBot 环境下校验插件的可装载性。

这个脚本不做单元测试，而是直接调用 AstrBot 自己的校验逻辑来回答
「这个插件能不能被 AstrBot 正常加载」：

    1. metadata.yaml 是否通过 ``_PluginUpdater.validate_plugin_metadata``
    2. _conf_schema.json 是否能被 ``PluginManager._load_plugin_config_schema``
       解析，并通过 ``AstrBotConfig`` 生成默认配置
    3. 插件类是否被 ``@filter.command("mc")`` 正确注册，且指令过滤器能匹配
       各种 ``/mc`` 写法
    4. GreedyStr 参数能否把剩余文本完整交给处理器
    5. 图片/文字两种输出模式都能产出可发送的消息链

用法（需要 AstrBot 的 Python 环境）：
    python tests/verify_astrbot_loading.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 仓库采用 AstrBot 约定的平铺结构：插件文件就在仓库根目录。
PLUGIN_DIR = PROJECT_ROOT
TESTS_DIR = Path(__file__).resolve().parent

# AstrBot 默认把数据目录放在「当前工作目录」下的 data/。
# 这里显式把 ASTRBOT_ROOT 指向临时目录，避免在校验时污染项目目录。
if not os.environ.get("ASTRBOT_ROOT"):
    os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="astrbot-verify-root-")

sys.path.insert(0, str(TESTS_DIR))

# 复用测试里的假事件对象；先安装极简 pytest 兼容层，
# 以便在没有 pytest 的环境下也能导入测试模块。
import minipytest  # noqa: E402

minipytest.install()

import _pluginpath  # noqa: E402,F401  （把仓库根目录注册为插件包）

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    """记录一条校验结果。"""
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  [ OK ] {label}")
    else:
        print(f"  [FAIL] {label}" + (f" -> {detail}" if detail else ""))
        FAILURES.append(label)


def section(title: str) -> None:
    """打印一个小节标题。"""
    print(f"\n{title}")


def main() -> int:
    """执行全部校验。"""
    print("=" * 72)
    print("AstrBot 插件装载校验")
    print("=" * 72)

    try:
        import astrbot
        from astrbot.core.config.astrbot_config import AstrBotConfig
        from astrbot.core.star.star_handler import star_handlers_registry
        from astrbot.core.star.updater import _PluginUpdater
        from astrbot.core.star.filter.command import CommandFilter, GreedyStr
        from astrbot.core.star.star_manager import PluginManager
    except ImportError as exc:
        print(f"无法导入 AstrBot（{exc}）；请设置 PYTHONPATH 指向 AstrBot 源码目录。")
        return 2

    print(f"AstrBot 版本：{astrbot.__version__}")

    # ------------------------------------------------------------------
    # 1. metadata.yaml
    # ------------------------------------------------------------------
    section("1. metadata.yaml 校验")
    import yaml

    metadata_path = PLUGIN_DIR / "metadata.yaml"
    check(metadata_path.exists(), "metadata.yaml 存在")
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    try:
        _PluginUpdater.validate_plugin_metadata(metadata, "metadata.yaml")
        check(True, "通过 AstrBot 的元数据校验（name/desc/version/author 齐全）")
    except ValueError as exc:
        check(False, "通过 AstrBot 的元数据校验", str(exc))

    check(
        metadata.get("name") == "astrbot_plugin_minecraft_status",
        "插件名与目录名一致（可被 importlib 加载）",
        str(metadata.get("name")),
    )
    check(
        isinstance(metadata.get("astrbot_version"), str),
        "声明了 astrbot_version 版本范围",
    )

    # 新增/未知字段不应导致加载失败。
    allowed_extra = {"display_name", "short_desc", "repo", "support_platforms"}
    unknown = set(metadata) - {
        "name", "desc", "author", "version", "astrbot_version",
    } - allowed_extra
    check(not unknown, "没有会触发元数据校验失败的未知字段", str(unknown))

    # ------------------------------------------------------------------
    # 2. _conf_schema.json
    # ------------------------------------------------------------------
    section("2. _conf_schema.json 校验")
    schema_path = PLUGIN_DIR / "_conf_schema.json"
    check(schema_path.exists(), "_conf_schema.json 存在")

    try:
        schema = PluginManager._load_plugin_config_schema(str(schema_path))
        check(True, "通过 AstrBot 的 schema 加载（UTF-8 合法 JSON）")
    except Exception as exc:
        schema = {}
        check(False, "通过 AstrBot 的 schema 加载", str(exc))

    # 每个 type 都必须是 AstrBot 支持的类型。
    from astrbot.core.config.default import DEFAULT_VALUE_MAP

    bad_types = [
        (key, spec.get("type"))
        for key, spec in schema.items()
        if spec.get("type") not in DEFAULT_VALUE_MAP
    ]
    check(not bad_types, "所有配置项类型均为 AstrBot 支持的类型", str(bad_types))

    # 用真实配置类生成默认配置，验证能读能写。
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "test_config.json")
        try:
            config = AstrBotConfig(config_path=cfg_path, schema=schema)
            check(True, "AstrBotConfig 能根据 schema 生成默认配置")
        except Exception as exc:
            config = {}
            check(False, "AstrBotConfig 能根据 schema 生成默认配置", str(exc))

        check(
            config.get("output_mode") in ("text", "image"),
            "output_mode 默认值合法",
            str(config.get("output_mode")),
        )
        check(
            isinstance(config.get("query_timeout"), int),
            "query_timeout 是整数",
            str(config.get("query_timeout")),
        )
        check(
            isinstance(config.get("allow_srv_lookup"), bool),
            "allow_srv_lookup 是布尔值",
            str(config.get("allow_srv_lookup")),
        )
        check(
            config.get("output_mode") == "text",
            "默认以文字方式输出",
            str(config.get("output_mode")),
        )
        check(
            config.get("render_engine") == "local",
            "默认使用本地渲染引擎（毫秒级）",
            str(config.get("render_engine")),
        )
        check(
            schema.get("render_engine", {}).get("options") == ["local", "html"],
            "render_engine 提供 local / html 两个选项",
            str(schema.get("render_engine", {}).get("options")),
        )

    # ------------------------------------------------------------------
    # 3. 插件类与指令注册
    # ------------------------------------------------------------------
    section("3. 插件类与指令注册")
    from astrbot_plugin_minecraft_status.main import MinecraftStatusPlugin
    from astrbot.api.star import Star

    check(
        issubclass(MinecraftStatusPlugin, Star),
        "插件类继承 astrbot.api.star.Star",
    )

    module_path = MinecraftStatusPlugin.__module__
    handlers = [
        h for h in star_handlers_registry
        if h.handler_module_path == module_path
    ]
    check(bool(handlers), "指令处理器已注册到 AstrBot 的 handler 注册表")

    mc_handler = next(
        (h for h in handlers if h.handler_name == "mc"),
        None,
    )
    check(mc_handler is not None, "找到了名为 mc 的指令处理器")

    if mc_handler is not None:
        command_filters = [
            f for f in mc_handler.event_filters if isinstance(f, CommandFilter)
        ]
        check(
            bool(command_filters),
            "mc 处理器挂载了 CommandFilter",
        )
        if command_filters:
            names = command_filters[0].get_complete_command_names()
            check("mc" in names, "指令名称为 mc", str(names))

        # 参数应当被解析为 GreedyStr（完整接收剩余文本）。
        check(
            "args" in command_filters[0].handler_params,
            "处理器带有 args 参数",
            str(command_filters[0].handler_params),
        )
        check(
            command_filters[0].handler_params.get("args") is GreedyStr
            or command_filters[0].handler_params.get("args") == "",
            "args 使用 GreedyStr 接收剩余全部文本",
            str(command_filters[0].handler_params.get("args")),
        )

    # ------------------------------------------------------------------
    # 4. 指令过滤器实际匹配
    # ------------------------------------------------------------------
    section("4. 指令过滤器匹配各种写法")
    from test_plugin_integration import FakeEvent

    cmd_filter = next(
        (f for f in (mc_handler.event_filters if mc_handler else [])
         if isinstance(f, CommandFilter)),
        None,
    )

    if cmd_filter is not None:
        class _Cfg(dict):
            """供过滤器使用的假配置。"""

        session_cfg = _Cfg()

        def matches(message: str) -> bool:
            """模拟 AstrBot 的唤醒前缀剥离后再执行指令过滤器。

            WakingCheckStage 会先把 `/`（默认 wake_prefix）从消息中移除，
            因此真正到达 CommandFilter 的是去掉前缀后的文本。
            """
            stripped = message[1:].strip() if message.startswith("/") else message
            event = FakeEvent(stripped)
            event.is_at_or_wake_command = True
            return cmd_filter.filter(event, session_cfg)

        for message in [
            "/mc",
            "/mc add 主服 mc.example.com",
            "/mc list",
            "/mc delete 主服",
            "/mc set 主服",
            "/mc 主服",
        ]:
            check(matches(message), f"匹配 {message}")

        check(not matches("/mcx"), "不匹配 /mcx（避免前缀误伤）")

        # 没有唤醒前缀时，WakingCheckStage 不会设置 is_at_or_wake_command，
        # 因此指令过滤器必须拒绝该消息（否则会误触发插件）。
        no_wake_event = FakeEvent("mc")
        no_wake_event.is_at_or_wake_command = False
        check(
            not cmd_filter.filter(no_wake_event, session_cfg),
            "未通过唤醒前缀/@ 唤醒时不触发指令",
        )

        # 验证 GreedyStr 把剩余文本完整传递。
        event = FakeEvent("mc add 主服 mc.example.com:25565")
        event.is_at_or_wake_command = True
        cmd_filter.filter(event, session_cfg)
        params = event.get_extra("parsed_params") or {}
        check(
            params.get("args") == "add 主服 mc.example.com:25565",
            "GreedyStr 完整保留了参数原文",
            str(params.get("args")),
        )
    else:
        check(False, "找到 mc 的 CommandFilter")

    # ------------------------------------------------------------------
    # 5. 输出模式
    # ------------------------------------------------------------------
    section("5. 文字 / 图片两种输出模式")

    from astrbot_plugin_minecraft_status.main import KV_KEY

    async def build_plugin(mode: str, engine: str = "local"):
        """构造一个使用内存存储的插件实例。"""
        instance = MinecraftStatusPlugin(
            context=None,
            config={
                "output_mode": mode,
                "render_engine": engine,
                "query_timeout": 5,
            },
        )
        storage: dict = {}

        async def load():
            return storage.get(KV_KEY, {})

        async def save(data):
            storage[KV_KEY] = data

        instance.store = type(instance.store)(loader=load, saver=save)
        return instance

    async def run_modes() -> None:
        import astrbot_plugin_minecraft_status.main as main_module
        from astrbot.core.message.components import ComponentType

        # 文字模式
        text_plugin = await build_plugin("text")
        await text_plugin.initialize()
        await text_plugin.mc(
            FakeEvent("/mc add 主服 a.example.com"),
            "add 主服 a.example.com",
        )
        text_result = await text_plugin.mc(FakeEvent("/mc list"), "list")
        check(
            bool(text_result.get_plain_text().strip()),
            "文字模式返回纯文本消息",
        )
        check(
            all(c.type == ComponentType.Plain for c in text_result.chain),
            "文字模式消息链只包含 Plain 组件",
        )

        # 图片模式 - 本地渲染引擎（默认，毫秒级，不依赖外部服务）
        local_plugin = await build_plugin("image")
        await local_plugin.initialize()
        check(local_plugin.render_engine == "local", "默认使用本地渲染引擎")
        await local_plugin.mc(
            FakeEvent("/mc add 主服 a.example.com"),
            "add 主服 a.example.com",
        )
        local_result = await local_plugin.mc(FakeEvent("/mc list"), "list")
        check(
            any(c.type == ComponentType.Image for c in local_result.chain),
            "本地渲染引擎返回 Image 组件",
        )
        local_files = [
            c.path for c in local_result.chain if c.type == ComponentType.Image
        ]
        check(
            all(Path(p).is_file() for p in local_files if p),
            "本地渲染产出真实图片文件",
        )

        # 图片模式 - HTML 渲染引擎（走 AstrBot 文转图服务）
        image_plugin = await build_plugin("image", engine="html")
        await image_plugin.initialize()
        check(
            image_plugin._template is not None,
            "HTML 模板加载成功",
        )

        from astrbot_plugin_minecraft_status.renderer_local import (
            render_status_card_to_file,
        )

        placeholder = render_status_card_to_file({"title": "占位"})
        captured: dict = {}

        async def fake_render(tmpl, data, return_url=True, options=None):
            # 确认渲染数据里包含模板需要的字段。
            assert "title" in data and "address" in data, data.keys()
            captured["return_url"] = return_url
            return placeholder

        image_plugin.html_render = fake_render  # type: ignore[method-assign]
        await image_plugin.mc(
            FakeEvent("/mc add 主服 a.example.com"),
            "add 主服 a.example.com",
        )
        image_result = await image_plugin.mc(FakeEvent("/mc list"), "list")
        check(
            any(c.type == ComponentType.Image for c in image_result.chain),
            "HTML 渲染引擎返回 Image 组件",
        )
        check(
            captured.get("return_url") is False,
            "HTML 渲染要求直接返回图片文件（省掉一次下载往返）",
        )

        # 两个引擎都失败时回退文字
        async def boom(tmpl, data, return_url=True, options=None):
            raise RuntimeError("渲染服务挂了")

        def local_boom(_data):
            raise RuntimeError("本地渲染挂了")

        image_plugin.html_render = boom  # type: ignore[method-assign]
        original = main_module.render_status_card_to_file
        main_module.render_status_card_to_file = local_boom  # type: ignore[assignment]
        try:
            fallback = await image_plugin.mc(FakeEvent("/mc list"), "list")
        finally:
            main_module.render_status_card_to_file = original  # type: ignore[assignment]
        check(
            bool(fallback.get_plain_text().strip()),
            "两条渲染路径都失败时自动回退为纯文本",
        )

    asyncio.run(run_modes())

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    if FAILURES:
        print(f"校验未通过：{len(FAILURES)}/{CHECKS} 项失败")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{CHECKS}/{CHECKS} 项校验成功")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
