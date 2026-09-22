import re
import subprocess
import sys
import time
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

def _fmt_long(kbs):

    if kbs < 1:
        return "0 B/s"
    if kbs < 1024:
        return f"{int(kbs * 1024)} B/s"
    if kbs < 1024 * 1024:
        return f"{kbs / 1024:.1f} MB/s"
    return f"{kbs / 1024 / 1024:.1f} GB/s"

def _fmt_short(kbs):

    if kbs < 1:
        return "0"
    if kbs < 1024:
        return f"{int(kbs)}K"
    if kbs < 10240:
        return f"{kbs / 1024:.1f}M"
    if kbs < 1024 * 1024:
        return f"{int(kbs / 1024)}M"
    return f"{kbs / 1024 / 1024:.1f}G"

def _clip(text, font, max_w):

    if not text:
        return ""
    try:
        if font.getlength(text) <= max_w:
            return text
    except Exception:
        return text
    ell = "…"
    try:
        ew = font.getlength(ell)
    except Exception:
        ew = 6
    out = ""
    for ch in text:
        try:
            if font.getlength(out + ch) + ew > max_w:
                break
        except Exception:
            break
        out += ch
    return out + ell

def _wifi_signal(iface):

    if not iface:
        return None
    try:
        if sys.platform == "win32":
            cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                timeout=2, creationflags=cf,
            ).decode("utf-8", "ignore")
            for line in out.splitlines():
                if "信号" in line or "Signal" in line:
                    m = re.search(r"(\d+)\s*%", line)
                    if m:
                        return int(m.group(1))
        elif sys.platform.startswith("linux"):
            with open("/proc/net/wireless", "r") as f:
                for line in f:
                    if iface in line:
                        parts = line.replace(":", " ").split()
                        if len(parts) >= 3:
                            quality = float(parts[2].rstrip("."))
                            return min(100, int(quality / 70 * 100))
    except Exception:
        pass
    return None

@register
class NetworkDetailWidget(Widget):
    name = "network_detail"
    interval = 1.0
    orientations = BOTH
    priority = 32

    config_schema = [
        Field("interface", "str", "网卡名",
              default="",
              help="留空=全部汇总。Windows 常见: 以太网/WLAN/Wi-Fi；"
                   "Linux 常见: eth0/wlan0/enp3s0"),
        Field("show_wifi", "bool", "显示 Wi-Fi 信号强度", default=True),
    ]

    def setup(self, ctx):
        ctx.state["_net"] = {
            "last": None,
            "t": time.time(),
            "down": 0.0,
            "up": 0.0,
        }

    def render(self, ctx):
        cfg = self.get_config(ctx)
        iface = (cfg.get("interface") or "").strip()
        show_wifi = bool(cfg.get("show_wifi", True))

        st = ctx.state.setdefault("_net", {
            "last": None, "t": time.time(), "down": 0.0, "up": 0.0,
        })

        now = time.time()
        try:
            counters = psutil.net_io_counters(pernic=True)
        except Exception:
            counters = {}

        if iface:
            io = counters.get(iface)
        else:
            total_recv = sum(c.bytes_recv for n, c in counters.items()
                             if not n.startswith("lo"))
            total_sent = sum(c.bytes_sent for n, c in counters.items()
                             if not n.startswith("lo"))
            class _Fake:
                pass
            io = _Fake()
            io.bytes_recv = total_recv
            io.bytes_sent = total_sent

        if io and st["last"]:
            dt = max(0.001, now - st["t"])
            st["down"] = max(0.0, (io.bytes_recv - st["last"].bytes_recv)
                             / dt / 1024)
            st["up"] = max(0.0, (io.bytes_sent - st["last"].bytes_sent)
                           / dt / 1024)
        st["last"] = io
        st["t"] = now

        if iface and iface not in counters:
            return self._draw_error(ctx, iface)

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        title = iface if iface else "ALL"
        wifi_pct = _wifi_signal(iface) if (iface and show_wifi) else None

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(d, W, H, title, st["down"], st["up"], wifi_pct)
        else:
            self._draw_portrait(d, W, H, title, st["down"], st["up"], wifi_pct)
        return img

    def _draw_landscape(self, d, W, H, title, down, up, wifi_pct):
        f_title = get_font(11, cjk=True)
        f_lbl = get_font(9, cjk=False)
        f_val = get_font(18, bold=True, cjk=False)

        title = _clip(title, f_title, W - 8)
        d.text((4, 3), title, fill="#909090", font=f_title)

        d.text((4, 24), "UP", fill="#909090", font=f_lbl, anchor="lt")
        d.text((W // 2 - 6, 22), _fmt_short(up),
               fill="#00e0ff", font=f_val, anchor="ra")

        d.text((4, 52), "DN", fill="#909090", font=f_lbl, anchor="lt")
        d.text((W // 2 - 6, 50), _fmt_short(down),
               fill="#22cc66", font=f_val, anchor="ra")

        if wifi_pct is not None:
            self._draw_wifi(d, W // 2 + 4, 20, W // 2 - 8, H - 40, wifi_pct)

    def _draw_portrait(self, d, W, H, title, down, up, wifi_pct):
        f_title = get_font(12, cjk=True)
        f_lbl = get_font(11, cjk=False)
        f_val = get_font(14, bold=True, cjk=False)

        title = _clip(title, f_title, W - 8)
        d.text((W // 2, 14), title, fill="#909090",
               font=f_title, anchor="mm")

        d.text((6, 40), "UP", fill="#909090", font=f_lbl, anchor="lt")
        d.text((W - 6, 38), _fmt_short(up),
               fill="#00e0ff", font=f_val, anchor="rt")

        d.text((6, 70), "DN", fill="#909090", font=f_lbl, anchor="lt")
        d.text((W - 6, 68), _fmt_short(down),
               fill="#22cc66", font=f_val, anchor="rt")

        if wifi_pct is not None:
            self._draw_wifi(d, 8, 104, W - 16, 46, wifi_pct)

    def _draw_wifi(self, d, x, y, w, h, pct):

        f = get_font(10, cjk=False)
        d.text((x, y), f"WiFi {pct}%", fill="#a0a0a0", font=f)

        bar_y = y + 16
        bar_h = h - 20
        n_bars = 4
        lit = int(pct / 100 * n_bars + 0.999)
        gap = 2
        bw = max(2, (w - (n_bars - 1) * gap) // n_bars)
        for i in range(n_bars):
            bh = int(bar_h * (i + 1) / n_bars)
            bx = x + i * (bw + gap)
            color = "#4a7fe0" if i < lit else "#2a2a30"
            d.rectangle([bx, bar_y + bar_h - bh, bx + bw, bar_y + bar_h],
                        fill=color)

    def _draw_error(self, ctx, iface):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f = get_font(12, cjk=True)
        f_s = get_font(10, cjk=False)
        d.text((W // 2, H // 2 - 12), "网卡不存在",
               fill="#ff6666", font=f, anchor="mm")
        d.text((W // 2, H // 2 + 8), _clip(iface, f_s, W - 8),
               fill="#909090", font=f_s, anchor="mm")
        return img
