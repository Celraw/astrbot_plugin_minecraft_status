"""使用 Pillow 在本地绘制状态卡片。

为什么需要它：AstrBot 的文转图服务是**网络**渲染（拿到图片 URL 后平台还要再
下载一次），单次通常需要数百毫秒到数秒，且会为模板注入约 1.2MB 的 Shiki 运行
时。本模块直接在进程内绘制，通常在 10ms 量级，且不依赖任何外部服务。

绘制风格与 templates/status.html 保持一致（深色卡片 + 状态徽章 + 玩家进度条）。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# 颜色（与 HTML 模板保持一致）
BG_TOP = (27, 42, 58)
BG_BOTTOM = (15, 27, 38)
CARD_BG = (33, 48, 64)
CARD_BORDER = (54, 74, 94)
TEXT_MAIN = (232, 238, 244)
TEXT_TITLE = (255, 255, 255)
TEXT_MUTED = (143, 166, 186)
ACCENT = (74, 158, 255)
ONLINE = (47, 191, 107)
OFFLINE = (217, 83, 79)
BADGE_ONLINE_BG = (31, 122, 69)
BADGE_OFFLINE_BG = (122, 35, 35)
BAR_BG = (52, 68, 84)
BAR_FILL = (47, 191, 107)
BAR_FULL = (217, 139, 31)
MOTD_BG = (22, 36, 50)
ERROR_BG = (60, 32, 36)

CARD_WIDTH = 620
PADDING = 22
MARGIN = 22
AVATAR_SIZE = 64

# 跨平台中文字体候选（按优先级）。
_FONT_CANDIDATES = (
    # 显式路径（Windows）
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    # 常见文件名（由 PIL 自行查找）
    "msyh.ttc",
    "msyhbd.ttc",
    "simhei.ttf",
    "NotoSansCJK-Regular.ttc",
    "NotoSansCJK-Bold.ttc",
    "PingFang.ttc",
    "Heiti.ttc",
    "DejaVuSans.ttf",
    "Arial.ttf",
)

# 字体缓存：避免每次渲染重复读字体文件（这是本地渲染的主要开销之一）。
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载指定字号的中文字体，带缓存。"""
    key = ("regular", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    for candidate in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size)
            _FONT_CACHE[key] = font
            return font
        except Exception:
            continue

    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _ellipsize(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> str:
    """把文本裁剪到指定宽度内，超出部分用省略号表示。"""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    if _text_width(text, font, draw) <= max_width:
        return text

    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(text[:mid] + ellipsis, font, draw) <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis if low else ellipsis


def _text_width(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    draw: ImageDraw.ImageDraw,
) -> int:
    """测量文本宽度。"""
    if not text:
        return 0
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return int(box[2] - box[0])
    except Exception:
        return int(font.getlength(text))


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
    max_lines: int = 3,
) -> list[str]:
    """把文本按宽度折行，超过最大行数时截断。"""
    text = (text or "").strip()
    if not text:
        return []

    lines: list[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            lines.append("")
            continue
        current = ""
        for char in raw_line:
            candidate = current + char
            if _text_width(candidate, font, draw) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = char
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) >= max_lines:
            break

    if not lines:
        return []

    # 若被截断，最后一行加省略号。
    joined_len = sum(len(line) for line in lines)
    if joined_len < len(text.replace("\n", "")):
        lines[-1] = _ellipsize(lines[-1] + "…", font, max_width, draw)
    return lines[:max_lines]


def _vertical_gradient(width: int, height: int) -> Image.Image:
    """生成一个竖直渐变背景。"""
    gradient = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
        )
        gradient.putpixel((0, y), color)
    return gradient.resize((width, height))


def _decode_icon(favicon: str | None, size: int) -> Image.Image | None:
    """把服务器的 favicon（data:image/png;base64,...）解码为图片。"""
    if not favicon or "base64," not in favicon:
        return None
    import base64
    import io

    try:
        raw = base64.b64decode(favicon.split("base64,", 1)[1], validate=False)
        icon = Image.open(io.BytesIO(raw)).convert("RGBA")
        return icon.resize((size, size), Image.Resampling.NEAREST)
    except Exception:
        return None


