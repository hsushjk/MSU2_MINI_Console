import logging
import shutil
import subprocess
import sys
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.app_usage")

def _fg_windows():
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil
        return psutil.Process(pid.value).name()
    except Exception as e:
        log.debug("win fg: %s", e)
        return None

def _fg_linux():
    try:
        if shutil.which("xdotool"):
            out = subprocess.check_output(
                ["xdotool", "getactivewindow", "getwindowpid"],
                timeout=2, stderr=subprocess.DEVNULL).decode().strip()
            pid = int(out)
            import psutil
            return psutil.Process(pid).name()
        if shutil.which("xprop"):
            win = subprocess.check_output(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                timeout=2, stderr=subprocess.DEVNULL).decode()
            wid = win.split()[-1]
            if wid and wid != "0x0":
                info = subprocess.check_output(
                    ["xprop", "-id", wid, "_NET_WM_PID"],
                    timeout=2, stderr=subprocess.DEVNULL).decode()
                pid = int(info.split()[-1])
                import psutil
                return psutil.Process(pid).name()
    except Exception as e:
        log.debug("linux fg: %s", e)
    return None

def _fg_mac():
    try:
        out = subprocess.check_output(
            ["osascript", "-e",
             'tell application "System Events" to get name of first '
             'application process whose frontmost is true'],
            timeout=2, stderr=subprocess.DEVNULL).decode().strip()
        return out or None
    except Exception as e:
        log.debug("mac fg: %s", e)
        return None

def _fg_app():
    try:
        if sys.platform == "win32":
            return _fg_windows()
        if sys.platform == "darwin":
            return _fg_mac()
        return _fg_linux()
    except Exception:
        return None

def _fmt_dur(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"

@register
class AppUsageWidget(Widget):
    name = "app_usage"
    interval = 1.0
    orientations = BOTH
    priority = 62

    config_schema = [
        Field("poll_sec", "float", "轮询间隔(秒)", default=1.0,
              min=0.5, max=5.0, step=0.5),
        Field("strip_ext", "bool", "去掉 .exe 后缀", default=True),
    ]

    def setup(self, ctx):
        ctx.state["_app"] = {
            "cur": "",
            "start": time.time(),
            "today": {},
            "day": time.localtime().tm_yday,
            "t_poll": 0.0,
        }

    def render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state.setdefault("_app", {
            "cur": "", "start": time.time(),
            "today": {}, "day": time.localtime().tm_yday, "t_poll": 0.0})

        now = time.time()
        lt = time.localtime(now)

        if lt.tm_yday != st["day"]:
            st["today"] = {}
            st["day"] = lt.tm_yday

        if now - st["t_poll"] >= float(cfg["poll_sec"]):
            st["t_poll"] = now
            app = _fg_app() or ""
            if app != st["cur"]:

                if st["cur"]:
                    elapsed = now - st["start"]
                    st["today"][st["cur"]] = st["today"].get(st["cur"], 0.0) + elapsed
                st["cur"] = app
                st["start"] = now

        cur = st["cur"] or "(无)"
        if cfg.get("strip_ext") and cur.lower().endswith(".exe"):
            cur = cur[:-4]

        session = now - st["start"]
        today = st["today"].get(st["cur"], 0.0) + session

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(d, W, H, cur, session, today)
        else:
            self._draw_portrait(d, W, H, cur, session, today)
        return img

    def _draw_landscape(self, d, W, H, cur, session, today):
        f_title = get_font(10, cjk=True)
        f_app = get_font(18, bold=True, cjk=True)
        f_lbl = get_font(9, cjk=True)
        f_val = get_font(12, bold=True, cjk=False)

        d.text((W // 2, 6), "当前应用",
               fill="#808080", font=f_title, anchor="mm")
        app = self._fit(cur, f_app, W - 8)
        d.text((W // 2, 24), app, fill="#ffffff", font=f_app, anchor="mm")

        sep_y = H - 26
        d.line([(6, sep_y), (W - 6, sep_y)], fill="#303030")
        d.text((6, sep_y + 3), "本次", fill="#808080", font=f_lbl)
        d.text((W // 2 - 4, sep_y + 2), _fmt_dur(session),
               fill="#00e0ff", font=f_val, anchor="ra")
        d.text((W // 2 + 4, sep_y + 3), "今日", fill="#808080", font=f_lbl)
        d.text((W - 6, sep_y + 2), _fmt_dur(today),
               fill="#22cc66", font=f_val, anchor="ra")

    def _draw_portrait(self, d, W, H, cur, session, today):
        f_title = get_font(10, cjk=True)
        f_app = get_font(14, bold=True, cjk=True)
        f_lbl = get_font(10, cjk=True)
        f_val = get_font(14, bold=True, cjk=False)

        d.text((W // 2, 12), "当前应用",
               fill="#808080", font=f_title, anchor="mm")

        lines = self._wrap(cur, f_app, W - 8, max_lines=3)
        y = 28
        for line in lines:
            d.text((W // 2, y), line, fill="#ffffff",
                   font=f_app, anchor="mm")
            y += 18

        y = max(y + 8, H - 60)
        d.text((4, y), "本次", fill="#808080", font=f_lbl, anchor="lt")
        d.text((W - 4, y), _fmt_dur(session),
               fill="#00e0ff", font=f_val, anchor="rt")

        d.text((4, y + 26), "今日", fill="#808080", font=f_lbl, anchor="lt")
        d.text((W - 4, y + 26), _fmt_dur(today),
               fill="#22cc66", font=f_val, anchor="rt")

    @staticmethod
    def _fit(text, font, max_w):
        if not text:
            return "(无)"
        try:
            if font.getlength(text) <= max_w:
                return text
            while text and font.getlength(text + "…") > max_w:
                text = text[:-1]
            return text + "…"
        except Exception:
            return text[:10]

    @staticmethod
    def _wrap(text, font, max_w, max_lines=3):
        if not text:
            return ["(无)"]
        lines, cur = [], ""
        for ch in text:
            try:
                if font.getlength(cur + ch) <= max_w:
                    cur += ch
                else:
                    lines.append(cur); cur = ch
                    if len(lines) >= max_lines: break
            except Exception:
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        return lines
