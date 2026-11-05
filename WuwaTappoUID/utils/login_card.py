"""使用已有 WuwaTappo 素材和字体绘制登录二维码。"""

import io

import qrcode
from PIL import Image, ImageDraw

from .render_common import ACCENT, BG, BROWN, SOFT, background, draw_text, footer, paste_asset, s


def render_login(url: str) -> bytes:
    canvas = Image.new("RGBA", (s(650), s(754)), BG)
    background(canvas, 650)
    draw = ImageDraw.Draw(canvas)
    paste_asset(canvas, "logo.webp", 36, 30, 196)
    draw_text(draw, 614, 43, 46, "WuwaTappo 登录", 34, BROWN, align="right")
    draw_text(draw, 614, 100, 23, "国服账号登录", 16, SOFT, align="right", rounded=False, weight=500)
    draw.rounded_rectangle((s(36), s(143), s(614), s(145)), radius=s(1), fill=(238, 215, 182))
    draw_text(draw, 36, 175, 29, "请使用浏览器扫码，登录本人国服账号", 20, SOFT, rounded=False)
    draw.rounded_rectangle((s(126), s(220), s(524), s(618)), radius=s(22), fill=(255, 250, 239))
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    tile = qr.make_image(fill_color=BROWN, back_color=(255, 250, 239)).convert("RGBA")
    tile = tile.resize((s(364), s(364)), Image.Resampling.NEAREST)
    canvas.alpha_composite(tile, (s(143), s(237)))
    draw_text(draw, 36, 641, 32, "5分钟内有效 · 请勿转发登录链接", 22, ACCENT, rounded=False)
    footer(draw, 700, 650)
    output = io.BytesIO()
    canvas.convert("RGB").save(output, "PNG")
    return output.getvalue()
