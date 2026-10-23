import datetime as _dt
import logging
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import row_text, text_w

log = logging.getLogger("msu2.widgets.holiday")

_FIXED = [
    ("元旦", 1, 1),
    ("劳动节", 5, 1),
    ("国庆节", 10, 1),
]

_LUNAR = {
    "春节": {
        2024: (2, 10), 2025: (1, 29), 2026: (2, 17),
        2027: (2, 6),  2028: (1, 26), 2029: (2, 13),
        2030: (2, 3),  2031: (1, 23), 2032: (2, 11),
        2033: (1, 31), 2034: (2, 19), 2035: (2, 8),
        2036: (1, 28), 2037: (2, 15), 2038: (2, 4),
        2039: (1, 24), 2040: (2, 12), 2041: (2, 1),
        2042: (1, 22), 2043: (2, 10), 2044: (1, 30),
        2045: (2, 17),
    },
    "清明": {
        2024: (4, 4),  2025: (4, 4),  2026: (4, 5),
        2027: (4, 5),  2028: (4, 4),  2029: (4, 4),
        2030: (4, 5),  2031: (4, 5),  2032: (4, 4),
        2033: (4, 4),  2034: (4, 5),  2035: (4, 5),
        2036: (4, 4),  2037: (4, 4),  2038: (4, 5),
        2039: (4, 5),  2040: (4, 4),  2041: (4, 4),
        2042: (4, 4),  2043: (4, 5),  2044: (4, 4),
        2045: (4, 4),
    },
    "端午": {
        2024: (6, 10), 2025: (5, 31), 2026: (6, 19),
        2027: (6, 9),  2028: (5, 28), 2029: (6, 16),
        2030: (6, 5),  2031: (6, 24), 2032: (6, 12),
        2033: (6, 1),  2034: (6, 20), 2035: (6, 10),
        2036: (5, 30), 2037: (6, 18), 2038: (6, 7),
        2039: (5, 27), 2040: (6, 14), 2041: (6, 3),
        2042: (6, 22), 2043: (6, 11), 2044: (5, 31),
        2045: (6, 19),
    },
    "中秋": {
        2024: (9, 17), 2025: (10, 6), 2026: (9, 25),
        2027: (9, 15), 2028: (10, 3), 2029: (9, 22),
        2030: (9, 12), 2031: (10, 1), 2032: (9, 19),
        2033: (9, 8),  2034: (9, 27), 2035: (9, 16),
        2036: (10, 4), 2037: (9, 24), 2038: (9, 13),
        2039: (10, 2), 2040: (9, 20), 2041: (9, 10),
        2042: (9, 28), 2043: (9, 17), 2044: (10, 5),
        2045: (9, 25),
    },
}

_MISSING_WARNED = set()

def _color_for(delta):
    if delta < 0:
        return "#666666"
    if delta == 0:
        return "#ffcc00"
    if delta <= 3:
        return "#ff4444"
    if delta <= 7:
        return "#ff9944"
    if delta <= 30:
        return "#4a7fe0"
    return "#a0a0a0"

def _fmt_days(v):
    if -1000 < v < 1000:
        return str(v)
    return f"{v / 1000:.1f}k"

def _fit_font(text, max_w, max_size, bold=True, cjk=True):
    try:
        size = max(8, int(max_size))
    except (TypeError, ValueError):
        size = 12
    while size > 8:
        f = get_font(size, bold=bold, cjk=cjk)
        if text_w(f, text) <= max_w:
            return f
        size -= 2
    return get_font(8, bold=bold, cjk=cjk)

def _name_font(text, max_w, base, floor=9):
    try:
        size = max(int(floor), int(base))
    except (TypeError, ValueError):
        size = 12
    while size > floor:
        f = get_font(size, bold=True, cjk=True)
        if text_w(f, text) <= max_w:
            return f
        size -= 1
    return get_font(max(6, int(floor)), bold=True, cjk=True)

def _parse_md(text):
    parts = str(text).split("-")
    if len(parts) != 2:
        return None
    try:
        mm = int(parts[0])
        dd = int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    return mm, dd

def _resolve_fixed(mm, dd, today):
    for i in range(8):
        try:
            t = _dt.date(today.year + i, mm, dd)
            if t >= today:
                return t
        except ValueError:
            pass
    return None

