"""开箱图：照 XWUIDCollection wwtappo 的开箱图（templates/draw.html）用 Pillow 绘制。

布局数值取自原版模板在浏览器里的实测排版（900 px 宽），按 SCALE 放大绘制；
素材与 TappoMaru 圆体取自同一项目。圆体只收了部分汉字，缺字逐字回退到 GS 的 MiSans，
和浏览器的字体回退一致；服务器的 Pillow 读不了 woff2 时整张图都用 MiSans。
"""

import hashlib
import io
import math
from collections import OrderedDict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from gsuid_core.utils.fonts.fonts import core_font
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).parent.parent / "assets" / "tappo"
SCALE = 1.5
WIDTH = 900
PAD_X, PAD_TOP, PAD_BOTTOM = 36, 28, 32
CARD, CARD_H, GAP_X, GAP_Y, COLUMNS = 148, 198, 18, 26, 5

BG = (254, 243, 224)
BROWN = (142, 94, 67)
SOFT = (168, 138, 114)
ACCENT = (200, 113, 74)
RARITIES = ("common", "uncommon", "rare", "epic")
RARITY_LABELS = {"common": "普通", "uncommon": "珍稀", "rare": "稀有", "epic": "史诗"}
TYPE_LABELS = {"character": "角色", "accessory": "配件", "emote": "表情"}
LABEL_COLORS = {"common": SOFT, "uncommon": (111, 143, 208), "rare": (180, 111, 208), "epic": (229, 130, 78)}
Color = tuple[int, int, int] | tuple[int, int, int, int]
SPRITE_CACHE_BYTES = 64 * 1024 * 1024
_sprites: OrderedDict[tuple[str, int, int], Image.Image] = OrderedDict()
_sprite_bytes = 0


@dataclass
class Card:
    name: str
    rarity: str
    kind: str
    image: bytes | None = None
    fresh: bool = False
    quantity: int | None = None


@dataclass
class Progress:
    label: str
    owned: int
    total: int


@dataclass
class Header:
    nickname: str
    uid: str = ""
    available: int | None = None
    avatar: bytes | None = None
    collected: tuple[int, int] | None = None


def s(value: float) -> int:
    return round(value * SCALE)


@cache
def asset(name: str) -> Image.Image:
    return Image.open(ASSETS / name).convert("RGBA")


