"""开箱通知里的物品图：裁掉透明边，居中放到按稀有度着色的圆角底板上。"""

from io import BytesIO

from PIL import Image, ImageDraw

TILE = 320
RADIUS = 28
FIT = 256
MAX_SCALE = 2.5
BORDER = 3
# 稀有度 -> (上端底色, 下端底色, 描边)；未知稀有度按普通处理。
RARITY_COLORS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = {
    "common": ((244, 246, 250), (225, 230, 238), (196, 204, 216)),
    "uncommon": ((241, 250, 243), (212, 237, 219), (120, 190, 140)),
    "rare": ((239, 245, 254), (208, 225, 248), (110, 160, 230)),
    "epic": ((247, 241, 254), (227, 212, 248), (170, 130, 225)),
    "legendary": ((254, 249, 234), (247, 226, 182), (230, 180, 80)),
}


def render_tile(raw: bytes, rarity: str) -> bytes:
    top, bottom, border = RARITY_COLORS.get(rarity.strip().lower(), RARITY_COLORS["common"])
    sprite = Image.open(BytesIO(raw)).convert("RGBA")
    box = sprite.getchannel("A").getbbox()
    if box is not None:
        sprite = sprite.crop(box)
    scale = min(FIT / sprite.width, FIT / sprite.height, MAX_SCALE)
    sprite = sprite.resize((max(1, round(sprite.width * scale)), max(1, round(sprite.height * scale))), Image.LANCZOS)

    background = Image.new("RGBA", (TILE, TILE))
    draw = ImageDraw.Draw(background)
    for y in range(TILE):
        t = y / (TILE - 1)
        draw.line([(0, y), (TILE, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    mask = Image.new("L", (TILE, TILE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, TILE - 1, TILE - 1), radius=RADIUS, fill=255)
    tile = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
    tile.paste(background, (0, 0), mask)
    ImageDraw.Draw(tile).rounded_rectangle(
        (1, 1, TILE - 2, TILE - 2), radius=RADIUS, outline=border + (255,), width=BORDER
    )
    tile.alpha_composite(sprite, ((TILE - sprite.width) // 2, (TILE - sprite.height) // 2))

    out = BytesIO()
    tile.save(out, format="PNG", optimize=True)
    return out.getvalue()
