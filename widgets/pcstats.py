import os
import sys
import time
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, get_font

def _disk_percent():
    try:
        path = (os.environ.get("SystemDrive", "C:").rstrip("\\") + "\\"
                if sys.platform == "win32" else "/")
        return psutil.disk_usage(path).percent
    except Exception:
        return 0.0

def _net_speed(state):

    now = time.time()
    try:
        io = psutil.net_io_counters()
    except Exception:
        return 0.0
    last = state.get("_pcstats_net")
    if not last:
        state["_pcstats_net"] = {"t": now, "recv": io.bytes_recv}
        return 0.0
    dt = now - last["t"]
    if dt < 0.2:
        return 0.0
    try:
        down = (io.bytes_recv - last["recv"]) / dt / 1024
    except (KeyError, TypeError):
        down = 0.0
    state["_pcstats_net"] = {"t": now, "recv": io.bytes_recv}
    return max(down, 0.0)

def _fmt_speed(kbs):
    if kbs < 1: return "0"
    if kbs < 1000: return f"{int(kbs)}K"
    if kbs < 9999: return f"{kbs/1024:.1f}M"
    return f"{int(kbs/1024)}M"

def _fmt_uptime(s):
    d = int(s // 86400); h = int((s % 86400) // 3600); m = int((s % 3600) // 60)
    if d > 0: return f"{d}d {h}h"
    if h > 0: return f"{h}h {m}m"
    return f"{m}m"

@register
class PCStatsWidget(Widget):
    name = "pcstats"
    interval = 1.0
    orientations = BOTH
    priority = 30

    def setup(self, ctx):
        psutil.cpu_percent(interval=None)

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        dsk = _disk_percent()
        net = _net_speed(ctx.state)
        up = time.time() - psutil.boot_time()

        if ctx.orientation == LANDSCAPE:
            row1 = [
                (0,   0, 53, 40, "CPU", f"{int(cpu)}%", cpu/100, "#00e0ff"),
                (53,  0, 53, 40, "RAM", f"{int(ram)}%", ram/100, "#00ff80"),
                (106, 0, 54, 40, "DSK", f"{int(dsk)}%", dsk/100, "#ff9900"),
            ]
            for x, y, w, h, *item in row1:
                self._draw_cell(d, x, y, w, h, *item, compact=True)
            self._draw_cell(d, 0, 40, 80, 40,
                            "NET", _fmt_speed(net),
                            min(net/1024, 1.0), "#8888ff", compact=True)
            self._draw_cell(d, 80, 40, 80, 40,
                            "UP", _fmt_uptime(up),
                            0.0, "#b0b0b0", compact=True)
        else:
            ch = H // 5
            data = [
                ("CPU", f"{int(cpu)}%", cpu/100, "#00e0ff"),
                ("RAM", f"{int(ram)}%", ram/100, "#00ff80"),
                ("DSK", f"{int(dsk)}%", dsk/100, "#ff9900"),
                ("NET", _fmt_speed(net), min(net/1024, 1.0), "#8888ff"),
                ("UP",  _fmt_uptime(up), 0.0, "#b0b0b0"),
            ]
            for i, item in enumerate(data):
                self._draw_cell(d, 0, i * ch, W, ch, *item, compact=False)
        return img

    def _draw_cell(self, d, x, y, w, h, label, value, ratio, color, compact):
        if compact:
            f_lbl = get_font(9, cjk=False)
            f_val = get_font(13, bold=True, cjk=False)
            pad = 3
            bar_h = 2
        else:
            f_lbl = get_font(10, cjk=False)
            f_val = get_font(15, bold=True, cjk=False)
            pad = 5
            bar_h = 3

        d.text((x + pad, y + 2), label, fill="#909090", font=f_lbl)
        d.text((x + w - pad, y + 2), value, fill=color, font=f_val, anchor="ra")

        if ratio > 0:
            bx = x + pad
            bw = w - pad * 2
            by = y + h - bar_h - 2
            d.rectangle([bx, by, bx + bw, by + bar_h], fill="#202020")
            fw = int(bw * max(0.0, min(1.0, ratio)))
            if fw > 0:
                d.rectangle([bx, by, bx + fw, by + bar_h], fill=color)
