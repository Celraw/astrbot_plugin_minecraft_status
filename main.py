"""AstrBot 插件：Minecraft 服务器状态查询。

功能：
    · /mc                                 查询本群默认服务器
    · /mc <名称>                           查询指定服务器
    · /mc add <名称> <IP/域名:端口>         添加服务器（端口留空默认 25565）
    · /mc delete <名称>                    删除服务器
    · /mc set <名称>                       设置默认服务器
    · /mc list                            列出本群添加的所有服务器
    · /mc help                            显示帮助

特性：
    · 每个群聊的服务器数据完全隔离（私聊按会话隔离）。
    · 可在插件配置中选择以文字或渲染图片的方式输出。
    · 本地直连 Minecraft SLP 协议，不依赖任何第三方 API。
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .commands import (
    ACTION_ADD,
    ACTION_DELETE,
    ACTION_HELP,
    ACTION_LIST,
    ACTION_QUERY,
    ACTION_QUERY_DEFAULT,
    ACTION_SET,
    ACTION_UNKNOWN,
    ParsedCommand,
    parse_mc_command,
)
from .formatter import (
    build_list_render_data,
    build_render_data,
    format_help,
    format_server_list,
    format_status_text,
    load_template,
)
from .mc_protocol import DEFAULT_PORT, ServerStatus, is_valid_address, query_server
from .renderer_local import render_status_card_to_file
from .store import ServerStore, StoreError, build_session_key

PLUGIN_NAME = "astrbot_plugin_minecraft_status"
KV_KEY = "servers"

MAX_CONCURRENT_QUERIES = 4
"""同时进行的最大查询数量，避免大量请求拖垮机器人。"""


class MinecraftStatusPlugin(Star):
    """Minecraft 服务器状态查询插件。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store = ServerStore(
            loader=self._load_store,
            saver=self._save_store,
        )
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_QUERIES)
        self._template: str | None = None

    # ------------------------------------------------------------------
    # 生命周期与持久化
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件启用时预加载数据与模板。"""
        await self.store.load()
        self._get_template()

    def _get_template(self) -> str | None:
        """获取 HTML 模板，首次访问时懒加载并缓存。

        懒加载保证即使 AstrBot 未调用 ``initialize()``（例如直接实例化），
        图片模式依然可用。
        """
        if self._template is None:
            try:
                self._template = load_template()
            except Exception as exc:
                logger.error(f"[mc] 加载 HTML 模板失败，将回退为纯文本输出：{exc}")
                self._template = ""
        return self._template or None

    async def _load_store(self) -> Any:
        """从 AstrBot 的插件 KV 存储中读取数据。"""
        try:
            return await self.get_kv_data(KV_KEY, {})
        except Exception as exc:
            logger.error(f"[mc] 读取服务器数据失败：{exc}")
            return {}

    async def _save_store(self, data: dict[str, Any]) -> None:
        """把数据写入 AstrBot 的插件 KV 存储。"""
        try:
            await self.put_kv_data(KV_KEY, data)
        except Exception as exc:
            logger.error(f"[mc] 保存服务器数据失败：{exc}")

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        """安全地读取插件配置。"""
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def output_mode(self) -> str:
        """输出方式：text 或 image。"""
        mode = str(self._cfg("output_mode", "text")).strip().lower()
        return "image" if mode == "image" else "text"

    @property
    def render_engine(self) -> str:
        """图片渲染方式：local（内置，毫秒级）或 html（AstrBot 文转图服务）。"""
        engine = str(self._cfg("render_engine", "local")).strip().lower()
        return "html" if engine == "html" else "local"

    @property
    def query_timeout(self) -> float:
        """查询超时时间（秒）。"""
        try:
            timeout = float(self._cfg("query_timeout", 5))
        except (TypeError, ValueError):
            timeout = 5.0
        return max(1.0, min(timeout, 60.0))

    @property
    def allow_srv(self) -> bool:
        """是否启用 SRV 记录解析。"""
        return bool(self._cfg("allow_srv_lookup", True))

    @property
    def show_player_list(self) -> bool:
        """是否显示在线玩家名单。"""
        return bool(self._cfg("show_player_list", True))

    @property
    def max_servers(self) -> int:
        """每个会话允许的最大服务器数量。"""
        try:
            value = int(self._cfg("max_servers_per_session", 10))
        except (TypeError, ValueError):
            value = 10
        return max(1, min(value, 100))

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    async def _render_or_text(
        self,
        event: AstrMessageEvent,
        text: str,
        data: dict[str, Any],
    ):
        """按配置输出文字或渲染图片。

        渲染失败时自动回退为纯文本，保证用户至少能拿到结果。

        Args:
            event: 消息事件。
            text: 纯文本内容（回退时使用）。
            data: 模板数据。

        Returns:
            一个 MessageEventResult。
        """
        if self.output_mode != "image":
            return event.plain_result(text)

        if self.render_engine == "local":
            result = await self._render_local(event, text, data)
            if result is not None:
                return result
            # 本地渲染失败时，尝试 AstrBot 的文转图服务兜底。
            logger.warning("[mc] 本地渲染失败，尝试使用 AstrBot 文转图服务。")

        return await self._render_html(event, text, data)

    async def _render_local(self, event: AstrMessageEvent, text: str, data: dict):
        """使用内置的本地渲染器绘制图片（毫秒级，不依赖外部服务）。"""
        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(
                None,
                lambda: render_status_card_to_file(data),
            )
        except Exception as exc:
            logger.error(f"[mc] 本地渲染图片失败：{exc}")
            return None

        # 让 AstrBot 在事件结束时自动清理临时图片，避免长期运行堆积文件。
        tracker = getattr(event, "track_temporary_local_file", None)
        if callable(tracker):
            try:
                tracker(path)
            except Exception:
                pass
        return event.image_result(path)

    async def _render_html(self, event: AstrMessageEvent, text: str, data: dict):
        """使用 AstrBot 的文转图服务渲染 HTML 卡片。

        Note:
            请求渲染服务时要求直接返回图片文件（``return_url=False``），
            避免平台适配器再按 URL 下载一次图片，可省掉一次网络往返。
        """
        template = self._get_template()
        if not template:
            return event.plain_result(text)

        try:
            path = await self.html_render(template, data, return_url=False)
            if path:
                tracker = getattr(event, "track_temporary_local_file", None)
                if callable(tracker):
                    try:
                        tracker(path)
                    except Exception:
                        pass
                return event.image_result(path)
            logger.warning("[mc] 文转图未返回图片地址，回退为纯文本。")
        except Exception as exc:
            logger.error(f"[mc] 渲染图片失败，回退为纯文本：{exc}")
        return event.plain_result(text)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    async def _do_query(self, address: str) -> ServerStatus:
        """带并发限制地查询一个服务器。"""
        async with self._semaphore:
            return await query_server(
                address,
                timeout=self.query_timeout,
                allow_srv=self.allow_srv,
                default_port=DEFAULT_PORT,
            )

    def _session_key(self, event: AstrMessageEvent) -> str:
        """获取当前会话的隔离标识。"""
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = ""
        try:
            session_id = event.get_session_id()
        except Exception:
            session_id = ""
        return build_session_key(group_id, session_id)

    async def _query_and_reply(
        self,
        event: AstrMessageEvent,
        name: str,
        address: str,
    ):
        """查询指定服务器并构造回复。"""
        status = await self._do_query(address)
        text = format_status_text(
            status,
            show_player_list=self.show_player_list,
            title=name,
        )
        data = build_render_data(
            status,
            title=name,
            show_player_list=self.show_player_list,
        )
        return await self._render_or_text(event, text, data)

    async def _reply_list(self, event: AstrMessageEvent, session_key: str):
        """回复服务器列表。"""
        servers = await self.store.list_servers(session_key)
        # 群聊显示「本群」，私聊显示「本会话」，避免措辞错位。
        label = "本群" if session_key.startswith("group:") else "本会话"
        text = format_server_list(servers, session_label=label)
        data = build_list_render_data(servers, session_label=label)
        return await self._render_or_text(event, text, data)

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------
    @filter.command("mc")
    async def mc(self, event: AstrMessageEvent, args: GreedyStr):
        """查询 Minecraft 服务器状态。

        用法：/mc [add|delete|set|list|help|<服务器名称>]
        """
        parsed = parse_mc_command(str(args or ""))
        session_key = self._session_key(event)

        try:
            return await self._dispatch(event, session_key, parsed)
        except StoreError as exc:
            return event.plain_result(f"❌ {exc}")
        except Exception as exc:
            logger.error(f"[mc] 处理指令失败：{exc}\n{traceback.format_exc()}")
            return event.plain_result(f"❌ 处理指令时发生错误：{exc}")

    async def _dispatch(
        self,
        event: AstrMessageEvent,
        session_key: str,
        parsed: ParsedCommand,
    ):
        """根据解析结果执行对应动作。"""
        action = parsed.action

        if action == ACTION_HELP or action == ACTION_UNKNOWN:
            return event.plain_result(format_help())

        if action == ACTION_LIST:
            return await self._reply_list(event, session_key)

        if action == ACTION_ADD:
            if not parsed.name:
                return event.plain_result(
                    "❌ 缺少服务器名称。用法：/mc add <名称> <IP/域名:端口>",
                )
            if not parsed.address:
                return event.plain_result(
                    "❌ 缺少服务器地址。用法：/mc add <名称> <IP/域名:端口>\n"
                    "端口可留空，默认使用 25565。",
                )
            if not is_valid_address(parsed.address):
                return event.plain_result(
                    f"❌ 地址「{parsed.address}」格式不合法。\n"
                    "示例：mc.example.com 或 mc.example.com:25565",
                )

            saved_name = await self.store.add_server(
                session_key,
                parsed.name,
                parsed.address,
                max_servers=self.max_servers,
            )
            default = await self.store.get_default(session_key)
            hint = ""
            if default and default[0] == saved_name:
                hint = "\n它已成为本群的默认服务器，可直接使用 /mc 查询。"
            return event.plain_result(
                f"✅ 已添加服务器「{saved_name}」\n地址：{parsed.address}{hint}",
            )

        if action == ACTION_DELETE:
            if not parsed.name:
                return event.plain_result(
                    "❌ 请指定要删除的服务器名称。用法：/mc delete <名称>",
                )
            deleted = await self.store.delete_server(session_key, parsed.name)
            default = await self.store.get_default(session_key)
            hint = ""
            if default:
                hint = f"\n当前默认服务器：{default[0]}"
            else:
                hint = "\n本群已没有服务器，默认服务器已清空。"
            return event.plain_result(f"✅ 已删除服务器「{deleted}」{hint}")

        if action == ACTION_SET:
            if not parsed.name:
                return event.plain_result(
                    "❌ 请指定要设为默认的服务器名称。用法：/mc set <名称>",
                )
            name = await self.store.set_default(session_key, parsed.name)
            address = await self.store.get_server(session_key, name)
            addr_text = address[1] if address else ""
            return event.plain_result(
                f"✅ 已将「{name}」设为默认服务器\n"
                f"地址：{addr_text}\n以后直接发送 /mc 即可查询。",
            )

        if action == ACTION_QUERY_DEFAULT:
            default = await self.store.get_default(session_key)
            if not default:
                servers = await self.store.list_servers(session_key)
                if servers:
                    return event.plain_result(
                        "❌ 本群尚未设置默认服务器。\n"
                        "使用 /mc set <名称> 设置，或用 /mc list 查看已有服务器。",
                    )
                return event.plain_result(
                    "❌ 本群还没有添加任何服务器。\n"
                    "使用 /mc add <名称> <IP/域名:端口> 添加一个服务器。\n"
                    "例如：/mc add 主服 mc.example.com:25565",
                )
            name, address = default
            return await self._query_and_reply(event, name, address)

        if action == ACTION_QUERY:
            target = (parsed.name or "").strip()
            if not target:
                return event.plain_result(format_help())

            found = await self.store.get_server(session_key, target)
            if found:
                name, address = found
                return await self._query_and_reply(event, name, address)

            # 名称不存在时，如果输入本身像一个地址，则直接查询，方便临时排障。
            if is_valid_address(target) and ("." in target or ":" in target):
                return await self._query_and_reply(event, target, target)

            servers = await self.store.list_servers(session_key)
            if servers:
                names = "、".join(name for name, _, _ in servers)
                return event.plain_result(
                    f"❌ 未找到服务器「{target}」。\n"
                    f"本群已添加：{names}\n"
                    "使用 /mc list 查看详情，或 /mc add 添加新服务器。",
                )
            return event.plain_result(
                f"❌ 未找到服务器「{target}」，本群还没有添加任何服务器。\n"
                "使用 /mc add <名称> <IP/域名:端口> 添加。",
            )

        return event.plain_result(format_help())

    async def terminate(self) -> None:
        """插件被禁用或重载时的清理钩子。"""
        self._template = None
