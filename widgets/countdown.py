import datetime as _dt
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import row_text, text_w

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

def _fit_font(text, max_w, max_size):
    try:
        size = max(8, int(max_size))
    except (TypeError, ValueError):
        size = 12
    while size > 8:
        f = get_font(size, bold=True, cjk=True)
        if text_w(f, text) <= max_w:
            return f
        size -= 2
    return get_font(8, bold=True, cjk=True)

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
    return get_font(max(6, min(int(floor), size)), bold=True, cjk=True)

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

    actions = {"next": "下一个", "prev": "上一个"}
    touch_help = "单击=下一个，双击=上一个"

    config_schema = [
        Field("items", "list_str", "倒计时列表",
              default=["元旦|01-01", "生日|01-01"],
              help="每行一个：名称|MM-DD（每年重复）或 名称|YYYY-MM-DD"),
        Field("cycle", "bool", "循环浏览", default=False,
              help="打开后按切换周期自动轮播"),
        Field("cycle_interval", "float", "切换周期(秒)", default=10.0,
              min=2.0, max=3600.0, step=1.0),
    ]

    def on_enter(self, ctx):
        ctx.state.pop("_cd_key", None)
        ctx.state["_cd_mono"] = time.monotonic()

    def on_action(self, action, payload, ctx):
        try:
            if action in ("next", "prev", "touch_single", "touch_double"):
                cfg = self.get_config(ctx)
                today = _dt.date.fromtimestamp(ctx.now)
                total = len(self._parse(cfg.get("items"), today))
                if total <= 0:
                    return {"ok": False, "err": "没有可用的倒计时"}
                st = ctx.state
                step = 1 if action in ("next", "touch_single") else -1
                st["_cd_idx"] = (int(st.get("_cd_idx", 0)) + step) % total
                st["_cd_key"] = None
                st["_cd_mono"] = time.monotonic()
                return {"ok": True}
            return {"ok": False, "err": f"未知动作: {action}"}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    def _cycle_conf(self, ctx, cfg):
        cyc = bool(cfg.get("cycle"))
        iv = cfg.get("cycle_interval", 10.0)
        try:
            iv = float(iv)
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
        items = self._parse(cfg.get("items"), today)
        if not items:
            key = ("E", W, H)
            if st.get("_cd_key") == key:
                return None
            st["_cd_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), "未配置倒计时", fill="#666666",
                   font=get_font(max(9, min(15, H // 8)), cjk=True), anchor="mm")
            return img
        items.sort(key=lambda x: x[2])
        total = len(items)
        cyc, iv = self._cycle_conf(ctx, cfg)
        mono = time.monotonic()
        if cyc and total > 1:
            last = st.get("_cd_mono")
            if not isinstance(last, (int, float)) or mono < last:
                st["_cd_mono"] = mono
            elif mono - last >= iv:
                st["_cd_mono"] = mono
                st["_cd_idx"] = (int(st.get("_cd_idx", 0)) + 1) % total
        else:
            st.pop("_cd_mono", None)
        idx = int(st.get("_cd_idx", 0)) % total
        st["_cd_idx"] = idx
        if ctx.orientation == LANDSCAPE:
            img, moving, sub = self._compose_landscape(ctx, W, H, items[idx], ctx.now)
            key = ("L", W, H, idx, sub)
        else:
            img, moving, sub = self._compose_portrait(ctx, W, H, items, idx, ctx.now)
            key = ("P", W, H, idx, sub)
        if not moving and st.get("_cd_key") == key:
            return None
        st["_cd_key"] = key
        return img

    def _compose_landscape(self, ctx, W, H, item, now):
        name, target, delta = item
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        margin = max(3, W // 32)
        fs = max(9, min(15, H // 6))
        f_lbl = get_font(fs, cjk=True)
        y1 = max(8, H // 5)
        cy = H - max(12, H // 4)
        unit = "天"
        if delta > 0:
            gap = max(2, W // 40)
            lx = margin
            rx = W - margin
            d.text((lx, y1), "距离", fill="#c0c0c0", font=f_lbl, anchor="lm")
            d.text((rx, y1), "还有", fill="#c0c0c0", font=f_lbl, anchor="rm")
            x0 = lx + text_w(f_lbl, "距离") + gap
            x1 = rx - text_w(f_lbl, "还有") - gap
            max_w = max(8, x1 - x0)
            f_name = _name_font(name, max_w, fs)
            tw = text_w(f_name, name)
            moving = tw > max_w
            start_x = x0 if moving else x0 + max(0, (max_w - tw) // 2)
            row_text(img, (start_x, y1), name, f_name, "#e8e8e8",
                     max_w, now, scroll=True)
            f_unit = get_font(max(9, min(15, H // 6)), cjk=True)
            num = _fmt_days(delta)
            ugap = max(2, W // 60)
            f_num = _fit_font(num, W - 4 * margin - text_w(f_unit, unit) - ugap,
                              H // 2)
            num_w = text_w(f_num, num)
            unit_w = text_w(f_unit, unit)
            total_w = num_w + ugap + unit_w
            gx = max(margin, (W - total_w) // 2)
            d.text((gx, cy), num, fill=_color_for(delta), font=f_num, anchor="lm")
            d.text((gx + num_w + ugap, cy), unit, fill="#c0c0c0",
                   font=f_unit, anchor="lm")
            return img, moving, ("f", name, delta, num)
        txt = "今天" if delta == 0 else f"已过 {_fmt_days(-delta)} 天"
        max_w = W - 2 * margin
        f_name = _name_font(name, max_w, fs)
        tw = text_w(f_name, name)
        moving = tw > max_w
        start_x = margin if moving else margin + max(0, (max_w - tw) // 2)
        row_text(img, (start_x, y1), name, f_name, "#e8e8e8",
                 max_w, now, scroll=True)
        f_txt = _fit_font(txt, W - 2 * margin, H // 2)
        d.text((W // 2, cy), txt, fill=_color_for(delta), font=f_txt, anchor="mm")
        return img, moving, ("p", name, delta, txt)

    def _compose_portrait_single(self, ctx, W, H, item, now):
        name, target, delta = item
        margin = max(3, W // 20)
        max_w = W - 2 * margin
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        fs_name = max(11, min(18, H // 9))
        f_name = _name_font(name, max_w, fs_name, 10)
        tw = text_w(f_name, name)
        moving = tw > max_w
        start_x = margin if moving else margin + max(0, (max_w - tw) // 2)
        row_text(img, (start_x, H * 3 // 10), name, f_name, "#e8e8e8",
                 max_w, now, scroll=True)
        if delta == 0:
            txt = "今天"
        elif delta < 0:
            txt = f"已过 {_fmt_days(-delta)} 天"
        else:
            txt = f"{_fmt_days(delta)} 天"
        f_txt = _fit_font(txt, max_w, max(16, H // 5))
        d.text((W // 2, H * 3 // 5), txt, fill=_color_for(delta), font=f_txt,
               anchor="mm")
        return img, moving, ("single", name, delta, txt)

    def _compose_portrait(self, ctx, W, H, items, idx, now):
        total = len(items)
        if total == 1:
            return self._compose_portrait_single(ctx, W, H, items[0], now)
        rows = max(2, H // 50)
        row_h = max(1, H // rows)
        count = min(total, rows)
        start = 0 if total <= rows else idx
        y0 = max(0, (H - row_h * count) // 2)
        margin = max(3, W // 20)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        all_visible = total <= rows and total > 1
        moving = False
        sub = []
        for i in range(count):
            j = (start + i) % total
            name, target, delta = items[j]
            y = y0 + i * row_h
            if all_visible and j == idx:
                d.rectangle([0, y + 1, W - 1, y + row_h - 1], fill="#1b3550")
            fs_name = max(9, min(14, row_h // 4))
            max_w = W - 2 * margin
            f_name = _name_font(name, max_w, fs_name)
            tw = text_w(f_name, name)
            if tw > max_w:
                moving = True
            start_x = margin if tw > max_w else margin + max(0, (max_w - tw) // 2)
            row_text(img, (start_x, y + row_h // 3), name, f_name, "#d8d8d8",
                     max_w, now, scroll=True)
            if delta == 0:
                vtxt = "今天"
                color = _color_for(0)
            elif delta < 0:
                vtxt = f"已过 {_fmt_days(-delta)} 天"
                color = "#a0a0a0"
            else:
                vtxt = f"{_fmt_days(delta)} 天"
                color = _color_for(delta)
            f_val = _fit_font(vtxt, W - 2 * margin,
                              max(10, min(22, row_h // 2)))
            d.text((W - margin, y + row_h * 2 // 3), vtxt, fill=color,
                   font=f_val, anchor="rm")
            sub.append((name, delta, vtxt))
        return img, moving, (tuple(sub), start, all_visible)

    @staticmethod
    def _parse(items, today):
        out = []
        for line in items or []:
            if not isinstance(line, str) or "|" not in line:
                continue
            name, date_s = line.split("|", 1)
            name = name.strip()
            date_s = date_s.strip()
            if not name:
                name = "倒计时"
            target = None
            try:
                target = _dt.datetime.strptime(date_s, "%Y-%m-%d").date()
            except ValueError:
                pass
            if target is None:
                md = _parse_md(date_s)
                if md is not None:
                    target = _next_occurrence(md[0], md[1], today)
            if target is None:
                continue
            out.append((name, target, (target - today).days))
        return out