import collections
import time
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

@register
class DiskIOWidget(Widget):
    name = "disk_io"
    interval = 1.0
    orientations = BOTH
    priority = 35

    config_schema = [
        Field("history", "int", "折线保留点数", default=60, min=20, max=160),
    ]

    def setup(self, ctx):
        ctx.state["_disk"] = {
            "last": None, "t": time.time(),
            "r_hist": collections.deque(maxlen=80),
            "w_hist": collections.deque(maxlen=80),
        }

    def render(self, ctx):
        cfg = self.get_config(ctx)
        maxlen = int(cfg["history"])
        st = ctx.state.setdefault("_disk", {
            "last": None, "t": time.time(),
            "r_hist": collections.deque(maxlen=maxlen),
            "w_hist": collections.deque(maxlen=maxlen),
        })
        st["r_hist"] = collections.deque(st["r_hist"], maxlen=maxlen)
        st["w_hist"] = collections.deque(st["w_hist"], maxlen=maxlen)

        try:
            io = psutil.disk_io_counters()
        except Exception:
            io = None

        now = time.time()
        r_spd = w_spd = 0.0
        if io and st["last"]:
            dt = max(0.001, now - st["t"])
            r_spd = max(0.0, (io.read_bytes - st["last"].read_bytes) / dt / 1024)
            w_spd = max(0.0, (io.write_bytes - st["last"].write_bytes) / dt / 1024)
        if io:
            st["last"] = io
            st["t"] = now
        st["r_hist"].append(r_spd)
        st["w_hist"].append(w_spd)

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        self._draw_bar(d, 0, 0, W, 14, "R", r_spd, "#22cc66")
        self._draw_bar(d, 0, 14, W, 14, "W", w_spd, "#ff7a3d")

        chart_y0 = 30
        chart_h = H - chart_y0 - 2
        self._draw_chart(d, 0, chart_y0, W, chart_h, st)

        return img

    def _draw_bar(self, d, x, y, w, h, label, kbs, color):
        f_lbl = get_font(10, cjk=False)
        f_val = get_font(12, bold=True, cjk=False)
        d.text((x + 4, y + h // 2), label, fill="#909090",
               font=f_lbl, anchor="lm")
        txt = self._fmt(kbs)
        d.text((x + w - 4, y + h // 2), txt, fill=color,
               font=f_val, anchor="rm")

    def _draw_chart(self, d, x, y, w, h, st):

        d.rectangle([x, y, x + w, y + h], fill="#0e0e12")
        r_hist = list(st["r_hist"])
        w_hist = list(st["w_hist"])
        if not r_hist or not w_hist:
            return
        peak = max(max(r_hist), max(w_hist), 1.0)

        def polyline(hist, color):
            n = len(hist)
            if n < 2:
                return
            pts = []
            step = w / max(1, n - 1)
            for i, v in enumerate(hist):
                px = x + i * step
                py = y + h - (v / peak) * (h - 2)
                pts.append((px, py))
            d.line(pts, fill=color, width=1)

        polyline(r_hist, "#22cc66")
        polyline(w_hist, "#ff7a3d")

        f_peak = get_font(9, cjk=False)
        d.text((x + 3, y + 1), f"peak {self._fmt(peak)}",
               fill="#606060", font=f_peak)

    @staticmethod
    def _fmt(kbs):
        if kbs < 1: return "0 KB/s"
        if kbs < 1024: return f"{int(kbs)} KB/s"
        if kbs < 1024 * 1024: return f"{kbs/1024:.1f} MB/s"
        return f"{kbs/1024/1024:.1f} GB/s"