@cache
def tappo(size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(str(ASSETS / "tappo.woff2"), s(size))
    except OSError:
        return None


@cache
def misans(size: int, weight: int = 520) -> ImageFont.FreeTypeFont:
    return core_font(s(size), weight)


@cache
def missing_shape(size: int) -> tuple[tuple[int, int], bytes]:
    font = tappo(size)
    assert font is not None
    mask = font.getmask("\U000f0000")
    return mask.size, bytes(mask)


@cache
def has_glyph(size: int, char: str) -> bool:
    font = tappo(size)
    if font is None:
        return False
    mask = font.getmask(char)
    return (mask.size, bytes(mask)) != missing_shape(size)


def runs(text: str, size: int, *, rounded: bool = True, weight: int = 520) -> list[tuple[ImageFont.FreeTypeFont, str]]:
    """按字体切分文字：圆体有的字用圆体，没有的用 MiSans。"""
    result: list[tuple[ImageFont.FreeTypeFont, str]] = []
    for char in text:
        font = tappo(size) if rounded and has_glyph(size, char) else misans(size, weight)
        assert font is not None
        if result and result[-1][0] is font:
            result[-1] = (font, result[-1][1] + char)
        else:
            result.append((font, char))
    return result


def width_of(parts: list[tuple[ImageFont.FreeTypeFont, str]], spacing: float = 0) -> float:
    return sum(font.getlength(text) for font, text in parts) + spacing * sum(len(text) for _, text in parts)


def baseline(top: float, line: float, size: int, rounded: bool) -> float:
    """CSS 行盒里的基线位置：内容区（上伸 + 下伸）在行高里上下居中。"""
    font = tappo(size) if rounded and tappo(size) is not None else misans(size)
    assert font is not None
    ascent, descent = font.getmetrics()
    return s(top) + (s(line) - ascent - descent) / 2 + ascent


def draw_text(
    draw: ImageDraw.ImageDraw,
    x: float,
    top: float,
    line: float,
    text: str,
    size: int,
    color: Color,
    *,
    align: str = "left",
    rounded: bool = True,
    weight: int = 520,
    spacing: float = 0,
) -> float:
    """在一行里画文字，返回画到的右端（px，未缩放）；x 是对齐基准。"""
    parts = runs(text, size, rounded=rounded, weight=weight)
    total = width_of(parts, s(spacing))
    start = {"left": s(x), "center": s(x) - total / 2, "right": s(x) - total}[align]
    y = baseline(top, line, size, rounded)
    for font, chunk in parts:
        if spacing:
            for char in chunk:
                draw.text((start, y), char, font=font, fill=color, anchor="ls")
                start += font.getlength(char) + s(spacing)
        else:
            draw.text((start, y), chunk, font=font, fill=color, anchor="ls")
            start += font.getlength(chunk)
    return start / SCALE


def ellipsize(text: str, size: int, limit: float) -> str:
    if width_of(runs(text, size)) <= s(limit):
        return text
    while text and width_of(runs(text + "…", size)) > s(limit):
        text = text[:-1]
    return text + "…"


def shadow(canvas: Image.Image, mask: Image.Image, xy: tuple[int, int], color: Color, blur: float) -> None:
    """把灰度形状模糊后按给定颜色与透明度叠到画布上（CSS box/drop-shadow）。"""
    pad = s(blur) * 2
    holder = Image.new("L", (mask.width + pad * 2, mask.height + pad * 2), 0)
    holder.paste(mask, (pad, pad))
    if blur:
        holder = holder.filter(ImageFilter.GaussianBlur(s(blur) / 2))
    alpha = color[3] if len(color) == 4 else 255
    holder = holder.point(lambda v: v * alpha // 255)
    layer = Image.new("RGBA", holder.size, color[:3] + (0,))
    layer.putalpha(holder)
    canvas.alpha_composite(layer, (xy[0] - pad, xy[1] - pad))


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def fit(image: Image.Image, width: int, height: int) -> Image.Image:
    ratio = min(width / image.width, height / image.height)
    return image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.LANCZOS)


def prepared_sprite(raw: bytes, width: int, height: int) -> Image.Image:
    """在绘图锁内复用已缩放的只读物品图；缓存不保留原始 PNG。"""
    global _sprite_bytes
    key = (hashlib.sha256(raw).hexdigest(), width, height)
    if key in _sprites:
        _sprites.move_to_end(key)
        return _sprites[key]
    picture = fit(Image.open(io.BytesIO(raw)).convert("RGBA"), width, height)
    size = picture.width * picture.height * 4
    while _sprites and _sprite_bytes + size > SPRITE_CACHE_BYTES:
        _, old = _sprites.popitem(last=False)
        _sprite_bytes -= old.width * old.height * 4
    _sprites[key] = picture
    _sprite_bytes += size
    return picture


@cache
def scaled_asset(name: str, width: float, opacity: float = 1) -> Image.Image:
    image = asset(name)
    height = width * image.height / image.width
    layer = image.resize((s(width), s(height)), Image.LANCZOS)
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda value: round(value * opacity)))
    return layer


def paste_asset(canvas: Image.Image, name: str, x: float, y: float, width: float, opacity: float = 1) -> None:
    canvas.alpha_composite(scaled_asset(name, width, opacity), (s(x), s(y)))


def paste_scaled(canvas: Image.Image, image: Image.Image, x: float, y: float, width: float, opacity: float = 1) -> None:
    height = width * image.height / image.width
    layer = image.resize((s(width), s(height)), Image.LANCZOS)
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda v: round(v * opacity)))
    canvas.alpha_composite(layer, (s(x), s(y)))


