import locale
import re
import subprocess
import sys
import threading
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

def _decode_out(raw):

    if not raw:
        return ""
    encs = []
    try:
        encs.append(locale.getpreferredencoding(False))
    except Exception:
        pass
    encs.extend(["cp936", "utf-8", "latin-1"])
    for enc in encs:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")

def _probe_wifi():

    out = {}
    try:
        if sys.platform == "win32":
            cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            raw = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                timeout=2, creationflags=cf, stderr=subprocess.DEVNULL,
            )
            name = ""
            for line in _decode_out(raw).splitlines():
                m = re.search(r"(?:名称|Name)\s*:\s*(.+)$", line)
                if m:
                    name = m.group(1).strip()
                m = re.search(r"(?:信号|Signal)\s*:\s*(\d+)\s*%", line)
                if m:
                    pct = max(0, min(100, int(m.group(1))))
                    out[name or "wlan"] = pct
        elif sys.platform.startswith("linux"):
            with open("/proc/net/wireless", "r") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    parts = line.replace(":", " ").split()
                    if len(parts) < 3:
                        continue
                    try:
                        quality = float(parts[2].rstrip("."))
                    except (TypeError, ValueError):
                        continue
                    out[parts[0]] = max(0, min(100, int(quality / 70 * 100)))
    except Exception:
        pass
    return out

class _WifiSource:
    def __init__(self, interval=3.0):
        self._interval = max(1.0, float(interval))
        self._lock = threading.Lock()
        self._data = {}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="WifiSource")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def get(self):
        with self._lock:
            return dict(self._data)

    def _loop(self):
        while not self._stop.is_set():
            try:
                data = _probe_wifi()
            except Exception:
                data = {}
            with self._lock:
                self._data = data
            self._stop.wait(self._interval)

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
        old = ctx.state.get("_net")
        if isinstance(old, dict) and old.get("wifi") is not None:
            try:
                old["wifi"].stop()
            except Exception:
                pass
        src = _WifiSource(3.0)
        src.start()
        ctx.state["_net"] = {
            "last": None,
            "t": time.time(),
            "down": 0.0,
            "up": 0.0,
            "wifi": src,
            "last_key": None,
        }

    def teardown(self, ctx):
        st = ctx.state.get("_net")
        if isinstance(st, dict) and st.get("wifi") is not None:
            try:
                st["wifi"].stop()
            except Exception:
                pass
            st["wifi"] = None

    def on_enter(self, ctx):
        st = ctx.state.get("_net")
        if isinstance(st, dict):
            st["last_key"] = None

    def render(self, ctx):
        cfg = self.get_config(ctx)
        iface = str(cfg.get("interface") or "").strip()
        show_wifi = bool(cfg.get("show_wifi", True))

        st = ctx.state.get("_net")
        if not isinstance(st, dict):
            st = {"last": None, "t": time.time(), "down": 0.0, "up": 0.0,
                  "wifi": None, "last_key": None}
            ctx.state["_net"] = st
        src = st.get("wifi")
        if src is None:
            try:
                src = _WifiSource(3.0)
                src.start()
                st["wifi"] = src
            except Exception:
                src = None

        now = time.time()
        try:
            counters = psutil.net_io_counters(pernic=True) or {}
        except Exception:
            counters = {}

        io = None
        if iface:
            io = counters.get(iface)
        else:
            total_recv = 0
            total_sent = 0
            for n, c in counters.items():
                if n.startswith("lo"):
                    continue
                total_recv += int(getattr(c, "bytes_recv", 0) or 0)
                total_sent += int(getattr(c, "bytes_sent", 0) or 0)
            class _Fake:
                pass
            io = _Fake()
            io.bytes_recv = total_recv
            io.bytes_sent = total_sent

        if io is not None and st.get("last") is not None:
            dt = max(0.001, now - float(st.get("t") or now))
            st["down"] = max(0.0, (io.bytes_recv - st["last"].bytes_recv)
                             / dt / 1024)
            st["up"] = max(0.0, (io.bytes_sent - st["last"].bytes_sent)
                           / dt / 1024)
        if io is not None:
            st["last"] = io
        st["t"] = now

        if iface and iface not in counters:
            key = (ctx.orientation, "err", iface)
            if st.get("last_key") == key:
                return None
            st["last_key"] = key
            return self._draw_error(ctx, iface)

        title = iface if iface else "ALL"
        wifi_pct = None
        if show_wifi and src is not None:
            try:
                data = src.get()
            except Exception:
                data = {}
            if iface:
                wifi_pct = data.get(iface)
            else:
                for pct in data.values():
                    wifi_pct = pct
                    break

        down = float(st.get("down") or 0.0)
        up = float(st.get("up") or 0.0)
        key = (ctx.orientation, title, _fmt_short(up), _fmt_short(down),
               ("off",) if not show_wifi else wifi_pct)
        if st.get("last_key") == key:
            return None
        st["last_key"] = key

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(d, W, H, title, down, up, wifi_pct, show_wifi)
        else:
            self._draw_portrait(d, W, H, title, down, up, wifi_pct, show_wifi)
        return img

    def _draw_landscape(self, d, W, H, title, down, up, wifi_pct, show_wifi):
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

        if show_wifi:
            self._draw_wifi(d, W // 2 + 4, 20, W // 2 - 8, H - 40, wifi_pct)

    def _draw_portrait(self, d, W, H, title, down, up, wifi_pct, show_wifi):
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

        if show_wifi:
            self._draw_wifi(d, 8, 104, W - 16, 46, wifi_pct)

    def _draw_wifi(self, d, x, y, w, h, pct):

        f = get_font(10, cjk=False)
        n_bars = 4
        if pct is None:
            d.text((x, y), "WiFi --", fill="#6a6a6a", font=f)
            lit = 0
        else:
            pct = max(0, min(100, int(pct)))
            d.text((x, y), f"WiFi {pct}%", fill="#a0a0a0", font=f)
            lit = int(pct / 100 * n_bars + 0.999)

        bar_y = y + 16
        bar_h = max(4, h - 20)
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