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

    def on_enter(self, ctx):
        ctx.state.pop("_clk_key", None)

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        W, H = ctx.size
        t = time.localtime(ctx.now)
        hh = time.strftime("%H", t)
        mm = time.strftime("%M", t)
        ss = time.strftime("%S", t)
        ymd = time.strftime("%Y-%m-%d", t)
        week = "周" + _WEEK[t.tm_wday]
        key = (ctx.orientation, W, H, ymd, week, hh, mm, ss)
        if ctx.state.get("_clk_key") == key:
            return None
        ctx.state["_clk_key"] = key
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        margin = max(3, W // 32)
        if ctx.orientation == LANDSCAPE:
            f_t = self._fit_time(f"{hh}:{mm}:{ss}", W - 2 * margin,
                                 max(18, min(36, H // 2)))
            f_s = get_font(max(9, min(14, H // 6)), cjk=True)
            cy = H // 2 - max(2, H // 12)
            d.text((W // 2, cy), f"{hh}:{mm}:{ss}",
                   fill="white", font=f_t, anchor="mm")
            base_y = H - max(3, H // 20)
            d.text((margin, base_y), ymd, fill="#b0b0b0", font=f_s, anchor="ls")
            d.text((W - margin, base_y), week, fill="#b0b0b0", font=f_s, anchor="rs")
        else:
            f_big = get_font(max(18, min(56, H // 4)), bold=True, cjk=False)
            f_ss = get_font(max(14, min(30, H // 7)), cjk=False)
            f_s = get_font(max(9, min(15, H // 12)), cjk=True)
            d.text((W // 2, H * 15 // 100), hh, fill="white", font=f_big, anchor="mm")
            d.text((W // 2, H * 41 // 100), mm, fill="white", font=f_big, anchor="mm")
            d.text((W // 2, H * 5 // 8), ss, fill="#c0c0c0", font=f_ss, anchor="mm")
            d.text((W // 2, H * 79 // 100), ymd, fill="#b0b0b0", font=f_s, anchor="mm")
            d.text((W // 2, H * 9 // 10), week, fill="#b0b0b0", font=f_s, anchor="mm")
        return img

    @staticmethod
    def _fit_time(text, max_w, max_size):
        size = max(16, int(max_size))
        while size > 16:
            f = get_font(size, bold=True, cjk=False)
            try:
                if f.getlength(text) <= max_w:
                    return f
            except Exception:
                return f
            size -= 2
        return get_font(16, bold=True, cjk=False)