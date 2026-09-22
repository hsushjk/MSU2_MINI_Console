import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, get_font

_WEEK = ["一", "二", "三", "四", "五", "六", "日"]

@register
class ClockWidget(Widget):
    name = "clock"
    interval = 1.0
    orientations = BOTH
    priority = 10

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        t = time.localtime(ctx.now)

        hh = time.strftime("%H", t)
        mm = time.strftime("%M", t)
        ss = time.strftime("%S", t)
        ymd = time.strftime("%Y-%m-%d", t)
        week = "周" + _WEEK[t.tm_wday]

        if ctx.orientation == LANDSCAPE:

            f_t = get_font(34, bold=True, cjk=False)
            f_s = get_font(13, cjk=True)
            d.text((W // 2, H // 2 - 4), f"{hh}:{mm}:{ss}",
                   fill="white", font=f_t, anchor="mm")
            d.text((5, H - 3), ymd, fill="#b0b0b0", font=f_s, anchor="ls")
            d.text((W - 5, H - 3), week, fill="#b0b0b0", font=f_s, anchor="rs")
        else:

            f_big = get_font(42, bold=True, cjk=False)
            f_ss = get_font(22, cjk=False)
            f_s = get_font(13, cjk=True)
            d.text((W // 2, 24), hh, fill="white", font=f_big, anchor="mm")
            d.text((W // 2, 66), mm, fill="white", font=f_big, anchor="mm")
            d.text((W // 2, 100), ss, fill="#c0c0c0", font=f_ss, anchor="mm")
            d.text((W // 2, 126), ymd, fill="#b0b0b0", font=f_s, anchor="mm")
            d.text((W // 2, 144), week, fill="#b0b0b0", font=f_s, anchor="mm")
        return img