def render_status_card(data: dict[str, Any]) -> Image.Image:
    """根据模板数据绘制状态卡片。

    Args:
        data: 与 HTML 模板相同结构的数据字典。

    Returns:
        绘制完成的 PIL 图像。

    Note:
        采用「先测高、再绘制」两遍布局：第一遍用真实字体测量每块内容的高度，
        因此卡片高度始终贴合内容，不会出现空白或截断。
    """
    title = str(data.get("title") or data.get("address") or "Minecraft 服务器")
    address = str(data.get("address") or "")
    is_list = bool(data.get("is_list"))
    # 列表卡片不是服务器状态，不应显示「在线」徽章与玩家统计。
    online = bool(data.get("online")) and not is_list
    show_status = not is_list
    motd = str(data.get("motd") or "")
    version = str(data.get("version") or "")
    protocol = data.get("protocol")
    players_online = int(data.get("players_online") or 0)
    players_max = int(data.get("players_max") or 0)
    latency_ms = data.get("latency_ms")
    error = str(data.get("error") or "")
    show_players = bool(data.get("show_players", True))
    player_sample = data.get("player_sample") or []
    extra_servers = data.get("extra_servers") or []
    timestamp = str(data.get("timestamp") or "")
    icon = _decode_icon(data.get("icon"), AVATAR_SIZE)

    # 先创建一个足够高的画布做测量。
    probe = Image.new("RGB", (CARD_WIDTH, 10))
    probe_draw = ImageDraw.Draw(probe)

    font_title = _load_font(24)
    font_address = _load_font(15)
    font_badge = _load_font(16)
    font_body = _load_font(17)
    font_motd = _load_font(17)
    font_stat_key = _load_font(13)
    font_stat_val = _load_font(18)
    font_small = _load_font(14)
    font_footer = _load_font(13)

    inner_width = CARD_WIDTH - PADDING * 2
    left = PADDING
    y = PADDING

    # ---- 头部：图标 + 标题/地址 + 状态徽章 ----
    header_height = AVATAR_SIZE if icon else 52
    text_left = left + (AVATAR_SIZE + 14 if icon else 0)
    badge_text = "在线" if online else "离线"
    # 列表卡片不显示状态徽章，标题可以使用整行宽度。
    badge_width = (
        _text_width(badge_text, font_badge, probe_draw) + 28 if show_status else 0
    )
    badge_height = 32

    text_area_width = inner_width - (AVATAR_SIZE + 14 if icon else 0) - badge_width - 12
    title_line = _ellipsize(title, font_title, max(40, text_area_width), probe_draw)
    address_line = _ellipsize(
        address, font_address, max(40, text_area_width), probe_draw
    )

    y += header_height + 18

    # ---- MOTD 区 ----
    motd_lines: list[str] = []
    motd_block_height = 0
    if online:
        motd_font = font_motd
        motd_lines = _wrap_text(motd, motd_font, inner_width - 24, probe_draw, max_lines=3)
        if not motd_lines:
            motd_lines = ["（该服务器未设置 MOTD）"]
        line_height = 25
        motd_block_height = len(motd_lines) * line_height + 20
        y += motd_block_height + 16

    # ---- 玩家进度条 ----
    bar_height = 9
    if online:
        y += bar_height + 16

    # ---- 统计区 ----
    stats: list[tuple[str, str]] = []
    if online:
        stats.append(("玩家", f"{players_online} / {players_max}"))
        stats.append(("版本", version or "未知"))
        if latency_ms is not None:
            stats.append(("延迟", f"{int(latency_ms)} ms"))
        if protocol is not None:
            stats.append(("协议号", str(protocol)))

    stat_box_height = 58
    stats_rows = [stats[i : i + 2] for i in range(0, len(stats), 2)]
    stats_height = len(stats_rows) * (stat_box_height + 10)
    if stats_height:
        y += stats_height + 6

    # ---- 玩家名单 ----
    player_lines: list[str] = []
    if online and show_players and player_sample:
        shown = [str(p) for p in player_sample[:6]]
        player_lines = shown
        if len(player_sample) > 6:
            player_lines.append(f"…等共 {len(player_sample)} 人")
        y += 12 + 24 + len(player_lines) * 22 + 10

    # ---- 错误信息 ----
    error_lines: list[str] = []
    if not online and error:
        error_lines = _wrap_text(error, font_body, inner_width - 24, probe_draw, 4)
        y += len(error_lines) * 24 + 22 + 14

    # ---- 服务器列表 ----
    list_rows = len(extra_servers)
    if list_rows:
        y += 12 + 24 + list_rows * 26 + 10

    # ---- 页脚 ----
    y += 16 + 24

    card_height = y + PADDING

    # ---- 正式绘制 ----
    image = _vertical_gradient(CARD_WIDTH, card_height)
    draw = ImageDraw.Draw(image)

    card_box = (MARGIN, MARGIN, CARD_WIDTH - MARGIN, card_height - MARGIN)
    draw.rounded_rectangle(card_box, radius=14, fill=CARD_BG, outline=CARD_BORDER, width=1)

    y = MARGIN + PADDING
    left = MARGIN + PADDING
    inner_width = CARD_WIDTH - MARGIN * 2 - PADDING * 2

    # 头部
    if icon:
        image.paste(icon, (left, y), icon)
    text_left = left + (AVATAR_SIZE + 14 if icon else 0)
    text_area_width = (
        inner_width - (AVATAR_SIZE + 14 if icon else 0) - badge_width - 12
    )
    draw.text(
        (text_left, y + 2),
        _ellipsize(title, font_title, max(40, text_area_width), draw),
        font=font_title,
        fill=TEXT_TITLE,
    )
    draw.text(
        (text_left, y + 34),
        _ellipsize(address, font_address, max(40, text_area_width), draw),
        font=font_address,
        fill=TEXT_MUTED,
    )

    # 状态徽章（列表卡片不显示）
    if show_status:
        badge_x = CARD_WIDTH - MARGIN - PADDING - badge_width
        badge_y = y + 6
        draw.rounded_rectangle(
            (badge_x, badge_y, badge_x + badge_width, badge_y + badge_height),
            radius=16,
            fill=BADGE_ONLINE_BG if online else BADGE_OFFLINE_BG,
            outline=ONLINE if online else OFFLINE,
            width=1,
        )
        draw.text(
            (badge_x + 14, badge_y + 7),
            badge_text,
            font=font_badge,
            fill=(214, 255, 231) if online else (255, 222, 222),
        )

    y += header_height + 18

    # MOTD
    if online:
        block = (left, y, CARD_WIDTH - MARGIN - PADDING, y + motd_block_height)
        draw.rounded_rectangle(block, radius=10, fill=MOTD_BG)
        draw.rectangle((left, y + 6, left + 3, y + motd_block_height - 6), fill=ACCENT)
        empty = motd_lines == ["（该服务器未设置 MOTD）"]
        text_y = y + 10
        for line in motd_lines:
            draw.text(
                (left + 14, text_y),
                line,
                font=font_motd,
                fill=TEXT_MUTED if empty else TEXT_MAIN,
            )
            text_y += 25
        y += motd_block_height + 16

    # 玩家进度条
    if online:
        draw.rounded_rectangle(
            (left, y, CARD_WIDTH - MARGIN - PADDING, y + bar_height),
            radius=5,
            fill=BAR_BG,
        )
        ratio = min(1.0, players_online / players_max) if players_max else 0.0
        fill_width = int((CARD_WIDTH - MARGIN * 2 - PADDING * 2) * ratio)
        if fill_width > 2:
            color = BAR_FULL if players_max and players_online >= players_max else BAR_FILL
            draw.rounded_rectangle(
                (left, y, left + fill_width, y + bar_height),
                radius=5,
                fill=color,
            )
        y += bar_height + 16

    # 统计
    if stats_rows:
        box_gap = 10
        box_width = (inner_width - box_gap) // 2
        for row_index, row in enumerate(stats_rows):
            row_y = y + row_index * (stat_box_height + box_gap)
            for col_index, (key, value) in enumerate(row):
                x0 = left + col_index * (box_width + box_gap)
                x1 = x0 + box_width
                draw.rounded_rectangle(
                    (x0, row_y, x1, row_y + stat_box_height),
                    radius=10,
                    fill=(40, 56, 73),
                    outline=CARD_BORDER,
                    width=1,
                )
                draw.text((x0 + 12, row_y + 9), key, font=font_stat_key, fill=TEXT_MUTED)
                draw.text(
                    (x0 + 12, row_y + 27),
                    _ellipsize(value, font_stat_val, box_width - 24, draw),
                    font=font_stat_val,
                    fill=TEXT_TITLE,
                )
        y += stats_height + 6

    # 玩家名单
    if online and show_players and player_lines:
        y += 12
        draw.text(
            (left, y),
            f"在线玩家（{len(player_sample)}）",
            font=font_small,
            fill=TEXT_MUTED,
        )
        y += 24
        for name in player_lines:
            draw.text(
                (left + 4, y),
                _ellipsize(name, font_small, inner_width - 8, draw),
                font=font_small,
                fill=TEXT_MAIN,
            )
            y += 22
        y += 10

    # 错误信息
    if not online and error:
        block_height = len(error_lines) * 24 + 22
        draw.rounded_rectangle(
            (left, y, CARD_WIDTH - MARGIN - PADDING, y + block_height),
            radius=10,
            fill=ERROR_BG,
        )
        draw.rectangle((left, y + 6, left + 3, y + block_height - 6), fill=OFFLINE)
        text_y = y + 11
        for line in error_lines:
            draw.text((left + 14, text_y), line, font=font_body, fill=(255, 214, 214))
            text_y += 24
        y += block_height + 14

    # 服务器列表
    if extra_servers:
        y += 12
        draw.line((left, y - 6, CARD_WIDTH - MARGIN - PADDING, y - 6), fill=CARD_BORDER)
        draw.text((left, y), "本群服务器", font=font_small, fill=TEXT_MUTED)
        y += 24
        for server in extra_servers:
            name = str(server.get("name") or "")
            addr = str(server.get("address") or "")
            is_default = bool(server.get("default"))
            draw.text(
                (left, y),
                _ellipsize(name, font_small, 160, draw),
                font=font_small,
                fill=TEXT_TITLE,
            )
            name_width = min(_text_width(name, font_small, draw), 160)
            if is_default:
                tag_x = left + name_width + 8
                tag_w = _text_width("默认", font_stat_key, draw) + 12
                draw.rounded_rectangle(
                    (tag_x, y - 2, tag_x + tag_w, y + 18),
                    radius=4,
                    fill=(126, 226, 168),
                )
                draw.text(
                    (tag_x + 6, y + 1),
                    "默认",
                    font=font_stat_key,
                    fill=(15, 27, 38),
                )
            addr_text = _ellipsize(addr, font_small, inner_width // 2, draw)
            addr_width = _text_width(addr_text, font_small, draw)
            draw.text(
                (CARD_WIDTH - MARGIN - PADDING - addr_width, y),
                addr_text,
                font=font_small,
                fill=TEXT_MUTED,
            )
            y += 26
        y += 10

    # 页脚
    y += 6
    draw.line((left, y, CARD_WIDTH - MARGIN - PADDING, y), fill=CARD_BORDER)
    y += 10
    draw.text(
        (left, y),
        "Minecraft 服务器状态 · 本地渲染",
        font=font_footer,
        fill=(108, 128, 147),
    )
    if timestamp:
        ts_width = _text_width(timestamp, font_footer, draw)
        draw.text(
            (CARD_WIDTH - MARGIN - PADDING - ts_width, y),
            timestamp,
            font=font_footer,
            fill=(108, 128, 147),
        )

    return image


def render_status_card_to_file(
    data: dict[str, Any],
    *,
    output_dir: str | None = None,
    quality: int = 88,
) -> str:
    """绘制卡片并保存为 JPEG 文件。

    Args:
        data: 模板数据。
        output_dir: 输出目录；为空时使用系统临时目录。
        quality: JPEG 质量（1-95）。

    Returns:
        生成的图片文件绝对路径。
    """
    image = render_status_card(data)

    if output_dir:
        directory = Path(output_dir)
    else:
        import tempfile

        directory = Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)

    filename = f"mc_status_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
    path = directory / filename
    image.save(path, format="JPEG", quality=quality, optimize=True)
    return str(path)