def _resolve_lunar_map(name, mapping, today):
    for year in sorted(mapping.keys()):
        mm, dd = mapping.get(year, (0, 0))
        try:
            t = _dt.date(year, mm, dd)
            if t >= today:
                return t
        except ValueError:
            continue
    key = (name, today.year)
    if key not in _MISSING_WARNED:
        _MISSING_WARNED.add(key)
        log.warning("农历节日 %s 缺少 %d 年之后的数据，已跳过", name, today.year)
    return None

@register
class HolidayWidget(Widget):
    name = "holiday"
    interval = 1.0
    orientations = BOTH
    priority = 22

    actions = {"next": "下一个", "prev": "上一个"}
    touch_help = "单击=下一个，双击=上一个"

    config_schema = [
        Field("show_builtin", "bool", "显示内置节假日", default=True,
              help="元旦 / 春节 / 清明 / 端午 / 劳动节 / 中秋 / 国庆"),
        Field("extra", "list_str", "自定义节日",
              default=[], help="每行一个：名称|MM-DD"),
        Field("cycle", "bool", "循环浏览", default=False,
              help="打开后按切换周期自动轮播"),
        Field("cycle_interval", "float", "切换周期(秒)", default=10.0,
              min=2.0, max=3600.0, step=1.0),
    ]

    def on_enter(self, ctx):
        ctx.state.pop("_hol_key", None)
        ctx.state["_hol_mono"] = time.monotonic()

    def on_action(self, action, payload, ctx):
        try:
            if action in ("next", "prev", "touch_single", "touch_double"):
                cfg = self.get_config(ctx)
                today = _dt.date.fromtimestamp(ctx.now)
                total = len(self._collect(cfg, today))
                if total <= 0:
                    return {"ok": False, "err": "没有可用的节假日"}
                st = ctx.state
                step = 1 if action in ("next", "touch_single") else -1
                st["_hol_idx"] = (int(st.get("_hol_idx", 0)) + step) % total
                st["_hol_key"] = None
                st["_hol_mono"] = time.monotonic()
                return {"ok": True}
            return {"ok": False, "err": f"未知动作: {action}"}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    @staticmethod
    def _cycle_conf(cfg):
        cyc = bool(cfg.get("cycle"))
        try:
            iv = float(cfg.get("cycle_interval"))
        except (TypeError, ValueError):
            iv = 10.0
        return cyc, max(2.0, min(3600.0, iv))

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        cfg = self.get_config(ctx)
        W, H = ctx.size
        st = ctx.state
        today = _dt.date.fromtimestamp(ctx.now)
        items = self._collect(cfg, today)
        if not items:
            key = ("E", W, H)
            if st.get("_hol_key") == key:
                return None
            st["_hol_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), "未配置节假日", fill="#666666",
                   font=get_font(max(9, min(15, H // 8)), cjk=True), anchor="mm")
            return img
        items.sort(key=lambda x: x[2])
        total = len(items)
        cyc, iv = self._cycle_conf(cfg)
        mono = time.monotonic()
        if cyc and total > 1:
            last = st.get("_hol_mono")
            if not isinstance(last, (int, float)) or mono < last:
                st["_hol_mono"] = mono
            elif mono - last >= iv:
                st["_hol_mono"] = mono
                st["_hol_idx"] = (int(st.get("_hol_idx", 0)) + 1) % total
        else:
            st.pop("_hol_mono", None)
        idx = int(st.get("_hol_idx", 0)) % total
        st["_hol_idx"] = idx
        if ctx.orientation == LANDSCAPE:
            img, moving, sub = self._compose_landscape(ctx, W, H, items[idx], ctx.now)
            key = ("L", W, H, idx, sub)
        else:
            img, moving, sub = self._compose_portrait(ctx, W, H, items, idx, ctx.now)
            key = ("P", W, H, idx, sub)
        if not moving and st.get("_hol_key") == key:
            return None
        st["_hol_key"] = key
        return img

    def _compose_landscape(self, ctx, W, H, item, now):
        name, target, delta = item
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        margin = max(3, W // 32)
        fs = max(9, min(15, H // 6))
        y1 = max(10, H * 3 // 10)
        cy = min(H - 10, max(y1 + fs + 8, H * 2 // 3))
        max_w = W - 2 * margin
        f_name = _name_font(name, max_w, fs)
        tw = text_w(f_name, name)
        moving = tw > max_w
        start_x = margin if moving else margin + max(0, (max_w - tw) // 2)
        row_text(img, (start_x, y1), name, f_name, "#e8e8e8",
                 max_w, now, scroll=True)
        if delta == 0:
            txt = "就是今天"
            color = _color_for(0)
        elif delta < 0:
            txt = f"已过 {_fmt_days(-delta)} 天"
            color = "#666666"
        else:
            txt = f"还有 {_fmt_days(delta)} 天"
            color = _color_for(delta)
        f_txt = _fit_font(txt, W - 2 * margin, max(14, H // 4))
        d.text((W // 2, cy), txt, fill=color, font=f_txt, anchor="mm")
        return img, moving, (name, delta, txt)

    def _compose_portrait(self, ctx, W, H, items, idx, now):
        total = len(items)
        if total == 1:
            return self._compose_portrait_single(ctx, W, H, items[0], now)
        rows = max(1, H // 26)
        row_h = max(1, H // rows)
        count = min(total, rows)
        start = 0 if total <= rows else idx
        y0 = max(0, (H - row_h * count) // 2)
        margin = max(3, W // 20)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        all_visible = total <= rows
        moving = False
        sub = []
        fs_name = max(9, min(12, row_h // 2 - 1))
        f_unit = get_font(max(8, min(11, row_h // 3)), cjk=True)
        fs_val = max(10, min(16, row_h // 2))
        f_val = get_font(fs_val, bold=True, cjk=True)
        unit_x = W - margin
        for i in range(count):
            j = (start + i) % total
            name, target, delta = items[j]
            y = y0 + i * row_h
            cy = y + row_h // 2
            if all_visible and j == idx:
                d.rectangle([0, y + 1, W - 1, y + row_h - 1], fill="#1b3550")
            if delta == 0:
                vtxt = "今天"
                color = _color_for(0)
                num_right = unit_x
                unit = ""
            else:
                vtxt = _fmt_days(delta)
                color = _color_for(delta)
                unit = "天"
                num_right = unit_x - text_w(f_unit, unit) - 2
            num_w = text_w(f_val, vtxt)
            name_x = margin
            max_w = max(8, num_right - num_w - 3 - name_x)
            f_name = _name_font(name, max_w, fs_name)
            tw = text_w(f_name, name)
            if tw > max_w:
                moving = True
            row_text(img, (name_x, cy), name, f_name, "#d8d8d8",
                     max_w, now, scroll=True)
            d.text((num_right, cy), vtxt, fill=color, font=f_val, anchor="rm")
            if unit:
                d.text((unit_x, cy), unit, fill="#909090",
                       font=f_unit, anchor="rm")
            sub.append((name, delta, vtxt))
        return img, moving, (tuple(sub), start, all_visible)

    def _compose_portrait_single(self, ctx, W, H, item, now):
        name, target, delta = item
        margin = max(3, W // 20)
        max_w = W - 2 * margin
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        fs_name = max(11, min(20, H // 10))
        f_name = _name_font(name, max_w, fs_name, 10)
        tw = text_w(f_name, name)
        moving = tw > max_w
        start_x = margin if moving else margin + max(0, (max_w - tw) // 2)
        row_text(img, (start_x, H * 3 // 10), name, f_name, "#e8e8e8",
                 max_w, now, scroll=True)
        if delta == 0:
            txt = "就是今天"
            color = _color_for(0)
        elif delta < 0:
            txt = f"已过 {_fmt_days(-delta)} 天"
            color = "#666666"
        else:
            txt = f"还有 {_fmt_days(delta)} 天"
            color = _color_for(delta)
        f_txt = _fit_font(txt, max_w, max(16, H // 5))
        d.text((W // 2, H * 3 // 5), txt, fill=color, font=f_txt, anchor="mm")
        return img, moving, ("single", name, delta, txt)

    def _collect(self, cfg, today):
        out = []
        if cfg.get("show_builtin", True):
            for name, mm, dd in _FIXED:
                t = _resolve_fixed(mm, dd, today)
                if t:
                    out.append((name, t, (t - today).days))
            for name, mapping in _LUNAR.items():
                t = _resolve_lunar_map(name, mapping, today)
                if t:
                    out.append((name, t, (t - today).days))
        for line in cfg.get("extra", []) or []:
            if not isinstance(line, str) or "|" not in line:
                continue
            name, date_s = line.split("|", 1)
            name = name.strip()
            date_s = date_s.strip()
            if not name:
                name = "节日"
            md = _parse_md(date_s)
            if md is None:
                continue
            t = _resolve_fixed(md[0], md[1], today)
            if t:
                out.append((name, t, (t - today).days))
        return out