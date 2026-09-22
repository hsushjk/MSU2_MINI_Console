import datetime as _dt
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

def _color_for(delta):
    if delta == 0:   return "#ffcc00"
    if delta <= 3:   return "#ff4444"
    if delta <= 7:   return "#ff9944"
    if delta <= 30:  return "#4a7fe0"
    return "#a0a0a0"

def _next_occurrence(mm, dd, today):
    year = today.year
    for _ in range(8):
        try:
            t = _dt.date(year, mm, dd)
            if t >= today:
                return t
        except ValueError:
            pass
        year += 1
    return None

@register
class CountdownWidget(Widget):
    name = "countdown"
    interval = 1.0
    orientations = BOTH
    priority = 20

    config_schema = [
        Field("items", "list_str", "倒计时列表",
              default=["春节|01-01", "生日|01-01"],
              help="每行一个：名称|MM-DD（自动滚动）或 名称|YYYY-MM-DD"),
        Field("cycle_enabled", "bool", "循环浏览", default=False,
              help="切换周期至少 15 秒"),
        Field("cycle_sec", "float", "切换周期(秒)", default=20.0,
              min=15.0, max=120.0, step=5.0),
    ]

    def on_enter(self, ctx):

        ctx.state.pop("_last_key", None)

    def render(self, ctx):
        cfg = self.get_config(ctx)
        items = self._parse(cfg["items"])
        W, H = ctx.size

        if not items:
            if ctx.state.get("_last_key") == "__empty__":
                return None
            ctx.state["_last_key"] = "__empty__"
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), "未配置",
                   fill="#666666", font=get_font(14), anchor="mm")
            return img

        items.sort(key=lambda x: x[2])
        cycle = max(15.0, float(cfg["cycle_sec"])) if cfg["cycle_enabled"] else 0

        if ctx.orientation == LANDSCAPE:
            if cycle > 0:
                idx = int(ctx.now / cycle) % len(items)
            else:
                idx = 0
            name, target, delta = items[idx]
            key = f"L|{name}|{target}|{delta}|{idx}"
            if ctx.state.get("_last_key") == key:
                return None
            ctx.state["_last_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            self._draw_big(d, W, H, name, delta)
            return img
        else:
            page_size = 4
            total = len(items)
            if cycle > 0 and total > page_size:
                start = int(ctx.now / cycle) % total
            else:
                start = 0
            shown = []
            for i in range(min(total, page_size)):
                idx = (start + i) % total
                shown.append(items[idx])
            key = "P|" + "|".join(f"{n}:{d}" for n, t, d in shown)
            if ctx.state.get("_last_key") == key:
                return None
            ctx.state["_last_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            row_h = H // len(shown)
            for i, (name, target, delta) in enumerate(shown):
                self._draw_row(d, 0, i * row_h, W, row_h, name, delta)
            return img

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
        elif delta < 0:
            d.text((W // 2, H // 2 + 4), f"已过 {-delta} 天",
                   fill="#666666", font=f_name, anchor="mm")
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
            txt = f"{delta}" if delta > 0 else f"-{-delta}"
            d.text((x + w - 16, y + h // 2), txt,
                   fill=color, font=f_num, anchor="rm")
            d.text((x + w - 4, y + h // 2 + 4), "天",
                   fill="#909090", font=f_unit, anchor="rm")

    @staticmethod
    def _parse(items):
        today = _dt.date.today()
        out = []
        for line in items or []:
            if "|" not in line:
                continue
            name, date_s = line.split("|", 1)
            name = name.strip(); date_s = date_s.strip()
            target = None
            try:
                target = _dt.datetime.strptime(date_s, "%Y-%m-%d").date()
            except ValueError:
                pass
            if target is None:
                try:
                    md = _dt.datetime.strptime(date_s, "%m-%d")
                    target = _next_occurrence(md.month, md.day, today)
                except ValueError:
                    pass
            if target is None:
                continue
            delta = (target - today).days
            out.append((name, target, delta))
        return out