@cache
def background_layer() -> Image.Image:
    """右上角背景图：cover 进 760×574 的框，按 200° 方向渐隐（15% 不透明到 70% 透明）。"""
    box_w, box_h = 760, 574
    bg = asset("bg.jpg")
    ratio = max(s(box_w) / bg.width, s(box_h) / bg.height)
    bg = bg.resize((math.ceil(bg.width * ratio), math.ceil(bg.height * ratio)), Image.LANCZOS)
    bg = bg.crop((bg.width - s(box_w), 0, bg.width, s(box_h)))
    angle = math.radians(200)
    dx, dy = math.sin(angle), -math.cos(angle)
    span = abs(box_w * dx) + abs(box_h * dy)
    small_w, small_h = box_w // 4, box_h // 4
    mask = Image.new("L", (small_w, small_h))
    mask.putdata(
        [
            round(255 * min(1, max(0, (0.70 - t) / 0.55)))
            for y in range(small_h)
            for x in range(small_w)
            for t in [((x * 4 - box_w / 2) * dx + (y * 4 - box_h / 2) * dy) / span + 0.5]
        ]
    )
    bg.putalpha(mask.resize(bg.size, Image.BILINEAR))
    return bg


def background(canvas: Image.Image, width: int = WIDTH) -> None:
    canvas.alpha_composite(background_layer(), (s(width - 760), 0))


def avatar(canvas: Image.Image, raw: bytes | None, width: int = WIDTH) -> None:
    x, y, size, border = width - PAD_X - 72, 45, 72, 4
    circle = Image.new("L", (s(size), s(size)), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, s(size) - 1, s(size) - 1), fill=255)
    shadow(canvas, circle, (s(x), s(y + 3)), (142, 94, 67, 56), 10)
    ring = Image.new("RGBA", circle.size, (255, 250, 240, 255))
    ring.putalpha(circle)
    canvas.alpha_composite(ring, (s(x), s(y)))
    inner = s(size) - s(border) * 2
    face = Image.new("RGBA", (inner, inner), (243, 226, 198, 255))
    if raw:
        try:
            picture = Image.open(io.BytesIO(raw)).convert("RGBA")
            side = min(picture.size)
            picture = picture.crop(
                (
                    (picture.width - side) // 2,
                    (picture.height - side) // 2,
                    (picture.width + side) // 2,
                    (picture.height + side) // 2,
                )
            )
            face.alpha_composite(picture.resize((inner, inner), Image.LANCZOS))
        except OSError:
            pass
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    face.putalpha(mask)
    canvas.alpha_composite(face, (s(x + border), s(y + border)))


def header(canvas: Image.Image, draw: ImageDraw.ImageDraw, info: Header, width: int = WIDTH) -> None:
    logo_w = 196
    logo_layer = scaled_asset("logo.webp", logo_w)
    shadow(canvas, logo_layer.getchannel("A"), (s(PAD_X), s(PAD_TOP + 2)), (222, 150, 80, 115), 0)
    canvas.alpha_composite(logo_layer, (s(PAD_X), s(PAD_TOP)))
    avatar(canvas, info.avatar, width)
    player_right = width - PAD_X - 72 - 14
    draw_text(draw, player_right, 54, 28.8, info.nickname[:12], 24, BROWN, align="right", rounded=False, weight=700)
    parts: list[tuple[str, Color]] = []
    if info.uid:
        parts.append((f"UID {info.uid}", SOFT))
    if info.collected is not None:
        if parts:
            parts.append((" · ", SOFT))
        parts += [("已收集 ", SOFT), (f"{info.collected[0]}/{info.collected[1]}", ACCENT)]
    if info.available is not None:
        if parts:
            parts.append((" · ", SOFT))
        parts += [("可用次数 ", SOFT), (str(info.available), ACCENT)]
    total = sum(width_of(runs(text, 15)) for text, _ in parts) / SCALE
    x = player_right - total
    for text, color in parts:
        x = draw_text(draw, x, 89, 19, text, 15, color)


