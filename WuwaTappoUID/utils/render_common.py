"""各类卡片共用的字体、素材、布局与页脚。"""

import hashlib
import io
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from gsuid_core.utils.fonts.fonts import core_font
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .render_cache import LocalImage, current_config, images
from .settings import bot_name

ASSETS = Path(__file__).parent.parent / "assets" / "tappo"
SCALE = 1.5
WIDTH = 900
PAD_X, PAD_TOP, PAD_BOTTOM = 36, 28, 32
BG = (254, 243, 224)
BROWN = (142, 94, 67)
SOFT = (168, 138, 114)
ACCENT = (200, 113, 74)
Color = tuple[int, int, int] | tuple[int, int, int, int]


def composite(canvas: Image.Image, layer: Image.Image, xy: tuple[int, int]) -> None:
    if canvas.mode == "RGB":
        with layer.getchannel("A") as mask:
            canvas.paste(layer, xy, mask)
    else:
        canvas.alpha_composite(layer, xy)


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
    with Image.open(ASSETS / name) as image:
        return image.convert("RGBA")


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
    composite(canvas, layer, (xy[0] - pad, xy[1] - pad))
    layer.close()
    holder.close()


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask


def fit(image: Image.Image, width: int, height: int) -> Image.Image:
    ratio = min(width / image.width, height / image.height)
    return image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))), Image.LANCZOS)


def image_key(source: bytes | LocalImage) -> object:
    return source if isinstance(source, LocalImage) else hashlib.sha256(source).digest()


def prepared_sprite(raw: bytes | LocalImage, width: int, height: int, *, retain: bool = True) -> Image.Image:
    key = ("sprite", image_key(raw), width, height)
    cached = images.get(key) if retain else None
    if cached is not None:
        return cached
    source = raw.path if isinstance(raw, LocalImage) else io.BytesIO(raw)
    with Image.open(source) as opened:
        with opened.convert("RGBA") as rgba:
            picture = fit(rgba, width, height)
    return images.put(key, picture) if retain else picture


@cache
def scaled_asset(name: str, width: float, opacity: float = 1) -> Image.Image:
    image = asset(name)
    height = width * image.height / image.width
    layer = image.resize((s(width), s(height)), Image.LANCZOS)
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda value: round(value * opacity)))
    return layer


def paste_asset(canvas: Image.Image, name: str, x: float, y: float, width: float, opacity: float = 1) -> None:
    composite(canvas, scaled_asset(name, width, opacity), (s(x), s(y)))


def paste_scaled(canvas: Image.Image, image: Image.Image, x: float, y: float, width: float, opacity: float = 1) -> None:
    height = width * image.height / image.width
    layer = image.resize((s(width), s(height)), Image.LANCZOS)
    if opacity < 1:
        layer.putalpha(layer.getchannel("A").point(lambda v: round(v * opacity)))
    composite(canvas, layer, (s(x), s(y)))
    layer.close()


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
    composite(canvas, background_layer(), (s(width - 760), 0))


def avatar(canvas: Image.Image, raw: bytes | None, width: int = WIDTH) -> None:
    x, y, size, border = width - PAD_X - 72, 45, 72, 4
    circle = Image.new("L", (s(size), s(size)), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, s(size) - 1, s(size) - 1), fill=255)
    shadow(canvas, circle, (s(x), s(y + 3)), (142, 94, 67, 56), 10)
    ring = Image.new("RGBA", circle.size, (255, 250, 240, 255))
    ring.putalpha(circle)
    composite(canvas, ring, (s(x), s(y)))
    inner = s(size) - s(border) * 2
    face = Image.new("RGBA", (inner, inner), (243, 226, 198, 255))
    if raw:
        try:
            with Image.open(io.BytesIO(raw)) as opened, opened.convert("RGBA") as picture:
                side = min(picture.size)
                box = (
                    (picture.width - side) // 2,
                    (picture.height - side) // 2,
                    (picture.width + side) // 2,
                    (picture.height + side) // 2,
                )
                with picture.crop(box) as cropped, cropped.resize((inner, inner), Image.LANCZOS) as resized:
                    face.alpha_composite(resized)
        except OSError:
            pass
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    face.putalpha(mask)
    composite(canvas, face, (s(x + border), s(y + border)))
    circle.close()
    ring.close()
    face.close()
    mask.close()


def header(canvas: Image.Image, draw: ImageDraw.ImageDraw, info: Header, width: int = WIDTH) -> None:
    logo_w = 196
    logo_layer = scaled_asset("logo.webp", logo_w)
    shadow(canvas, logo_layer.getchannel("A"), (s(PAD_X), s(PAD_TOP + 2)), (222, 150, 80, 115), 0)
    composite(canvas, logo_layer, (s(PAD_X), s(PAD_TOP)))
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
        composite(canvas, line, (s(start), s(line_y)))
        line.close()
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
    glow.close()
    draw_text(draw, left + 30, top + 6, 29, title, 22, (255, 250, 240), spacing=2)


def progress_box(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, top: float, rows: list[Progress], width: int = WIDTH
) -> None:
    box_w, box_h = width - PAD_X * 2, 75
    shape = rounded_mask((s(box_w), s(box_h)), s(22))
    fill = Image.new("RGBA", shape.size, (244, 226, 196, 255))
    fill.putalpha(shape)
    composite(canvas, fill, (s(PAD_X), s(top)))
    inner = rounded_mask((s(box_w) - s(2) * 2, s(box_h) - s(2) * 2), s(20))
    panel = Image.new("RGBA", inner.size, (255, 251, 242, 224))
    panel.putalpha(inner.point(lambda v: v * 224 // 255))
    composite(canvas, panel, (s(PAD_X) + s(2), s(top) + s(2)))
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
            composite(canvas, bar, (s(x), s(bar_y)))


def footer(draw: ImageDraw.ImageDraw, y: float, width: float = WIDTH) -> None:
    parts = (
        ("Powered by ", 430),
        ("GsCore", 700),
        (" & Created By ", 430),
        ("WuwaTappoUID", 700),
        (" Bot：", 430),
        ((current_config.get().bot_name if current_config.get() is not None else bot_name()), 630),
    )
    size = 13
    total = sum(misans(size, weight).getlength(value) for value, weight in parts) / SCALE
    while total > width - PAD_X * 2 and size > 8:
        size -= 1
        total = sum(misans(size, weight).getlength(value) for value, weight in parts) / SCALE
    x = (width - total) / 2
    for value, weight in parts:
        x = draw_text(draw, x, y, 26, value, size, BROWN if weight >= 630 else SOFT, rounded=False, weight=weight)
