"""OTT status cards (v11): cinematic 1280x720 posters for /upcoming & /today.

Adapted from the standalone ``image_gen`` plugin: backdrop + poster +
provider logos + title/meta/plot, optional watermark. The channel logo
is best-effort — missing file/font never breaks card generation.
"""
from __future__ import annotations

import logging
import os
from io import BytesIO

log = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False


async def _download_image(client, url):
    if not url or not _PIL_OK:
        return None
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200:
            img = Image.open(BytesIO(r.content))
            img.load()
            return img
    except Exception as exc:  # noqa: BLE001
        log.debug("card image download failed: %s", exc)
    return None


def _safe(text: str | None) -> str:
    t = str(text or "")
    for k, v in {"\u2018": "'", "\u2019": "'", "\u201c": '"',
                 "\u201d": '"', "\u2013": "-", "\u2014": "-",
                 "…": "...", "⭐": "*"}.items():
        t = t.replace(k, v)
    return t


def _font(name: str, size: int):
    for cand in (name, "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(cand, size)
        except IOError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    import textwrap
    try:
        avg = draw.textlength("a", font=font) or 10
    except Exception:  # noqa: BLE001
        avg = 10
    return textwrap.wrap(str(text), width=max(10, int(max_width / avg)))


async def generate_status_image(backdrop_url, poster_url, provider_urls,
                                title, year, rating, genres, plot,
                                is_upcoming=False, release_date=None):
    """Return a JPEG BytesIO (1280x720) or None when PIL is missing."""
    if not _PIL_OK:
        return None
    import httpx

    title, year, rating = _safe(title), _safe(year), _safe(rating)
    genres, plot = _safe(genres), _safe(plot)
    release_date = _safe(release_date)

    async with httpx.AsyncClient(trust_env=False) as client:
        import asyncio
        results = await asyncio.gather(
            _download_image(client, backdrop_url),
            _download_image(client, poster_url),
            *[_download_image(client, u)
              for u in (provider_urls or [])[:5]],
        )
    backdrop, poster, providers = results[0], results[1], results[2:]

    width, height = 1280, 720
    canvas = Image.new("RGBA", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    if backdrop:
        bg_w, bg_h = backdrop.size
        ratio = max(width / bg_w, height / bg_h)
        ns = (int(bg_w * ratio), int(bg_h * ratio))
        backdrop = backdrop.resize(ns, Image.Resampling.LANCZOS)
        left, top = (ns[0] - width) / 2, (ns[1] - height) / 2
        backdrop = backdrop.crop((left, top, left + width, top + height))
        backdrop = backdrop.filter(ImageFilter.GaussianBlur(5))
        canvas.paste(backdrop, (0, 0))
        canvas = Image.alpha_composite(
            canvas, Image.new("RGBA", (width, height), (0, 0, 0, 160)))
        draw = ImageDraw.Draw(canvas)
    else:
        canvas = Image.new("RGBA", (width, height), (20, 20, 20))
        draw = ImageDraw.Draw(canvas)

    if poster:
        target_h = 600
        p_w, p_h = poster.size
        new_p_w = int(p_w * (target_h / p_h))
        poster = poster.resize((new_p_w, target_h), Image.Resampling.LANCZOS)
        shadow = Image.new("RGBA", (new_p_w + 20, target_h + 20),
                           (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle([(10, 10), (new_p_w + 10, target_h + 10)],
                             radius=20, fill=(0, 0, 0, 150))
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        pos_x, pos_y = 60, (height - target_h) // 2
        canvas.paste(shadow, (pos_x - 5, pos_y - 5), shadow)
        mask = Image.new("L", (new_p_w, target_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [(0, 0), (new_p_w, target_h)], radius=20, fill=255)
        canvas.paste(poster.convert("RGBA"), (pos_x, pos_y), mask)
        text_x = pos_x + new_p_w + 60
    else:
        text_x = 60

    title_font = _font("arial.ttf", 43)
    meta_font = _font("arial.ttf", 28)
    plot_font = _font("arial.ttf", 24)
    genre_font = _font("arial.ttf", 24)
    cs_font = _font("arial.ttf", 55)
    max_w = width - text_x - 40
    y = 100

    if is_upcoming:
        for line in _wrap(draw, f"{title} ({year})",
                          title_font, max_w)[:3]:
            draw.text((text_x, y), line, font=title_font, fill="white")
            y += 60
        y += 60
        draw.text((text_x, y), "COMING SOON", font=cs_font, fill="#FFD700")
        y += 80
        if release_date:
            draw.text((text_x, y), release_date,
                      font=title_font, fill="white")
    else:
        for line in _wrap(draw, title, title_font, max_w)[:2]:
            draw.text((text_x, y), line, font=title_font, fill="white")
            y += 60
        y += 10
        draw.text((text_x, y), f"{year}   |   {rating}",
                  font=meta_font, fill="#FFD700")
        y += 50
        if genres and genres != "N/A":
            draw.text((text_x, y), genres, font=genre_font, fill="#A0A0A0")
            y += 50
        draw.line([(text_x, y),
                   (text_x + min(300, max_w), y)], fill="white", width=2)
        y += 35
        for line in _wrap(draw, plot or "No description available.",
                          plot_font, max_w)[:7]:
            draw.text((text_x, y), line, font=plot_font, fill="white")
            y += 35

    providers = [p for p in providers if p]
    if providers:
        cur_x, bottom_y = width - 60, height - 130
        for p in reversed(providers):
            pw, ph = p.size
            npw = int(pw * (70 / ph))
            pr = p.resize((npw, 70), Image.Resampling.LANCZOS)
            cur_x -= npw
            canvas.paste(pr, (cur_x, bottom_y),
                         pr if pr.mode == "RGBA" else None)
            cur_x -= 20

    # Watermark: channel logo if present, else text only.
    try:
        logo_path = "plugins/Extra/logo.jpg"
        logo_img = None
        if os.path.exists(logo_path):
            logo_img = Image.open(logo_path).convert("RGBA")
            lh = 50
            lw = int(logo_img.size[0] * (lh / logo_img.size[1]))
            logo_img = logo_img.resize((lw, lh), Image.Resampling.LANCZOS)
            m = Image.new("L", (lw, lh), 0)
            ImageDraw.Draw(m).rounded_rectangle(
                [(0, 0), (lw, lh)], radius=15, fill=255)
            logo_img.putalpha(m)
            wx, wy = width - lw - 40, 40
            canvas.paste(logo_img, (wx, wy), logo_img)
        wm_font = _font("arial.ttf", 20)
        wm_text = "t.me/moovidex"
        try:
            bb = draw.textbbox((0, 0), wm_text, font=wm_font)
            tw = bb[2] - bb[0]
        except Exception:  # noqa: BLE001
            tw = 120
        anchor_x = (width - (lw if logo_img else 0) - 40)
        tx = anchor_x + ((lw - tw) // 2 if logo_img else -tw - 10)
        ty = (40 + 50 + 5) if logo_img else 40
        if tx + tw > width - 10:
            tx = width - tw - 10
        draw.text((tx + 1, ty + 1), wm_text, font=wm_font, fill="black")
        draw.text((tx, ty), wm_text, font=wm_font, fill="white")
    except Exception as exc:  # noqa: BLE001
        log.debug("watermark failed: %s", exc)

    out = BytesIO()
    canvas.convert("RGB").save(out, "JPEG", quality=90)
    out.seek(0)
    out.name = "ott_card.jpg"
    return out
