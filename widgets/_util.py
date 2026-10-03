from PIL import Image, ImageDraw


def text_w(font, text):
    try:
        return int(round(font.getlength(text)))
    except Exception:
        try:
            return int(font.getsize(text)[0])
        except Exception:
            return max(1, len(str(text))) * 8


def ellipsis(font, text, max_w):
    text = str(text)
    if max_w <= 0 or text_w(font, text) <= max_w:
        return text
    dot = "…"
    if text_w(font, dot) > max_w:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_w(font, text[:mid] + dot) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + dot


def marquee(img, xy, text, font, fill, max_w, now, speed=28.0, gap=24):
    text = str(text)
    if max_w <= 0 or not text:
        return
    x, y = int(xy[0]), int(xy[1])
    tw = text_w(font, text)
    if tw <= max_w:
        ImageDraw.Draw(img).text((x, y), text, font=font, fill=fill, anchor="lm")
        return
    try:
        left, top, right, bottom = font.getbbox(text)
    except Exception:
        left, top, right, bottom = 0, 0, tw, 12
    tw = max(tw, int(right - left))
    th = max(1, int(bottom - top))
    strip_w = tw + gap
    strip = Image.new("RGBA", (strip_w, th + 2), (0, 0, 0, 0))
    ImageDraw.Draw(strip).text((-left, 1 - top), text, font=font, fill=fill)
    off = int((now * speed) % strip_w)
    window = Image.new("RGBA", (max_w, th + 2), (0, 0, 0, 0))
    first = strip.crop((off, 0, min(off + max_w, strip_w), th + 2))
    window.paste(first, (0, 0))
    if first.width < max_w:
        rest = strip.crop((0, 0, min(strip_w, max_w - first.width), th + 2))
        window.paste(rest, (first.width, 0))
    img.alpha_composite(window, (x, y - (th + 2) // 2))


def row_text(img, xy, text, font, fill, max_w, now, scroll=True,
             speed=28.0, gap=24):
    if scroll:
        marquee(img, xy, text, font, fill, max_w, now, speed, gap)
    else:
        ImageDraw.Draw(img).text((int(xy[0]), int(xy[1])),
                                 ellipsis(font, text, max_w),
                                 font=font, fill=fill, anchor="lm")