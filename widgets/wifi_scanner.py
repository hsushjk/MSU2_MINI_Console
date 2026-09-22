import logging
import re
import shutil
import subprocess
import sys
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.wifi_scanner")

def _scan_windows():
    cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        out = subprocess.check_output(
            ["netsh", "wlan", "show", "networks", "mode=bssid"],
            timeout=8, creationflags=cf,
        ).decode("utf-8", "ignore")
    except Exception as e:
        log.debug("netsh 调用失败: %s", e)
        return []

    results = []
    cur_ssid = None
    for line in out.splitlines():
        line = line.strip()
        m = re.match(r"SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            cur_ssid = m.group(1).strip()
            continue
        m = re.match(r"(?:信号|Signal)\s*:\s*(\d+)\s*%", line)
        if m and cur_ssid is not None:
            results.append((cur_ssid or "(隐藏)", int(m.group(1))))
            cur_ssid = None
    best = {}
    for ssid, sig in results:
        if ssid not in best or sig > best[ssid]:
            best[ssid] = sig
    return sorted(best.items(), key=lambda x: -x[1])

def _scan_linux():
    if not shutil.which("nmcli"):
        return []
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"],
            timeout=8, stderr=subprocess.DEVNULL,
        ).decode("utf-8", "ignore")
    except Exception as e:
        log.debug("nmcli 调用失败: %s", e)
        return []

    best = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        ssid, _, sig = line.rpartition(":")
        ssid = ssid.replace("\\:", ":").strip() or "(隐藏)"
        try:
            s = int(sig)
        except ValueError:
            continue
        if ssid not in best or s > best[ssid]:
            best[ssid] = s
    return sorted(best.items(), key=lambda x: -x[1])

def _scan_mac():
    if not shutil.which("airport"):
        return []
    try:
        out = subprocess.check_output(
            ["airport", "-s"], timeout=8, stderr=subprocess.DEVNULL,
        ).decode("utf-8", "ignore")
    except Exception as e:
        log.debug("airport 调用失败: %s", e)
        return []

    best = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        ssid = parts[0]
        try:
            sig = int(parts[2])
        except ValueError:
            continue
        pct = max(0, min(100, 2 * (sig + 100)))
        if ssid not in best or pct > best[ssid]:
            best[ssid] = pct
    return sorted(best.items(), key=lambda x: -x[1])

def _scan():
    try:
        if sys.platform == "win32":
            return _scan_windows()
        if sys.platform == "darwin":
            return _scan_mac()
        return _scan_linux()
    except Exception as e:
        log.debug("扫描异常: %s", e)
        return []

@register
class WifiScannerWidget(Widget):
    name = "wifi_scanner"
    interval = 1.0
    orientations = BOTH
    priority = 55

    config_schema = [
        Field("scan_sec", "float", "扫描间隔(秒)", default=15.0,
              min=5.0, max=120.0, step=5.0),
        Field("show_n", "int", "每屏条数", default=5, min=2, max=8),
        Field("cycle_sec", "float", "翻页周期(秒)", default=6.0,
              min=3.0, max=60.0, step=1.0),
    ]

    def setup(self, ctx):
        ctx.state["_wifi"] = {"items": [], "t": 0.0, "last_key": None}

    def on_enter(self, ctx):
        st = ctx.state.setdefault("_wifi", {})
        st["last_key"] = None

    def on_action(self, action, payload, ctx):
        if action in ("touch_single", "refresh"):
            st = ctx.state.setdefault("_wifi", {})
            st["t"] = 0.0
            st["last_key"] = None
            return {"ok": True}
        return {"ok": False, "err": f"未知动作: {action}"}

    def render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state.setdefault("_wifi", {"items": [], "t": 0.0, "last_key": None})

        now = time.time()
        if now - st["t"] >= float(cfg["scan_sec"]) or not st["items"]:
            st["items"] = _scan()
            st["t"] = now

        items = st["items"]
        W, H = ctx.size

        if not items:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2 - 8), "扫描中…",
                   fill="#808080", font=get_font(13, cjk=True), anchor="mm")
            d.text((W // 2, H // 2 + 8), "或未安装扫描工具",
                   fill="#505050", font=get_font(10, cjk=True), anchor="mm")
            return img

        show_n = int(cfg["show_n"])
        cycle = max(3.0, float(cfg["cycle_sec"]))
        total = len(items)
        if total > show_n:
            start = int(ctx.now / cycle) % total
        else:
            start = 0
        shown = [items[(start + i) % total] for i in range(min(show_n, total))]

        key = f"{start}:" + "|".join(f"{s}:{v}" for s, v in shown)
        if st.get("last_key") == key:
            return None
        st["last_key"] = key

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(d, W, H, shown)
        else:
            self._draw_portrait(d, W, H, shown)
        return img

    def _draw_landscape(self, d, W, H, rows):
        n = len(rows)
        row_h = H // n
        f_ssid = get_font(10, cjk=True)
        f_pct = get_font(10, bold=True, cjk=False)

        x_ssid = 4
        w_ssid = 60
        x_bar = x_ssid + w_ssid + 4
        w_bar = W - x_bar - 34
        x_pct = W - 4

        for i, (ssid, sig) in enumerate(rows):
            y = i * row_h
            cy = y + row_h // 2

            label = self._fit(ssid or "(隐藏)", f_ssid, w_ssid)
            d.text((x_ssid, cy), label, fill="#d0d0d0",
                   font=f_ssid, anchor="lm")

            bar_h = 4
            by = cy - bar_h // 2
            d.rectangle([x_bar, by, x_bar + w_bar, by + bar_h],
                        fill="#1c1c20")
            fw = int(w_bar * sig / 100)
            if fw > 0:
                color = self._sig_color(sig)
                d.rectangle([x_bar, by, x_bar + fw, by + bar_h], fill=color)

            d.text((x_pct, cy), f"{sig}%", fill="#00e0ff",
                   font=f_pct, anchor="rm")

    def _draw_portrait(self, d, W, H, rows):
        n = len(rows)
        row_h = H // n
        f_ssid = get_font(11, cjk=True)
        f_pct = get_font(11, bold=True, cjk=False)

        for i, (ssid, sig) in enumerate(rows):
            y = i * row_h

            label = self._fit(ssid or "(隐藏)", f_ssid, W - 40)
            d.text((4, y + row_h // 2 - 6), label,
                   fill="#d0d0d0", font=f_ssid, anchor="lm")
            d.text((W - 4, y + row_h // 2 - 6), f"{sig}%",
                   fill="#00e0ff", font=f_pct, anchor="rm")

            bar_h = 4
            bx = 4
            bw = W - 8
            by = y + row_h // 2 + 4
            d.rectangle([bx, by, bx + bw, by + bar_h], fill="#1c1c20")
            fw = int(bw * sig / 100)
            if fw > 0:
                color = self._sig_color(sig)
                d.rectangle([bx, by, bx + fw, by + bar_h], fill=color)

    @staticmethod
    def _sig_color(sig):
        if sig >= 75:
            return "#22cc66"
        if sig >= 50:
            return "#4a7fe0"
        if sig >= 25:
            return "#ff9944"
        return "#ff4444"

    @staticmethod
    def _fit(text, font, max_w):
        if not text:
            return ""
        try:
            if font.getlength(text) <= max_w:
                return text
            while text and font.getlength(text + "…") > max_w:
                text = text[:-1]
            return text + "…"
        except Exception:
            return text[:8]
