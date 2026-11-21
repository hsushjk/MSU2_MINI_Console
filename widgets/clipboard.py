import logging
import shutil
import subprocess
import sys
import threading
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import row_text, text_w

log = logging.getLogger("msu2.widgets.clipboard")


def _int_or(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _float_or(value, default, lo, hi):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _flatten(text):
    try:
        return " ".join(
            line.strip() for line in str(text or "").splitlines() if line.strip()
        )
    except Exception:
        return ""


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
    ("xclip", ["xclip", "-selection", "clipboard", "-o"]),
    ("xsel", ["xsel", "--clipboard", "--output"]),
]


def _get_linux():
    for name, cmd in _LINUX_TOOLS:
        if not shutil.which(name):
            continue
        try:
            out = subprocess.check_output(cmd, timeout=1, stderr=subprocess.DEVNULL)
            return out.decode("utf-8", "ignore")
        except subprocess.CalledProcessError as e:
            log.debug("%s 读取剪贴板失败: %s", name, e)
            continue
        except Exception as e:
            log.debug("%s 调用异常: %s", name, e)
            continue
    return None


def _get_mac():
    try:
        out = subprocess.check_output(
            ["pbpaste"], timeout=1, stderr=subprocess.DEVNULL
        )
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


def _write_clipboard(text):
    text = str(text or "")
    try:
        if sys.platform == "win32":
            return _set_windows(text)
        if sys.platform == "darwin":
            return _set_mac(text)
        return _set_linux(text)
    except Exception:
        return False


def _set_windows(text):
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        size = len(data)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        handle = kernel32.GlobalAlloc(0x0042, size)
        if not handle:
            return False
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(wintypes.HGLOBAL(handle))
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, size)
        finally:
            kernel32.GlobalUnlock(wintypes.HGLOBAL(handle))
        user32.SetClipboardData(CF_UNICODETEXT, wintypes.HANDLE(handle))
        return True
    finally:
        user32.CloseClipboard()


def _set_mac(text):
    p = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                       timeout=3, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    return p.returncode == 0


