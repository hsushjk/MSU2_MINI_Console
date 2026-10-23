import calendar as _cal
import datetime as _dt
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import text_w

_WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"]

@register
class CalendarWidget(Widget):
    name = "calendar"
    interval = 1.0
    orientations = BOTH
    priority = 15

    config_schema = [
        Field("first_weekday", "select", "每周起始",
              default="mon", options=["mon", "sun"]),
    ]

    def on_enter(self, ctx):
        ctx.state.pop("_cal_key", None)

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        cfg = self.get_config(ctx)
        W, H = ctx.size
        t = time.localtime(ctx.now)
        first = cfg.get("first_weekday") if cfg.get("first_weekday") in ("mon", "sun") else "mon"
        key = (ctx.orientation, W, H, t.tm_year, t.tm_mon, t.tm_mday, first)
        if ctx.state.get("_cal_key") == key:
            return None
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if ctx.orientation == LANDSCAPE:
            self._draw_month(d, W, H, t, first)
        else:
            self._draw_week(d, W, H, t, first)
        ctx.state["_cal_key"] = key
        return img

    def _draw_month(self, d, W, H, t, first):
        firstweek = 0 if first == "mon" else 6
        year, month, today = t.tm_year, t.tm_mon, t.tm_mday
        weekend = (5, 6) if firstweek == 0 else (0, 6)
        margin = max(2, W // 40)

        f_title = get_font(max(11, min(13, H // 6)), bold=True, cjk=False)
        title_y = max(5, H // 14)
        d.text((W // 2, title_y), f"{year}-{month:02d}",
               fill="#f0f0f0", font=f_title, anchor="mm")

        names = _WEEK_CN if first == "mon" else [_WEEK_CN[-1]] + _WEEK_CN[:-1]
        head_y = max(title_y + 9, H // 5)
        f_head = get_font(max(8, min(10, H // 10)), cjk=True)
        col_w = W / 7.0
        for i, n in enumerate(names):
            x = int((i + 0.5) * col_w)
            color = "#ff6666" if i in weekend else "#909090"
            d.text((x, head_y), n, fill=color, font=f_head, anchor="mm")

        weeks = _cal.Calendar(firstweekday=firstweek).monthdayscalendar(year, month)
        rows = max(1, len(weeks))
        y0 = head_y + max(6, H // 13)
        row_h = max(8, (H - margin - y0) // rows)
        box = max(2, row_h // 2 - 1)
        f_d = get_font(max(7, min(10, row_h - 1)), cjk=False)
        for wi, week in enumerate(weeks):
            for di, day in enumerate(week):
                if day == 0:
                    continue
                x = int((di + 0.5) * col_w)
                y = y0 + wi * row_h + row_h // 2
                if day == today:
                    hw = max(box, (text_w(f_d, str(day)) + 1) // 2)
                    d.rectangle([x - hw, y - box, x + hw, y + box],
                                fill="#2b5fd9")
                    d.text((x, y), str(day), fill="#ffffff",
                           font=f_d, anchor="mm")
                else:
                    color = "#ff8888" if di in weekend else "#d0d0d0"
                    d.text((x, y), str(day), fill=color, font=f_d, anchor="mm")

    def _draw_week(self, d, W, H, t, first):
        now = _dt.date(t.tm_year, t.tm_mon, t.tm_mday)
        if first == "mon":
            start = now - _dt.timedelta(days=now.weekday())
            names = _WEEK_CN
            weekend = (5, 6)
        else:
            start = now - _dt.timedelta(days=(now.weekday() + 1) % 7)
            names = [_WEEK_CN[-1]] + _WEEK_CN[:-1]
            weekend = (0, 6)

        margin = max(3, W // 10)
        f_t = get_font(max(11, min(14, H // 11)), bold=True, cjk=True)
        title_y = max(10, H // 10)
        d.text((W // 2, title_y), f"{t.tm_year}-{t.tm_mon:02d}",
               fill="#f0f0f0", font=f_t, anchor="mm")

        rows_start = max(title_y + 14, H * 2 // 9)
        row_h = max(12, (H - rows_start - 3) // 7)
        box = max(6, row_h // 2 - 1)
        f_day = get_font(max(10, min(13, row_h * 3 // 4)), cjk=True)
        f_num = get_font(max(11, min(16, row_h)), bold=True, cjk=False)
        x_day = max(6, W // 12)
        x_num = W - margin
        for i in range(7):
            day = start + _dt.timedelta(days=i)
            y = rows_start + i * row_h + row_h // 2
            is_today = day == now
            if is_today:
                d.rectangle([3, y - box, W - 4, y + box], fill="#2b5fd9")
            fg = "#ffffff" if is_today else ("#ff8888" if i in weekend else "#c0c0c0")
            d.text((x_day, y), "周" + names[i], fill=fg, font=f_day, anchor="lm")
            d.text((x_num, y), f"{day.day}", fill=fg, font=f_num, anchor="rm")