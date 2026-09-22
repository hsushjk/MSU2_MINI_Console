import time
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

@register
class ProcessTopWidget(Widget):
    name = "process_top"
    interval = 2.0
    orientations = BOTH
    priority = 34

    config_schema = [
        Field("sort_by", "select", "排序依据", default="cpu",
              options=["cpu", "memory"]),
        Field("show_n", "int", "显示条数", default=5, min=3, max=8),
        Field("cycle_enabled", "bool", "循环浏览", default=False),
        Field("cycle_sec", "float", "切换周期(秒)", default=20.0,
              min=15.0, max=120.0, step=5.0),
    ]

    def setup(self, ctx):
        ctx.state["_pt"] = {"procs": [], "t": 0.0, "last_key": None}

    def on_enter(self, ctx):
        st = ctx.state.setdefault("_pt", {})
        st["last_key"] = None

    def render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state.setdefault("_pt", {"procs": [], "t": 0.0, "last_key": None})

        now = time.time()
        if now - st["t"] > 3.0 or not st["procs"]:
            st["procs"] = self._collect(cfg["sort_by"])
            st["t"] = now

        procs = st["procs"]
        if not procs:
            img = Image.new("RGBA", ctx.size, (0, 0, 0, 0))
            ImageDraw.Draw(img).text(
                (ctx.size[0] // 2, ctx.size[1] // 2), "no data",
                fill="#666", font=get_font(14), anchor="mm")
            return img

        show_n = int(cfg["show_n"])
        cycle = max(15.0, float(cfg["cycle_sec"])) if cfg["cycle_enabled"] else 0
        if cycle > 0 and len(procs) > show_n:
            start = int(now / cycle) % len(procs)
        else:
            start = 0
        shown = [procs[(start + i) % len(procs)] for i in range(min(show_n, len(procs)))]

        key = "|".join(f"{n}:{v}" for n, v, _ in shown)
        if st["last_key"] == key:
            return None
        st["last_key"] = key

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if ctx.orientation == LANDSCAPE:
            self._draw_grid(d, W, H, shown)
        else:
            self._draw_rows(d, W, H, shown)
        return img

    def _collect(self, sort_by):
        out = []
        for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            try:
                info = p.info
                name = info.get("name") or "?"
                if sort_by == "memory":
                    v = info.get("memory_percent") or 0.0
                else:
                    v = info.get("cpu_percent") or 0.0
                out.append((name, v, "cpu" if sort_by == "cpu" else "mem"))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        out.sort(key=lambda x: -x[1])
        return out

    def _draw_grid(self, d, W, H, rows):

        n = len(rows)
        cols = 2
        r = (n + 1) // cols
        cw, ch = W // cols, H // r
        f_n = get_font(9, cjk=False)
        f_v = get_font(11, bold=True, cjk=False)
        for i, (name, v, unit) in enumerate(rows):
            x = (i % cols) * cw
            y = (i // cols) * ch
            max_w = cw - 8
            label = name if f_n.getlength(name) <= max_w else name[:10] + "…"
            d.text((x + 4, y + 2), label, fill="#c0c0c0", font=f_n)
            txt = f"{int(v)}%" if unit == "cpu" else f"{v:.1f}%"
            d.text((x + cw - 4, y + ch - 14), txt,
                   fill="#00e0ff" if unit == "cpu" else "#00ff80",
                   font=f_v, anchor="ra")

    def _draw_rows(self, d, W, H, rows):
        row_h = H // len(rows)
        f_n = get_font(10, cjk=False)
        f_v = get_font(13, bold=True, cjk=False)
        for i, (name, v, unit) in enumerate(rows):
            y = i * row_h
            max_w = W - 40
            label = name if f_n.getlength(name) <= max_w else name[:8] + "…"
            d.text((4, y + row_h // 2), label,
                   fill="#c0c0c0", font=f_n, anchor="lm")
            txt = f"{int(v)}%" if unit == "cpu" else f"{v:.0f}%"
            d.text((W - 4, y + row_h // 2), txt,
                   fill="#00e0ff" if unit == "cpu" else "#00ff80",
                   font=f_v, anchor="rm")
