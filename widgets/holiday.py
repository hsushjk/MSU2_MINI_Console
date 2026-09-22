import datetime as _dt
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

_FIXED = [
    ("元旦", 1, 1),
    ("劳动节", 5, 1),
    ("国庆节", 10, 1),
]

_LUNAR = {
    "春节": {
        2024: (2, 10), 2025: (1, 29), 2026: (2, 17),
        2027: (2, 6),  2028: (1, 26), 2029: (2, 13), 2030: (2, 3),
    },
    "清明": {
        2024: (4, 4), 2025: (4, 4), 2026: (4, 5),
        2027: (4, 5), 2028: (4, 4), 2029: (4, 4), 2030: (4, 5),
    },
    "端午": {
        2024: (6, 10), 2025: (5, 31), 2026: (6, 19),
        2027: (6, 9),  2028: (5, 28), 2029: (6, 16), 2030: (6, 5),
    },
    "中秋": {
        2024: (9, 17), 2025: (10, 6), 2026: (9, 25),
        2027: (9, 15), 2028: (10, 3), 2029: (9, 22), 2030: (9, 12),
    },
}

def _color_for(delta):

    if delta == 0:   return "#ffcc00"
    if delta <= 3:   return "#ff4444"
    if delta <= 7:   return "#ff9944"
    if delta <= 30:  return "#4a7fe0"
    return "#a0a0a0"

def _resolve_fixed(mm, dd, today):

    try:
        t = _dt.date(today.year, mm, dd)
        if t >= today:
            return t
    except ValueError:
        pass
    try:
        return _dt.date(today.year + 1, mm, dd)
    except ValueError:
        return None

def _resolve_lunar_map(mapping, today):

    for year in sorted(mapping.keys()):
        mm, dd = mapping[year]
        try:
            t = _dt.date(year, mm, dd)
            if t >= today:
                return t
        except ValueError:
            continue
    return None

@register
class HolidayWidget(Widget):
    name = "holiday"
    interval = 60.0
    orientations = BOTH
    priority = 22

    config_schema = [
        Field("show_builtin", "bool", "显示内置节假日", default=True,
              help="元旦 / 春节 / 清明 / 端午 / 劳动节 / 中秋 / 国庆"),
        Field("extra", "list_str", "自定义节日",
              default=[], help="每行一个：名称|MM-DD"),
    ]

    def render(self, ctx):
        cfg = self.get_config(ctx)
        today = _dt.date.today()
        items = self._collect(cfg, today)
        W, H = ctx.size

        if not items:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), "未配置",
                   fill="#666666", font=get_font(14), anchor="mm")
            return img

        items.sort(key=lambda x: x[2])

        if ctx.orientation == LANDSCAPE:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            name, target, delta = items[0]
            self._draw_big(d, W, H, name, delta)
            return img
        else:
            page = items[:4]
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            row_h = H // len(page)
            for i, (name, target, delta) in enumerate(page):
                self._draw_row(d, 0, i * row_h, W, row_h, name, delta)
            return img

    def _collect(self, cfg, today):
        out = []
        if cfg.get("show_builtin", True):

            for name, mm, dd in _FIXED:
                t = _resolve_fixed(mm, dd, today)
                if t:
                    out.append((name, t, (t - today).days))

            for name, mapping in _LUNAR.items():
                t = _resolve_lunar_map(mapping, today)
                if t:
                    out.append((name, t, (t - today).days))

        for line in cfg.get("extra", []) or []:
            if "|" not in line:
                continue
            name, date_s = line.split("|", 1)
            name = name.strip(); date_s = date_s.strip()
            try:
                md = _dt.datetime.strptime(date_s, "%m-%d")
                t = _resolve_fixed(md.month, md.day, today)
                if t:
                    out.append((name, t, (t - today).days))
            except ValueError:
                continue
        return out

    def _draw_big(self, d, W, H, name, delta):
        f_lbl = get_font(13, cjk=True)
        f_name = get_font(16, bold=True, cjk=True)
        f_num = get_font(40, bold=True, cjk=False)
        f_unit = get_font(14, cjk=True)

        d.text((W // 2, 12), f"距离 {name} 还有",
               fill="#c0c0c0", font=f_lbl, anchor="mm")

        color = _color_for(delta)
        if delta == 0:
            d.text((W // 2, H // 2 + 4), "今天",
                   fill=color, font=f_name, anchor="mm")
        else:
            d.text((W // 2, H // 2 + 6), str(delta),
                   fill=color, font=f_num, anchor="mm")
            d.text((W - 10, H // 2 + 14), "天",
                   fill="#c0c0c0", font=f_unit, anchor="rm")

    def _draw_row(self, d, x, y, w, h, name, delta):
        f_name = get_font(11, cjk=True)
        f_num = get_font(16, bold=True, cjk=False)
        f_unit = get_font(9, cjk=True)
        d.text((x + 6, y + h // 2), name, fill="#d0d0d0",
               font=f_name, anchor="lm")
        color = _color_for(delta)
        if delta == 0:
            d.text((x + w - 6, y + h // 2), "今天",
                   fill=color, font=f_name, anchor="rm")
        else:
            d.text((x + w - 16, y + h // 2), str(delta),
                   fill=color, font=f_num, anchor="rm")
            d.text((x + w - 4, y + h // 2 + 4), "天",
                   fill="#909090", font=f_unit, anchor="rm")
