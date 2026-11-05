"""开箱和合成结果卡片。"""

import io
import math
from dataclasses import dataclass
from functools import cache

from PIL import Image, ImageDraw

from .render_cache import LocalImage
from .render_common import (
    BG,
    BROWN,
    PAD_BOTTOM,
    PAD_TOP,
    SCALE,
    SOFT,
    WIDTH,
    Header,
    Progress,
    asset,
    background,
    draw_text,
    ellipsize,
    footer,
    header,
    heading,
    misans,
    paste_asset,
    prepared_sprite,
    progress_box,
    rounded_mask,
    runs,
    s,
    shadow,
    width_of,
)

CARD, CARD_H, GAP_X, GAP_Y, COLUMNS = 148, 198, 18, 26, 5
RARITIES = ("common", "uncommon", "rare", "epic")
RARITY_LABELS = {"common": "普通", "uncommon": "珍稀", "rare": "稀有", "epic": "史诗"}
TYPE_LABELS = {"character": "角色", "accessory": "配件", "emote": "表情"}
LABEL_COLORS = {"common": SOFT, "uncommon": (111, 143, 208), "rare": (180, 111, 208), "epic": (229, 130, 78)}


@dataclass
class Card:
    name: str
    rarity: str
    kind: str
    image: bytes | LocalImage | None = None
    fresh: bool = False
    quantity: int | None = None


@cache
def prize_masks() -> tuple[Image.Image, Image.Image]:
    size = (s(CARD), s(CARD))
    return asset("frame_mask.png").getchannel("A").resize(size, Image.LANCZOS), Image.new("L", size, 0)


def card(canvas: Image.Image, draw: ImageDraw.ImageDraw, x: float, y: float, item: Card) -> None:
    rarity = item.rarity if item.rarity in RARITIES else "common"
    if rarity in ("rare", "epic"):
        paste_asset(canvas, f"rays_{rarity}.webp", x - CARD * 0.24, y - CARD * 0.24, CARD * 1.48, 0.9)
    paste_asset(canvas, f"frame_{rarity}.webp", x, y, CARD)
    if item.image:
        try:
            box = s(CARD * 0.88)
            picture = prepared_sprite(item.image, box, box)
        except OSError:
            picture = None
        if picture is not None:
            prize = Image.new("RGBA", (s(CARD), s(CARD)), (0, 0, 0, 0))
            prize.alpha_composite(
                picture,
                (s(CARD * 0.06) + (box - picture.width) // 2, s(CARD * 0.05) + (box - picture.height) // 2),
            )
            mask, empty = prize_masks()
            prize.putalpha(Image.composite(prize.getchannel("A"), empty, mask))
            canvas.alpha_composite(prize, (s(x), s(y)))
    paste_asset(canvas, f"stars_{rarity}.webp", x - CARD * 0.08, y - CARD * 0.10, CARD * 1.16)
    if rarity != "common":
        paste_asset(canvas, f"ribbons_{rarity}.webp", x - CARD * 0.06, y + CARD * 0.02, CARD * 1.12)
    if item.fresh:
        badge_w, badge_h = 7 + width_of(runs("NEW", 14)) / SCALE + 7, 22
        badge = rounded_mask((s(badge_w), s(badge_h)), s(8))
        shadow(canvas, badge, (s(x - 6), s(y - 6 + 2)), (142, 94, 67, 77), 4)
        fill = Image.new("RGBA", badge.size, (200, 136, 101, 255))
        fill.putalpha(badge)
        canvas.alpha_composite(fill, (s(x - 6), s(y - 6)))
        draw_text(draw, x - 6 + 7, y - 6 + 2, 18, "NEW", 14, (255, 243, 221))
    if item.quantity is not None and item.quantity > 1:
        label = f"×{item.quantity}"
        font = misans(14, 630)
        bounds = draw.textbbox((0, 0), label, font=font)
        badge_w, badge_h = bounds[2] - bounds[0] + s(8), s(22)
        right, bottom = s(x + CARD), s(y + CARD)
        draw.rounded_rectangle((right - badge_w, bottom - badge_h, right, bottom), badge_h / 2, fill=BROWN)
        # 按实际字形边界居中，避免字体左右留白让 ×N 看起来偏移。
        center_x, center_y = right - badge_w / 2, bottom - badge_h / 2
        draw.text(
            (center_x - (bounds[0] + bounds[2]) / 2, center_y - (bounds[1] + bounds[3]) / 2),
            label,
            font=font,
            fill=(255, 243, 221),
        )
    draw_text(draw, x + CARD / 2, y + CARD + 8, 22, ellipsize(item.name, 16, CARD), 16, BROWN, align="center")
    label = f"{RARITY_LABELS[rarity]} · {TYPE_LABELS.get(item.kind, '物品')}"
    draw_text(draw, x + CARD / 2, y + CARD + 8 + 22 + 2, 18, label, 13, LABEL_COLORS[rarity], align="center")


def render_chest_card(
    info: Header, title: str, cards: list[Card], note: str = "", progress: list[Progress] | None = None
) -> bytes:
    """画一张开箱图，返回 JPEG。"""
    columns = max(1, min(len(cards), COLUMNS))
    rows = max(1, math.ceil(len(cards) / COLUMNS))
    heading_top = PAD_TOP + 106 + 26
    section_top = heading_top + 41 + 18
    if note:
        note_top = section_top - 8
        section_top = note_top + 20 + 16
    grid_top = section_top + 6
    grid_bottom = grid_top + rows * CARD_H + (rows - 1) * GAP_Y
    progress_top = grid_bottom + 22
    footer_top = (progress_top + 75 if progress else grid_bottom) + 22
    bottom = footer_top + 26 + PAD_BOTTOM

    canvas = Image.new("RGBA", (s(WIDTH), s(bottom)), BG + (255,))
    background(canvas)
    draw = ImageDraw.Draw(canvas)
    header(canvas, draw, info)
    heading(canvas, draw, heading_top, title)
    if note:
        draw_text(draw, WIDTH / 2, note_top, 20, note, 15, SOFT, align="center")
    grid_w = columns * CARD + (columns - 1) * GAP_X
    left = (WIDTH - grid_w) / 2
    # 和原版的 CSS 网格一样：网格整体居中，最后一行不满时靠左排。
    for index, item in enumerate(cards):
        row, col = divmod(index, COLUMNS)
        card(canvas, draw, left + col * (CARD + GAP_X), grid_top + row * (CARD_H + GAP_Y), item)
    if progress:
        progress_box(canvas, draw, progress_top, progress)
    footer(draw, footer_top)
    output = io.BytesIO()
    canvas.convert("RGB").save(output, "JPEG", quality=90, optimize=True)
    return output.getvalue()
