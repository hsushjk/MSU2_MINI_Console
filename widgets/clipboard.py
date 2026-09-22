import shutil
import subprocess
import sys
import threading
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

def _get_windows():
    try:
        import ctypes
        from ctypes import wintypes
        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

        if not user32.OpenClipboard(None):
            return None
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        return None

_LINUX_TOOLS = [
    ("wl-paste", ["wl-paste", "--no-newline"]),
    ("xclip",    ["xclip", "-selection", "clipboard", "-o"]),
    ("xsel",     ["xsel", "--clipboard", "--output"]),
]

def _get_linux():
    for name, cmd in _LINUX_TOOLS:
        if not shutil.which(name):
            continue
        try:
            out = subprocess.check_output(
                cmd, timeout=1, stderr=subprocess.DEVNULL)
            return out.decode("utf-8", "ignore")
        except subprocess.CalledProcessError:
            return ""
        except Exception:
            continue
    return None

def _get_mac():
    try:
        out = subprocess.check_output(["pbpaste"], timeout=1,
                                      stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "ignore")
    except Exception:
        return None

def _read_clipboard():
    try:
        if sys.platform == "win32":
            return _get_windows()
        if sys.platform == "darwin":
            return _get_mac()
        return _get_linux()
    except Exception:
        return None

def _linux_hint():
    if not sys.platform.startswith("linux"):
        return None
    have = [n for n, _ in _LINUX_TOOLS if shutil.which(n)]
    if have:
        return None
    return "apt install xclip  或  wl-clipboard"

@register
class ClipboardWidget(Widget):
    name = "clipboard"
    interval = 0.3
    orientations = BOTH
    priority = 60

    config_schema = [
        Field("max_chars", "int", "最大字符数", default=500, min=50, max=2000),
        Field("poll_sec", "float", "轮询间隔(秒)", default=0.5,
              min=0.2, max=5.0, step=0.1),
    ]

    def setup(self, ctx):
        st = {
            "text": "",
            "available": True,
            "t_last": 0.0,
            "scroll": 0.0,
            "pinned": False,
            "lock": threading.Lock(),
            "stop": False,
        }
        ctx.state["_cb"] = st
        self._start_poller(ctx)

    def _start_poller(self, ctx):
        st = ctx.state["_cb"]
        st["stop"] = False

        def loop():
            while not st.get("stop"):
                cfg = self.get_config(ctx)
                txt = _read_clipboard()
                with st["lock"]:
                    if txt is None:
                        st["available"] = False
                    else:
                        st["available"] = True
                        txt = txt[:int(cfg["max_chars"])]
                        if not st.get("pinned") and txt != st["text"]:
                            st["text"] = txt
                            st["scroll"] = 0.0
                time.sleep(float(cfg["poll_sec"]))

        threading.Thread(target=loop, daemon=True,
                         name="ClipboardPoller").start()

    def teardown(self, ctx):
        st = ctx.state.get("_cb")
        if st:
            st["stop"] = True

    def on_enter(self, ctx):
        st = ctx.state.get("_cb")
        if st:
            st["scroll"] = 0.0

    def on_action(self, action, payload, ctx):
        st = ctx.state.get("_cb")
        if st is None:
            return {"ok": False, "err": "未初始化"}
        if action in ("touch_long", "toggle_pin"):
            with st["lock"]:
                st["pinned"] = not st.get("pinned", False)
            return {"ok": True}
        return {"ok": False, "err": f"未知动作: {action}"}

    def render(self, ctx):
        st = ctx.state.get("_cb") or {}
        lock = st.get("lock")
        if lock:
            with lock:
                text = st.get("text", "")
                available = st.get("available", True)
                pinned = st.get("pinned", False)
        else:
            text, available, pinned = "", True, False

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if not available:
            d.text((W // 2, H // 2 - 10), "无法读取剪贴板",
                   fill="#c05050", font=get_font(12, cjk=True), anchor="mm")
            hint = _linux_hint()
            if hint:
                d.text((W // 2, H // 2 + 8), hint,
                       fill="#707070", font=get_font(9, cjk=False),
                       anchor="mm")
            return img

        if not text:
            d.text((W // 2, H // 2), "(空)",
                   fill="#505050", font=get_font(14, cjk=False), anchor="mm")
            if pinned:
                d.ellipse([W - 8, 3, W - 4, 7], fill="#ff5555")
            return img

        flat = " ".join(line.strip() for line in text.splitlines()
                        if line.strip())

        f = get_font(11, cjk=True) if ctx.orientation == LANDSCAPE \
            else get_font(12, cjk=True)
        line_h = 14
        max_w = W - 8
        lines = self._wrap(flat, f, max_w)
        total_h = len(lines) * line_h

        if total_h <= H - 4:
            y = (H - total_h) // 2
            for line in lines:
                d.text((W // 2, y + line_h // 2), line,
                       fill="#e0e0e0", font=f, anchor="mm")
                y += line_h
        else:
            max_scroll = total_h - (H - 4)
            if lock:
                with lock:
                    st["scroll"] = (st.get("scroll", 0.0) + 1) % (max_scroll + 20)
                    off = int(st["scroll"])
            else:
                off = 0
            y = 2 - off
            for line in lines:
                if -line_h < y < H:
                    d.text((W // 2, y + line_h // 2), line,
                           fill="#e0e0e0", font=f, anchor="mm")
                y += line_h

        if pinned:
            d.ellipse([W - 8, 3, W - 4, 7], fill="#ff5555")

        return img

    def _wrap(self, text, font, max_w):
        out = []
        cur = ""
        for ch in text:
            try:
                w = font.getlength(cur + ch)
            except Exception:
                w = len(cur + ch) * 7
            if w <= max_w:
                cur += ch
            else:
                if cur:
                    out.append(cur)
                cur = ch
        if cur:
            out.append(cur)
        return out or [""]