def _set_linux(text):
    tools = [
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ]
    for name, cmd in tools:
        if not shutil.which(name):
            continue
        try:
            p = subprocess.run(cmd, input=text.encode("utf-8"), timeout=3,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if p.returncode == 0:
                return True
        except Exception:
            continue
    return False


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

    actions = {
        "next": "下一个",
        "prev": "上一个",
        "pin": "固定/取消固定",
        "copy": "回写到剪贴板",
    }
    touch_help = "单击=下一个，双击=上一个，长按=固定/取消固定"

    config_schema = [
        Field("max_chars", "int", "最大字符数", default=500, min=50, max=2000),
        Field(
            "poll_sec",
            "float",
            "轮询间隔(秒)",
            default=0.5,
            min=0.2,
            max=5.0,
            step=0.1,
        ),
    ]

    def setup(self, ctx):
        old = ctx.state.get("_cb")
        if old:
            ev = old.get("stop_ev")
            if ev:
                ev.set()
            t = old.get("thread")
            if t and t.is_alive():
                t.join(timeout=1.0)
        st = {
            "text": "",
            "history": [],
            "index": 0,
            "available": True,
            "scroll": 0.0,
            "pinned": False,
            "pin_text": "",
            "pin_index": 0,
            "lock": threading.Lock(),
            "stop_ev": threading.Event(),
            "thread": None,
            "_last_key": None,
        }
        ctx.state["_cb"] = st
        self._start_poller(ctx)

    def _start_poller(self, ctx):
        st = ctx.state["_cb"]
        stop_ev = st["stop_ev"]

        def loop():
            while not stop_ev.is_set():
                cfg = self.get_config(ctx)
                txt = _read_clipboard()
                max_chars = _int_or(cfg.get("max_chars"), 500, 50, 2000)
                with st["lock"]:
                    if txt is None:
                        st["available"] = False
                    else:
                        st["available"] = True
                        txt = txt[:max_chars]
                        if txt != st.get("text"):
                            st["text"] = txt
                            history = list(st.get("history") or [])
                            history = [txt] + [old for old in history if old != txt]
                            st["history"] = history[:12]
                            if not st.get("pinned"):
                                st["index"] = 0
                                st["scroll"] = 0.0
                poll = _float_or(cfg.get("poll_sec"), 0.5, 0.2, 5.0)
                stop_ev.wait(poll)

        t = threading.Thread(target=loop, daemon=True, name="ClipboardPoller")
        st["thread"] = t
        t.start()

    def teardown(self, ctx):
        st = ctx.state.get("_cb")
        if not st:
            return
        ev = st.get("stop_ev")
        if ev:
            ev.set()
        t = st.get("thread")
        if t and t.is_alive():
            t.join(timeout=2.0)

    def on_enter(self, ctx):
        st = ctx.state.get("_cb")
        if st:
            st["scroll"] = 0.0
            st["_last_key"] = None

    def on_leave(self, ctx):
        pass

    def on_action(self, action, payload, ctx):
        try:
            st = ctx.state.get("_cb")
            if st is None:
                return {"ok": False, "err": "未初始化"}
            if action in ("pin", "toggle_pin", "touch_long"):
                if not self._do_pin(st):
                    return {"ok": False, "err": "没有可固定的内容"}
                st["_last_key"] = None
                return {"ok": True}
            if action in ("copy", "write", "to_clipboard"):
                with st["lock"]:
                    if st.get("pinned") and st.get("pin_text"):
                        text = st["pin_text"]
                    else:
                        history = list(st.get("history") or [])
                        index = int(st.get("index", 0) or 0)
                        if history:
                            if index < 0 or index >= len(history):
                                index = 0
                            text = history[index]
                        else:
                            text = st.get("text", "")
                if not text:
                    return {"ok": False, "err": "没有可写入的内容"}
                if not _write_clipboard(text):
                    return {"ok": False, "err": "写入剪贴板失败"}
                st["_last_key"] = None
                return {"ok": True, "msg": "已写回剪贴板"}
            if action in ("next", "touch_single"):
                if not self._do_step(st, 1):
                    return {"ok": False, "err": "没有历史内容"}
                st["_last_key"] = None
                return {"ok": True}
            if action in ("prev", "touch_double"):
                if not self._do_step(st, -1):
                    return {"ok": False, "err": "没有历史内容"}
                st["_last_key"] = None
                return {"ok": True}
            return {"ok": False, "err": "未知动作: " + str(action)}
        except Exception as exc:
            log.warning("clipboard 动作 %s 失败: %s", action, exc)
            return {"ok": False, "err": "动作失败"}

    def _do_pin(self, st):
        with st["lock"]:
            if st.get("pinned"):
                st["pinned"] = False
                st["pin_text"] = ""
                st["pin_index"] = 0
                st["index"] = 0
                return True
            history = list(st.get("history") or [])
            index = int(st.get("index", 0) or 0)
            if history:
                if index < 0 or index >= len(history):
                    index = 0
                display = history[index]
            else:
                display = st.get("text", "")
            if not display:
                return False
            st["pinned"] = True
            st["pin_text"] = display
            st["pin_index"] = index
            return True

    def _do_step(self, st, delta):
        with st["lock"]:
            history = list(st.get("history") or [])
            if not history:
                return False
            total = len(history)
            if st.get("pinned"):
                index = int(st.get("pin_index", 0) or 0)
                index = (index + delta) % total
                st["pin_index"] = index
                st["pin_text"] = history[index]
            else:
                index = int(st.get("index", 0) or 0)
                st["index"] = (index + delta) % total
            st["scroll"] = 0.0
            return True

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception as exc:
            log.warning("clipboard 渲染失败: %s", exc)
            return None

    def _render(self, ctx):
        st = ctx.state.get("_cb") or {}
        lock = st.get("lock")
        if lock:
            with lock:
                text = st.get("text", "")
                available = st.get("available", True)
                pinned = st.get("pinned", False)
                pin_text = st.get("pin_text", "")
                history = list(st.get("history") or [])
                index = int(st.get("index", 0) or 0)
        else:
            text, available, pinned = "", True, False
            pin_text, history, index = "", [], 0

        if pinned:
            current = pin_text or text
        elif history:
            if index < 0 or index >= len(history):
                index = 0
            current = history[index]
        else:
            current = text

        W, H = ctx.size

        if not available:
            key = "na|%sx%s" % (W, H)
            if st.get("_last_key") == key:
                return None
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text(
                (W // 2, H // 2 - 10),
                "无法读取剪贴板",
                fill="#c05050",
                font=get_font(12, cjk=True),
                anchor="mm",
            )
            hint = _linux_hint()
            if hint:
                d.text(
                    (W // 2, H // 2 + 8),
                    hint,
                    fill="#707070",
                    font=get_font(9, cjk=False),
                    anchor="mm",
                )
            st["_last_key"] = key
            return img

        flat = _flatten(current)

        if not flat:
            key = "empty|%sx%s|%s" % (W, H, pinned)
            if st.get("_last_key") == key:
                return None
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text(
                (W // 2, H // 2),
                "(空)",
                fill="#505050",
                font=get_font(14, cjk=False),
                anchor="mm",
            )
            if pinned:
                d.ellipse([W - 8, 3, W - 4, 7], fill="#ff5555")
            st["_last_key"] = key
            return img

        f = get_font(11, cjk=True) if ctx.orientation == LANDSCAPE else get_font(12, cjk=True)
        line_h = 14
        max_w = W - 8
        lines = self._wrap(flat, f, max_w)
        total_h = len(lines) * line_h

        if total_h <= H - 4:
            off = 0
        else:
            max_scroll = total_h - (H - 4)
            if lock:
                with lock:
                    st["scroll"] = (st.get("scroll", 0.0) + 1) % (max_scroll + 20)
                    off = int(st["scroll"])
            else:
                off = 0

        key = "%sx%s|%s|%s|%s" % (W, H, pinned, off, flat)
        if st.get("_last_key") == key:
            return None

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        if off == 0 and total_h <= H - 4:
            y = (H - total_h) // 2
            for line in lines:
                x = max(2, (W - text_w(f, line)) // 2)
                row_text(
                    img,
                    (x, y + line_h // 2),
                    line,
                    f,
                    "#e0e0e0",
                    max(1, W - x - 2),
                    ctx.now,
                    scroll=True,
                )
                y += line_h
        else:
            y = 2 - off
            for line in lines:
                if -line_h < y < H:
                    row_text(
                        img,
                        (2, y + line_h // 2),
                        line,
                        f,
                        "#e0e0e0",
                        max(1, W - 4),
                        ctx.now,
                        scroll=True,
                    )
                y += line_h

        if pinned:
            ImageDraw.Draw(img).ellipse([W - 8, 3, W - 4, 7], fill="#ff5555")

        st["_last_key"] = key
        return img

    def _wrap(self, text, font, max_w):
        out = []
        cur = []
        width = 0.0
        for ch in text:
            try:
                w = font.getlength(ch)
            except Exception:
                w = 7.0
            if width + w <= max_w:
                cur.append(ch)
                width += w
            else:
                if cur:
                    out.append("".join(cur))
                cur = [ch]
                width = w
        if cur:
            out.append("".join(cur))
        return out or [""]