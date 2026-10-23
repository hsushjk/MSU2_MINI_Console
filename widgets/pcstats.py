import os
import sys
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, get_font

def _tw(font, text):
    try:
        return int(font.getlength(text))
    except Exception:
        return len(text) * 8

def _disk_percent():
    try:
        path = (os.environ.get("SystemDrive", "C:").rstrip("\\") + "\\"
                if sys.platform == "win32" else "/")
        return max(0.0, min(100.0, float(psutil.disk_usage(path).percent)))
    except Exception:
        return 0.0

def _cpu_percent():
    try:
        return max(0.0, min(100.0, float(psutil.cpu_percent(interval=None))))
    except Exception:
        return 0.0

def _ram_percent():
    try:
        return max(0.0, min(100.0, float(psutil.virtual_memory().percent)))
    except Exception:
        return 0.0

def _uptime(now):
    try:
        return max(0.0, now - float(psutil.boot_time()))
    except Exception:
        return 0.0

def _net_speed(state, now):

    try:
        io = psutil.net_io_counters()
    except Exception:
        io = None
    if io is None:
        state.pop("_pcstats_net", None)
        return 0.0
    try:
        recv = float(io.bytes_recv)
    except (AttributeError, TypeError, ValueError):
        state.pop("_pcstats_net", None)
        return 0.0
    last = state.get("_pcstats_net")
    if not last:
        state["_pcstats_net"] = {"t": now, "recv": recv}
        return 0.0
    try:
        dt = now - float(last["t"])
    except (KeyError, TypeError, ValueError):
        state["_pcstats_net"] = {"t": now, "recv": recv}
        return 0.0
    if dt < 0.2:
        return 0.0
    try:
        down = (recv - float(last["recv"])) / dt / 1024
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        down = 0.0
    state["_pcstats_net"] = {"t": now, "recv": recv}
    return max(down, 0.0)

def _fmt_speed(kbs):
    try:
        kbs = max(0.0, float(kbs))
    except (TypeError, ValueError):
        return "0"
    if kbs < 1: return "0"
    if kbs < 1000: return f"{int(kbs)}K"
    if kbs < 9999: return f"{kbs/1024:.1f}M"
    return f"{int(kbs/1024)}M"

def _fmt_uptime(s):
    try:
        s = max(0.0, float(s))
    except (TypeError, ValueError):
        return "0m"
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
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    def on_enter(self, ctx):
        ctx.state.pop("_pcstats_net", None)
        ctx.state.pop("_pcs_key", None)

    def render(self, ctx):
        W, H = ctx.size
        now = ctx.now

        cpu = _cpu_percent()
        ram = _ram_percent()
        dsk = _disk_percent()
        net = _net_speed(ctx.state, now)
        up = _uptime(now)

        net_txt = _fmt_speed(net)
        up_txt = _fmt_uptime(up)
        key = (ctx.orientation, int(cpu), int(ram), int(dsk), net_txt, up_txt)
        if ctx.state.get("_pcs_key") == key:
            return None

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if ctx.orientation == LANDSCAPE:
            cw = max(1, W // 3)
            rh = max(1, H // 2)
            row1 = [
                (0,    0, cw, rh, "CPU", f"{int(cpu)}%", cpu/100, "#00e0ff"),
                (cw,   0, cw, rh, "RAM", f"{int(ram)}%", ram/100, "#00ff80"),
                (2*cw, 0, max(1, W - 2*cw), rh,
                 "DSK", f"{int(dsk)}%", dsk/100, "#ff9900"),
            ]
            for x, y, w, h, *item in row1:
                self._draw_cell(d, x, y, w, h, *item, compact=True)
            hw = max(1, W // 2)
            bh = max(1, H - rh)
            self._draw_cell(d, 0, rh, hw, bh,
                            "NET", net_txt,
                            min(net/1024, 1.0), "#8888ff", compact=True)
            self._draw_cell(d, hw, rh, max(1, W - hw), bh,
                            "UP", up_txt,
                            0.0, "#b0b0b0", compact=True)
        else:
            ch = max(1, H // 5)
            data = [
                ("CPU", f"{int(cpu)}%", cpu/100, "#00e0ff"),
                ("RAM", f"{int(ram)}%", ram/100, "#00ff80"),
                ("DSK", f"{int(dsk)}%", dsk/100, "#ff9900"),
                ("NET", net_txt, min(net/1024, 1.0), "#8888ff"),
                ("UP",  up_txt, 0.0, "#b0b0b0"),
            ]
            for i, item in enumerate(data):
                hh = ch if i < len(data) - 1 else max(1, H - i * ch)
                self._draw_cell(d, 0, i * ch, W, hh, *item, compact=False)

        ctx.state["_pcs_key"] = key
        return img

    def _draw_cell(self, d, x, y, w, h, label, value, ratio, color, compact):
        if compact:
            f_lbl = get_font(9, cjk=False)
            base = 13
            pad = 3
            bar_h = 2
        else:
            f_lbl = get_font(10, cjk=False)
            base = 15
            pad = 5
            bar_h = 3

        lw = _tw(f_lbl, label)
        f_val = self._fit_val(value, max(8, w - pad * 2 - lw - 4), base)

        d.text((x + pad, y + 2), label, fill="#909090", font=f_lbl)
        d.text((x + w - pad, y + 2), value, fill=color, font=f_val, anchor="ra")

        if ratio > 0:
            bx = x + pad
            bw = max(1, w - pad * 2)
            by = y + h - bar_h - 2
            d.rectangle([bx, by, bx + bw - 1, by + bar_h - 1], fill="#202020")
            fw = int(bw * max(0.0, min(1.0, ratio)))
            if fw > 0:
                d.rectangle([bx, by, bx + fw - 1, by + bar_h - 1], fill=color)

    @staticmethod
    def _fit_val(text, max_w, max_size):
        for size in range(max_size, 7, -2):
            f = get_font(size, bold=True, cjk=False)
            if _tw(f, text) <= max_w:
                return f
        return get_font(8, bold=True, cjk=False)