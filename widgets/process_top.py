import time
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import row_text, text_w

@register
class ProcessTopWidget(Widget):
    name = "process_top"
    interval = 2.0
    orientations = BOTH
    priority = 34

    config_schema = [
        Field("sort_by", "select", "排序依据", default="cpu",
              options=["cpu", "memory"]),
        Field("show_n", "int", "显示条数", default=6, min=3, max=8),
    ]

    def setup(self, ctx):
        ctx.state["_pt"] = {"procs": [], "t": 0.0, "last_key": None}
        try:
            for p in psutil.process_iter():
                try:
                    p.cpu_percent(None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

    def on_enter(self, ctx):
        st = ctx.state.setdefault("_pt", {"procs": [], "t": 0.0, "last_key": None})
        st["last_key"] = None

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state.get("_pt")
        if not isinstance(st, dict):
            st = {"procs": [], "t": 0.0, "last_key": None}
            ctx.state["_pt"] = st

        now = time.time()
        sort_by = "memory" if cfg.get("sort_by") == "memory" else "cpu"
        try:
            last_t = float(st.get("t") or 0.0)
        except (TypeError, ValueError):
            last_t = 0.0
        procs = st.get("procs")
        if not isinstance(procs, list) or not procs or now - last_t > 3.0:
            procs = self._collect(sort_by)
            st["procs"] = procs
            st["t"] = now

        W, H = ctx.size
        if not procs:
            key = ("noshow", W, H, ctx.orientation)
            if st.get("last_key") == key:
                return None
            st["last_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(img).text(
                (W // 2, H // 2), "no data",
                fill="#666", font=get_font(14), anchor="mm")
            return img

        show_n = self._clamp_int(cfg.get("show_n"), 6, 1, 8)
        shown = procs[:show_n]
        lay = self._layout(W, H, ctx.orientation, len(shown))
        scrolling = False
        for name, _, _ in shown:
            if text_w(lay["f_n"], str(name or "?")) > lay["name_w"]:
                scrolling = True
                break
        key = (ctx.orientation, W, H, sort_by, show_n,
               tuple(f"{n}|{v:.1f}|{u}" for n, v, u in shown))
        if scrolling:
            key = key + (int(float(ctx.now) * 28.0),)
        if st.get("last_key") == key:
            return None
        st["last_key"] = key

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        self._draw(img, lay, shown, ctx.now)
        return img

    def _collect(self, sort_by):
        out = []
        try:
            it = psutil.process_iter(["name", "cpu_percent", "memory_percent"])
        except Exception:
            return out
        for p in it:
            try:
                info = p.info
                name = str(info.get("name") or "?")
                raw = info.get("memory_percent") if sort_by == "memory" else info.get("cpu_percent")
                try:
                    v = float(raw or 0.0)
                except (TypeError, ValueError):
                    v = 0.0
                out.append((name, v, "cpu" if sort_by == "cpu" else "mem"))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        out.sort(key=lambda x: -x[1])
        return out

    def _layout(self, W, H, orientation, n):
        n = max(1, int(n))
        if orientation == LANDSCAPE:
            cols = 1 if n <= 2 else 2
            rows = max(1, (n + cols - 1) // cols)
            cw = max(1, W // cols)
            ch = max(1, H // rows)
            f_n = get_font(9 if ch >= 22 else 8, cjk=False)
            f_v = get_font(11 if ch >= 22 else 9, bold=True, cjk=False)
        else:
            cols = 1
            rows = n
            cw = max(1, W)
            ch = max(1, H // rows)
            f_n = get_font(10 if ch >= 22 else 8, cjk=False)
            f_v = get_font(13 if ch >= 24 else 11, bold=True, cjk=False)
        val_w = text_w(f_v, "100%")
        name_w = max(8, cw - 12 - val_w)
        return {"cols": cols, "cw": cw, "ch": ch, "n": n,
                "f_n": f_n, "f_v": f_v, "name_w": name_w}

    def _draw(self, img, lay, rows, now):
        d = ImageDraw.Draw(img)
        for i, (name, v, unit) in enumerate(rows):
            x = (i % lay["cols"]) * lay["cw"]
            y = (i // lay["cols"]) * lay["ch"]
            cy = y + lay["ch"] // 2
            row_text(img, (x + 4, cy), str(name or "?"), lay["f_n"],
                     "#c0c0c0", lay["name_w"], now, scroll=True)
            d.text((x + lay["cw"] - 4, cy), self._value_txt(v, unit),
                   fill="#00e0ff" if unit == "cpu" else "#00ff80",
                   font=lay["f_v"], anchor="rm")

    @staticmethod
    def _value_txt(v, unit):
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        if v != v or v < 0:
            v = 0.0
        v = min(v, 100.0)
        if unit == "cpu":
            return f"{int(v)}%"
        return f"{v:.0f}%"

    @staticmethod
    def _clamp_int(value, default, lo, hi):
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))