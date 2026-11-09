"""与现有卡片同款的环形开箱统计图。"""

import io
import math

from PIL import Image, ImageDraw

from .atlas_card import DOT_COLORS, RARITIES, RARITY_LABELS
from .render_common import (
    BG,
    BROWN,
    SOFT,
    WIDTH,
    Header,
    background,
    draw_text,
    ellipsize,
    footer,
    header,
    heading,
    runs,
    s,
    width_of,
)
from .statistics import ChestStatistics

PANEL = (255, 251, 242)
BORDER = (236, 210, 174)


def panel(draw: ImageDraw.ImageDraw, top: float, bottom: float) -> None:
    draw.rounded_rectangle((s(36), s(top), s(864), s(bottom)), s(24), fill=PANEL, outline=BORDER, width=s(1))


def donut(draw: ImageDraw.ImageDraw, stats: ChestStatistics) -> None:
    cx, cy, radius, inner = 247, 395, 142, 85
    bounds = (s(cx - radius), s(cy - radius), s(cx + radius), s(cy + radius))
    draw.ellipse(bounds, fill=(242, 227, 203))
    angle = -90.0
    for rarity in RARITIES:
        count = stats.counts[rarity]
        if not count:
            continue
        end = angle + count / stats.known * 360
        draw.pieslice(bounds, angle, end, fill=DOT_COLORS[rarity])
        if count != stats.known:
            radians = math.radians(angle)
            draw.line(
                (s(cx), s(cy), s(cx + radius * math.cos(radians)), s(cy + radius * math.sin(radians))),
                fill=PANEL,
                width=s(2),
            )
        angle = end
    draw.ellipse((s(cx - inner), s(cy - inner), s(cx + inner), s(cy + inner)), fill=PANEL)
    text = f"{stats.total:,}"
    text_width = width_of(runs(text, 56))
    size = min(56, max(18, int(56 * s(158) / text_width)))
    draw_text(draw, cx, 354, 74, text, size, BROWN, align="center")
    draw_text(draw, cx, 426, 26, "累计开箱", 19, BROWN, align="center")


def render_statistics(
    info: Header,
    caption: str,
    stats: ChestStatistics,
    top: list[tuple[str, str, int]],
    coverage: str,
) -> bytes:
    detail_notes = []
    if stats.missing:
        detail_notes.append(f"缺少物品详情 {stats.missing} 次。")
    if stats.invalid:
        detail_notes.append(f"物品详情异常 {stats.invalid} 次，请主人检查原始流水。")
    if detail_notes:
        detail_notes.append(f"占比、消耗及排名仅统计详情有效的 {stats.known} 次。")
    rank_top = 584 + len(detail_notes) * 24
    rank_bottom = rank_top + 70 + max(1, len(top)) * 50
    note_top = rank_bottom + 18
    footer_top = note_top + 27
    canvas = Image.new("RGB", (s(WIDTH), s(footer_top + 50)), BG)
    background(canvas)
    draw = ImageDraw.Draw(canvas)
    header(canvas, draw, info)
    heading(canvas, draw, 142, "开箱统计")
    draw_text(draw, WIDTH / 2, 195, 26, caption, 17, SOFT, align="center")

    panel(draw, 230, 560)
    donut(draw, stats)
    draw_text(draw, 432, 253, 30, "稀有度分布", 23, BROWN)
    for index, rarity in enumerate(RARITIES):
        y = 296 + index * 42
        draw.rounded_rectangle((s(424), s(y), s(830), s(y + 36)), s(16), fill=(253, 243, 228))
        draw.ellipse((s(440), s(y + 9), s(458), s(y + 27)), fill=DOT_COLORS[rarity])
        draw_text(draw, 473, y + 3, 30, RARITY_LABELS[rarity], 20, BROWN)
        draw_text(draw, 644, y + 3, 30, f"{stats.counts[rarity]:,} 次", 19, BROWN, align="right")
        percent = f"{stats.counts[rarity] / stats.known:.1%}" if stats.known else "—"
        draw_text(draw, 815, y + 3, 30, percent, 19, SOFT, align="right")
    draw.line((s(424), s(483), s(830), s(483)), fill=BORDER, width=s(1))
    draw_text(draw, 432, 497, 30, "累计消耗点击", 19, BROWN)
    draw_text(draw, 824, 492, 38, f"{stats.clicks:,}", 25, BROWN, align="right")
    for index, message in enumerate(detail_notes):
        draw_text(draw, WIDTH / 2, 567 + index * 24, 22, message, 13, SOFT, align="center")

    panel(draw, rank_top, rank_bottom)
    draw.rounded_rectangle((s(42), s(rank_top + 6), s(858), s(rank_top + 48)), s(18), fill=(233, 237, 211))
    draw_text(draw, 68, rank_top + 11, 30, "掉落最多 TOP 5", 23, BROWN)
    if not top:
        message = "暂无开箱记录" if not stats.total else "暂无可展示的物品详情"
        draw_text(draw, WIDTH / 2, rank_top + 70, 28, message, 18, SOFT, align="center")
    for index, (name, rarity, count) in enumerate(top):
        y = rank_top + 61 + index * 50
        draw.rounded_rectangle((s(66), s(y + 2), s(112), s(y + 32)), s(15), fill=(250, 229, 194))
        draw_text(draw, 89, y + 2, 30, str(index + 1), 19, BROWN, align="center")
        draw_text(draw, 137, y, 28, ellipsize(name, 18, 530), 18, BROWN)
        draw_text(draw, 826, y, 28, f"{count:,}件", 21, BROWN, align="right")
        bar_end = 137 + 565 * count / top[0][2]
        draw.rounded_rectangle((s(137), s(y + 30), s(bar_end), s(y + 38)), s(4), fill=DOT_COLORS[rarity])
    draw_text(draw, WIDTH / 2, note_top, 22, coverage, 13, SOFT, align="center")
    footer(draw, footer_top)
    output = io.BytesIO()
    canvas.save(output, "JPEG", quality=90, optimize=True)
    canvas.close()
    return output.getvalue()