def heading(canvas: Image.Image, draw: ImageDraw.ImageDraw, top: float, title: str, width: int = WIDTH) -> None:
    parts = runs(title, 22)
    text_w = width_of(parts, s(2)) / SCALE
    pill_w, pill_h = text_w + 60, 41
    left = (width - pill_w) / 2
    line_y = top + (pill_h - 2) / 2
    for start, end, fade_left in ((PAD_X, left - 14, True), (left + pill_w + 14, width - PAD_X, False)):
        span = s(end) - s(start)
        if span <= 0:
            continue
        # 两侧细线朝标题方向由透明渐变到 #ecd2ae。
        ramp = [i / max(1, span - 1) for i in range(span)]
        alpha = bytes(round(255 * (t if fade_left else 1 - t)) for t in ramp)
        line = Image.new("RGBA", (span, s(2)), (236, 210, 174, 0))
        line.putalpha(Image.frombytes("L", (span, 1), alpha).resize((span, s(2))))
        canvas.alpha_composite(line, (s(start), s(line_y)))
    radius = s(pill_h) // 2
    draw.rounded_rectangle((s(left), s(top), s(left + pill_w) - 1, s(top + pill_h) - 1), radius, fill=(234, 169, 102))
    draw.rounded_rectangle(
        (s(left), s(top), s(left + pill_w) - 1, s(top + pill_h - 3) - 1), radius, fill=(248, 200, 134)
    )
    # 大图的标题阴影只绘制标题区域，保留原来的像素对齐位置。
    glow = Image.new("L", (s(pill_w), s(pill_h)), 0)
    text_x = (s(left + 30) - s(left)) / SCALE
    text_y = (s(top + 7) - s(top)) / SCALE
    draw_text(ImageDraw.Draw(glow), text_x, text_y, 29, title, 22, 255, spacing=2)
    shadow(canvas, glow, (s(left), s(top)), (180, 100, 50, 115), 2)
    draw_text(draw, left + 30, top + 6, 29, title, 22, (255, 250, 240), spacing=2)


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


def progress_box(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, top: float, rows: list[Progress], width: int = WIDTH
) -> None:
    box_w, box_h = width - PAD_X * 2, 75
    shape = rounded_mask((s(box_w), s(box_h)), s(22))
    fill = Image.new("RGBA", shape.size, (244, 226, 196, 255))
    fill.putalpha(shape)
    canvas.alpha_composite(fill, (s(PAD_X), s(top)))
    inner = rounded_mask((s(box_w) - s(2) * 2, s(box_h) - s(2) * 2), s(20))
    panel = Image.new("RGBA", inner.size, (255, 251, 242, 224))
    panel.putalpha(inner.point(lambda v: v * 224 // 255))
    canvas.alpha_composite(panel, (s(PAD_X) + s(2), s(top) + s(2)))
    column = (box_w - 4 - 40 - 16 * 2) / 3
    for index, row in enumerate(rows[:3]):
        x = PAD_X + 2 + 20 + index * (column + 16)
        row_top = top + 2 + 15
        draw_text(draw, x, row_top, 22, row.label, 16, BROWN)
        draw_text(draw, x + column, row_top, 22, f"{row.owned}/{row.total}", 16, SOFT, align="right")
        bar_y = row_top + 22 + 8
        draw.rounded_rectangle((s(x), s(bar_y), s(x + column) - 1, s(bar_y + 10) - 1), s(5), fill=(242, 227, 203))
        ratio = min(1, max(0, row.owned / row.total)) if row.total else 0
        filled = s(column * ratio)
        if filled >= s(2):
            gradient = Image.frombytes(
                "RGB",
                (filled, 1),
                b"".join(
                    bytes(round(a + (b - a) * i / max(1, filled - 1)) for a, b in zip((245, 183, 122), (233, 140, 93)))
                    for i in range(filled)
                ),
            ).resize((filled, s(10)))
            bar = gradient.convert("RGBA")
            bar.putalpha(rounded_mask((filled, s(10)), s(5)))
            canvas.alpha_composite(bar, (s(x), s(bar_y)))


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
    bottom = (progress_top + 75 if progress else grid_bottom) + PAD_BOTTOM

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
    output = io.BytesIO()
    canvas.convert("RGB").save(output, "JPEG", quality=90, optimize=True)
    return output.getvalue()
