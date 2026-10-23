import datetime as _dt
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import row_text, text_w

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

def _parse_date(text):
    try:
        return _dt.datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

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

@register
class AnniversaryWidget(Widget):
    name = "anniversary"
    interval = 1.0
    orientations = BOTH
    priority = 25

    actions = {"next": "下一个", "prev": "上一个"}
    touch_help = "单击=下一个，双击=上一个"

    config_schema = [
        Field("items", "list_str", "纪念日列表",
              default=["被霸道总裁爱上|2020-01-01"],
              help="每行一条：名称|YYYY-MM-DD"),
        Field("unit", "select", "单位", default="天",
              options=["天", "周", "月"]),
        Field("cycle", "bool", "循环浏览", default=False,
              help="条数超过一屏时自动轮播"),
        Field("cycle_interval", "float", "切换周期(秒)", default=10.0,
              min=2.0, max=3600.0, step=1.0),
    ]

    def setup(self, ctx):
        try:
            store = ctx.config.setdefault("widget_config", {})
            if isinstance(store, dict) and not isinstance(store.get(self.name), dict):
                store[self.name] = {}
        except Exception:
            pass

    def on_enter(self, ctx):
        ctx.state.pop("_ann_key", None)
        ctx.state["_ann_mono"] = time.monotonic()

    def on_action(self, action, payload, ctx):
        try:
            if action in ("next", "prev", "touch_single", "touch_double"):
                cfg = self.get_config(ctx)
                today = _dt.date.fromtimestamp(ctx.now)
                total = len(self._parse(cfg, today))
                if total <= 0:
                    return {"ok": False, "err": "没有可用的纪念日"}
                st = ctx.state
                step = 1 if action in ("next", "touch_single") else -1
                st["_ann_idx"] = (int(st.get("_ann_idx", 0)) + step) % total
                st["_ann_key"] = None
                st["_ann_mono"] = time.monotonic()
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
        unit = cfg.get("unit") if cfg.get("unit") in ("天", "周", "月") else "天"
        raw = cfg.get("items") or []
        items = self._parse(cfg, today)
        if not items:
            has_raw = any(isinstance(x, str) and x.strip() for x in raw)
            err = "日期格式错误" if has_raw else "未配置纪念日"
            key = ("E", W, H, err)
            if st.get("_ann_key") == key:
                return None
            st["_ann_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), err, fill="#666666",
                   font=get_font(max(9, min(15, H // 8)), cjk=True), anchor="mm")
            return img
        total = len(items)
        cyc, iv = self._cycle_conf(cfg)
        mono = time.monotonic()
        if cyc and total > 1:
            last = st.get("_ann_mono")
            if not isinstance(last, (int, float)) or mono < last:
                st["_ann_mono"] = mono
            elif mono - last >= iv:
                st["_ann_mono"] = mono
                st["_ann_idx"] = (int(st.get("_ann_idx", 0)) + 1) % total
        else:
            st.pop("_ann_mono", None)
        idx = int(st.get("_ann_idx", 0)) % total
        st["_ann_idx"] = idx
        if ctx.orientation == LANDSCAPE:
            img, moving, sub = self._compose_landscape(ctx, W, H, items, idx, unit, ctx.now)
            key = ("L", W, H, idx, unit, sub)
        else:
            img, moving, sub = self._compose_portrait(ctx, W, H, items, idx, unit, ctx.now)
            key = ("P", W, H, idx, unit, sub)
        if not moving and st.get("_ann_key") == key:
            return None
        st["_ann_key"] = key
        return img

    def _compose_landscape(self, ctx, W, H, items, idx, unit, now):
        name, start_date, value = items[idx]
        margin = max(3, W // 24)
        max_w = W - 2 * margin
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f_name = _name_font(name, max_w, max(10, min(16, H // 6)), 9)
        tw = text_w(f_name, name)
        moving = tw > max_w
        start_x = margin if moving else margin + max(0, (max_w - tw) // 2)
        row_text(img, (start_x, H * 3 // 10), name, f_name, "#d8d8d8",
                 max_w, now, scroll=True)
        vtxt = f"{value} {unit}"
        f_val = _fit_font(vtxt, max_w, max(15, H // 4))
        d.text((W // 2, H * 3 // 5), vtxt, fill="#ff6b9d", font=f_val,
               anchor="mm")
        return img, moving, ("single", idx, name, value)

    def _compose_portrait_single(self, ctx, W, H, item, unit, now):
        name, start_date, value = item
        margin = max(3, W // 20)
        max_w = W - 2 * margin
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        fs_name = max(11, min(18, H // 9))
        f_name = _name_font(name, max_w, fs_name, 10)
        tw = text_w(f_name, name)
        moving = tw > max_w
        start_x = margin if moving else margin + max(0, (max_w - tw) // 2)
        row_text(img, (start_x, H * 3 // 10), name, f_name, "#d8d8d8",
                 max_w, now, scroll=True)
        vtxt = f"{value} {unit}"
        f_val = _fit_font(vtxt, max_w, max(16, H // 5))
        d.text((W // 2, H * 3 // 5), vtxt, fill="#ff6b9d", font=f_val,
               anchor="mm")
        return img, moving, ("single", name, value)

    def _compose_portrait(self, ctx, W, H, items, idx, unit, now):
        total = len(items)
        if total == 1:
            return self._compose_portrait_single(ctx, W, H, items[0], unit, now)
        rows = max(2, H // 50)
        row_h = max(1, H // rows)
        count = min(total, rows)
        start = 0 if total <= rows else idx
        y0 = max(0, (H - row_h * count) // 2)
        margin = max(3, W // 20)
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        all_visible = total <= rows and total > 1
        fs_name = max(9, min(14, row_h // 4))
        val_max = max(12, min(24, row_h // 2))
        max_w = W - 2 * margin
        moving = False
        sub = []
        for i in range(count):
            j = (start + i) % total
            name, start_date, value = items[j]
            y = y0 + i * row_h
            if all_visible and j == idx:
                d.rectangle([0, y + 1, W - 1, y + row_h - 1], fill="#1b3550")
            f_name = _name_font(name, max_w, fs_name)
            tw = text_w(f_name, name)
            if tw > max_w:
                moving = True
            row_text(img, (margin, y + row_h * 3 // 10), name, f_name,
                     "#d8d8d8", max_w, now, scroll=True)
            vtxt = f"{value} {unit}"
            f_val = _fit_font(vtxt, max_w, val_max)
            d.text((W - margin, y + row_h * 7 // 10), vtxt, fill="#ff6b9d",
                   font=f_val, anchor="rm")
            sub.append((name, value))
        return img, moving, (tuple(sub), start, all_visible)

    def _parse(self, cfg, today):
        unit = cfg.get("unit") if cfg.get("unit") in ("天", "周", "月") else "天"
        out = []
        for line in cfg.get("items") or []:
            if not isinstance(line, str) or "|" not in line:
                continue
            name, date_s = line.split("|", 1)
            name = name.strip()
            start = _parse_date(date_s)
            if start is None:
                continue
            if not name:
                name = "纪念日"
            days = (today - start).days
            if unit == "周":
                value = days // 7
            elif unit == "月":
                value = (today.year - start.year) * 12 + (today.month - start.month)
                if today.day < start.day:
                    value -= 1
            else:
                value = days
            out.append((name, start, value))
        return out