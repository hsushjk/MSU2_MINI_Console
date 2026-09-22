import calendar as _cal
import datetime as _dt
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

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

    def render(self, ctx):
        cfg = self.get_config(ctx)
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        t = time.localtime(ctx.now)

        if ctx.orientation == LANDSCAPE:
            self._draw_month(d, W, H, t, cfg)
        else:
            self._draw_week(d, W, H, t)
        return img

    def _draw_month(self, d, W, H, t, cfg):
        firstweek = 0 if cfg["first_weekday"] == "mon" else 6
        year, month, today = t.tm_year, t.tm_mon, t.tm_mday

        f_title = get_font(12, bold=True, cjk=False)
        d.text((W // 2, 5), f"{year}-{month:02d}",
               fill="#f0f0f0", font=f_title, anchor="mm")

        names = _WEEK_CN if cfg["first_weekday"] == "mon" \
            else [_WEEK_CN[-1]] + _WEEK_CN[:-1]
        f = get_font(8, cjk=True)
        col_w = W / 7
        for i, n in enumerate(names):
            x = int((i + 0.5) * col_w)
            color = "#ff6666" if i >= 5 else "#909090"
            d.text((x, 18), n, fill=color, font=f, anchor="mm")

        weeks = _cal.Calendar(firstweekday=firstweek).monthdayscalendar(year, month)
        f_d = get_font(9, cjk=False)
        row_h = 10
        y0 = 26
        for wi, week in enumerate(weeks):
            if y0 + wi * row_h + row_h > H - 2:
                break
            for di, day in enumerate(week):
                if day == 0:
                    continue
                x = int((di + 0.5) * col_w)
                y = y0 + wi * row_h + row_h // 2
                if day == today:
                    d.rectangle([x - 5, y - 5, x + 5, y + 5], fill="#2b5fd9")
                    d.text((x, y), str(day), fill="#ffffff",
                           font=f_d, anchor="mm")
                else:
                    color = "#ff8888" if di >= 5 else "#d0d0d0"
                    d.text((x, y), str(day), fill=color, font=f_d, anchor="mm")

    def _draw_week(self, d, W, H, t):
        now = _dt.date(t.tm_year, t.tm_mon, t.tm_mday)
        monday = now - _dt.timedelta(days=now.weekday())

        f_t = get_font(13, bold=True, cjk=True)
        d.text((W // 2, 14), f"{t.tm_year}-{t.tm_mon:02d}",
               fill="#f0f0f0", font=f_t, anchor="mm")

        f_day = get_font(11, cjk=True)
        f_num = get_font(14, bold=True, cjk=False)
        y = 34
        for i in range(7):
            day = monday + _dt.timedelta(days=i)
            is_today = (day == now)
            if is_today:
                d.rectangle([6, y - 9, W - 6, y + 9], fill="#2b5fd9")
            fg = "#ffffff" if is_today else ("#ff8888" if i >= 5 else "#c0c0c0")
            d.text((14, y), "周" + _WEEK_CN[i], fill=fg, font=f_day, anchor="lm")
            d.text((W - 12, y), f"{day.day}",
                   fill=fg, font=f_num, anchor="rm")
            y += 18
