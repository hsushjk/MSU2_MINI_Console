import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

from ._util import ellipsis, text_w

log = logging.getLogger("msu2.widgets.app_usage")

_SLEEP_GAP = 60.0
_SAVE_EVERY = 30.0
_SAVE_NAME = "app_usage.json"


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


def _day_key(lt):
    return (int(lt.tm_year), int(lt.tm_yday))


def _data_dir():
    try:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
        if base:
            return Path(base) / "MSU2_MINI"
        return Path.home() / ".msu2_mini"
    except Exception:
        return Path(__file__).resolve().parent.parent / "data"


def _load_saved():
    try:
        path = _data_dir() / _SAVE_NAME
        with open(str(path), "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return None
        day = obj.get("day")
        if not isinstance(day, (list, tuple)) or len(day) != 2:
            return None
        try:
            day = (int(day[0]), int(day[1]))
        except (TypeError, ValueError, OverflowError):
            return None
        today = obj.get("today")
        if not isinstance(today, dict):
            return None
        clean = {}
        for key, val in today.items():
            try:
                secs = float(val)
            except (TypeError, ValueError):
                continue
            if secs > 0 and secs == secs:
                clean[str(key)] = secs
        return day, clean
    except Exception as e:
        log.debug("读取 app_usage 统计失败: %s", e)
        return None


def _save_saved(day, today):
    tmp = None
    try:
        if not isinstance(today, dict):
            return False
        try:
            day2 = [int(day[0]), int(day[1])]
        except (TypeError, ValueError, IndexError, OverflowError):
            return False
        data = {"day": day2,
                "today": {str(k): round(float(v), 3) for k, v in today.items()}}
        path = _data_dir() / _SAVE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(str(tmp), "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
        tmp = None
        return True
    except Exception as e:
        log.debug("保存 app_usage 统计失败: %s", e)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(str(tmp))
            except Exception:
                pass


class _FgSource:
    def __init__(self, interval=1.0, stats=None):
        self._interval = max(0.5, float(interval))
        self._lock = threading.Lock()
        self._app = ""
        self._stop = threading.Event()
        self._thread = None
        self._alive = False
        self._stats = stats if isinstance(stats, dict) else {}

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop = threading.Event()
        self._alive = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="FgSource")
        try:
            self._thread.start()
            return True
        except Exception as e:
            self._alive = False
            log.debug("FgSource 启动失败: %s", e)
            return False

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass

    def alive(self):
        return bool(self._alive) and self._thread is not None and self._thread.is_alive()

    def get(self):
        with self._lock:
            return self._app

    def _commit(self, mono, app):
        st = self._stats
        lock = st.get("lock")
        if not hasattr(lock, "acquire"):
            return
        save_data = None
        with lock:
            today = st.get("today")
            if not isinstance(today, dict):
                today = {}
                st["today"] = today
            sample = st.get("_last_sample")
            if sample is not None and mono - float(sample) > _SLEEP_GAP:
                st["cur"] = app
                st["start"] = mono
                st["last_tick"] = mono
                st["_last_sample"] = mono
                st["t_poll"] = mono
                return
            day = _day_key(time.localtime())
            if tuple(st.get("day") or ()) != day:
                st["today"] = {}
                st["day"] = day
                st["cur"] = app
                st["start"] = mono
                st["last_tick"] = mono
                st["_last_sample"] = mono
                st["t_poll"] = mono
                return
            cur = st.get("cur") or ""
            last = st.get("last_tick")
            delta = max(0.0, mono - float(last)) if last is not None else 0.0
            if app != cur:
                if cur and delta > 0:
                    today[cur] = float(today.get(cur, 0.0)) + delta
                    st["_dirty"] = True
                st["cur"] = app
                st["start"] = mono
            elif cur and delta > 0:
                today[cur] = float(today.get(cur, 0.0)) + delta
                st["_dirty"] = True
            st["last_tick"] = mono
            st["_last_sample"] = mono
            st["t_poll"] = mono
            if st.get("_dirty") and mono - float(st.get("_save_ts") or 0.0) >= _SAVE_EVERY:
                st["_save_ts"] = mono
                st["_dirty"] = False
                save_data = (st.get("day"), dict(today))
        if save_data is not None:
            _save_saved(save_data[0], save_data[1])

    def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    app = _fg_app() or ""
                except Exception:
                    app = ""
                try:
                    with self._lock:
                        self._app = app
                except Exception:
                    pass
                try:
                    if self._stats:
                        self._commit(time.monotonic(), app)
                except Exception as e:
                    log.debug("FgSource 统计失败: %s", e)
                try:
                    self._stop.wait(self._interval)
                except Exception:
                    time.sleep(self._interval)
        finally:
            self._alive = False


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

    actions = {"reset": "重置今日"}

    def setup(self, ctx):
        try:
            self._setup(ctx)
        except Exception as e:
            log.warning("app_usage setup 失败: %s", e)
            st = ctx.state.get("_app")
            if isinstance(st, dict):
                self._stop_source(st)
                st["fg"] = None

    def _setup(self, ctx):
        cfg = self.get_config(ctx)
        poll_sec = self._poll_sec(cfg)
        st = self._state(ctx)
        self._stop_source(st)
        mono = time.monotonic()
        lock = st["lock"]
        with lock:
            self._roll_day(st, time.localtime(), mono)
            if not st.get("_loaded"):
                st["_loaded"] = True
                saved = _load_saved()
                if saved is not None and tuple(saved[0]) == tuple(st.get("day") or ()):
                    for key, secs in saved[1].items():
                        if secs > float(st["today"].get(key, 0.0)):
                            st["today"][key] = secs
            st["cur"] = ""
            st["start"] = mono
            st["last_tick"] = mono
            st["t_poll"] = mono
            st["_last_sample"] = mono
            st["_save_ts"] = mono
            st["_dirty"] = False
            st["last_key"] = None
            st["poll_sec"] = poll_sec
            st["_restart_at"] = 0.0
            st["_active"] = True
        src = _FgSource(poll_sec, st)
        st["fg"] = src if src.start() else None

    def teardown(self, ctx):
        st = ctx.state.get("_app")
        if not isinstance(st, dict):
            return
        st["_active"] = False
        try:
            self._stop_source(st)
        except Exception:
            pass
        try:
            lock = st.get("lock")
            if not hasattr(lock, "acquire"):
                return
            mono = time.monotonic()
            with lock:
                self._roll_day(st, time.localtime(), mono)
                today = st.get("today")
                if not isinstance(today, dict):
                    today = {}
                    st["today"] = today
                cur = st.get("cur") or ""
                last = st.get("last_tick")
                if cur and last is not None:
                    delta = max(0.0, mono - float(last))
                    if delta > 0:
                        today[cur] = float(today.get(cur, 0.0)) + delta
                st["last_tick"] = mono
                st["start"] = mono
                st["_dirty"] = False
                st["last_key"] = None
            _save_saved(st.get("day"), today)
        except Exception as e:
            log.debug("app_usage teardown 失败: %s", e)

    def on_enter(self, ctx):
        try:
            st = self._state(ctx)
            st["last_key"] = None
            st["_health_ts"] = 0.0
            if st.get("_active"):
                self._ensure_source(self._poll_sec(self.get_config(ctx)), st)
        except Exception as e:
            log.debug("app_usage on_enter 失败: %s", e)

    def on_leave(self, ctx):
        try:
            st = ctx.state.get("_app")
            if isinstance(st, dict):
                st["last_key"] = None
        except Exception:
            pass

    def render(self, ctx):
        try:
            cfg = self.get_config(ctx)
            poll_sec = self._poll_sec(cfg)
            st = self._state(ctx)
            self._ensure_source(poll_sec, st)
            mono = time.monotonic()
            lock = st["lock"]
            with lock:
                self._roll_day(st, time.localtime(), mono)
                sample = st.get("_last_sample")
                if sample is not None and mono - float(sample) > _SLEEP_GAP:
                    st["start"] = mono
                    st["last_tick"] = mono
                    st["_last_sample"] = mono
                session = max(0.0, mono - float(st.get("start") or mono))
                cur = st.get("cur") or ""
                today_map = st.get("today")
                if not isinstance(today_map, dict):
                    today_map = {}
                    st["today"] = today_map
                residual = 0.0
                if cur:
                    last = st.get("last_tick")
                    if last is not None:
                        residual = max(0.0, mono - float(last))
                today = float(today_map.get(cur, 0.0)) + residual
            if cfg.get("strip_ext") and cur.lower().endswith(".exe"):
                cur = cur[:-4]
            disp = cur or "(无)"
            key = (ctx.orientation, disp, _fmt_dur(session), _fmt_dur(today))
            with lock:
                if st.get("last_key") == key:
                    return None
                st["last_key"] = key
            W, H = ctx.size
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            if ctx.orientation == LANDSCAPE:
                self._draw_landscape(d, W, H, disp, session, today)
            else:
                self._draw_portrait(d, W, H, disp, session, today)
            return img
        except Exception as e:
            log.debug("app_usage render 失败: %s", e)
            return None

    def on_action(self, action, payload, ctx):
        if action != "reset":
            return {"ok": False, "err": f"未知动作: {action}"}
        try:
            st = self._state(ctx)
            mono = time.monotonic()
            lock = st["lock"]
            with lock:
                st["today"] = {}
                st["cur"] = ""
                st["start"] = mono
                st["last_tick"] = mono
                st["t_poll"] = mono
                st["_last_sample"] = mono
                st["_save_ts"] = mono
                st["_dirty"] = False
                st["last_key"] = None
                day = st.get("day")
            if not _save_saved(day, {}):
                log.debug("app_usage 清零后保存失败")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    def _state(self, ctx):
        st = ctx.state.get("_app")
        if not isinstance(st, dict):
            st = {}
            ctx.state["_app"] = st
        lock = st.get("lock")
        if not hasattr(lock, "acquire"):
            st["lock"] = threading.RLock()
        if not isinstance(st.get("today"), dict):
            st["today"] = {}
        mono = time.monotonic()
        if "cur" not in st:
            st["cur"] = ""
        if "start" not in st:
            st["start"] = mono
        if "last_tick" not in st:
            st["last_tick"] = st["start"]
        if "day" not in st:
            st["day"] = _day_key(time.localtime())
        if "t_poll" not in st:
            st["t_poll"] = 0.0
        return st

    def _ensure_source(self, poll_sec, st):
        if not st.get("_active"):
            return st.get("fg")
        src = st.get("fg")
        try:
            if isinstance(src, _FgSource) and src.alive():
                return src
        except Exception:
            pass
        mono = time.monotonic()
        if mono - float(st.get("_health_ts") or 0.0) < 5.0:
            return src
        st["_health_ts"] = mono
        if src is not None:
            try:
                src.stop()
            except Exception:
                pass
            st["fg"] = None
        lock = st.get("lock")
        if hasattr(lock, "acquire"):
            with lock:
                sample = st.get("_last_sample")
                if sample is not None and mono - float(sample) > _SLEEP_GAP:
                    st["cur"] = ""
                    st["start"] = mono
                    st["last_tick"] = mono
                    st["_last_sample"] = mono
        new = _FgSource(poll_sec, st)
        if new.start():
            st["fg"] = new
            return new
        st["fg"] = None
        return None

    def _stop_source(self, st):
        src = st.get("fg")
        st["fg"] = None
        if src is not None:
            try:
                src.stop()
            except Exception:
                pass

    @staticmethod
    def _roll_day(st, lt, mono):
        day = _day_key(lt)
        if tuple(st.get("day") or ()) != day:
            st["today"] = {}
            st["day"] = day
            st["cur"] = ""
            st["start"] = mono
            st["last_tick"] = mono
            st["_last_sample"] = mono
            st["_dirty"] = True
        return day

    def _draw_landscape(self, d, W, H, cur, session, today):
        f_title = get_font(10, cjk=True)
        f_app = get_font(18, bold=True, cjk=True)
        f_lbl = get_font(9, cjk=True)
        f_val = get_font(12, bold=True, cjk=False)

        d.text((W // 2, 6), "当前应用",
               fill="#808080", font=f_title, anchor="mm")
        app = ellipsis(f_app, cur, W - 8)
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
        f_val = get_font(12, bold=True, cjk=False)

        d.text((W // 2, 12), "当前应用",
               fill="#808080", font=f_title, anchor="mm")

        lines = self._wrap(cur, f_app, W - 8, max_lines=3)
        y = 28
        for line in lines:
            d.text((W // 2, y), line, fill="#ffffff", font=f_app, anchor="mm")
            y += 18

        y = max(y + 8, H - 60)
        d.text((4, y), "本次", fill="#808080", font=f_lbl, anchor="lt")
        d.text((W - 4, y), _fmt_dur(session),
               fill="#00e0ff", font=f_val, anchor="rt")
        d.text((4, y + 26), "今日", fill="#808080", font=f_lbl, anchor="lt")
        d.text((W - 4, y + 26), _fmt_dur(today),
               fill="#22cc66", font=f_val, anchor="rt")

    @staticmethod
    def _poll_sec(cfg):
        try:
            v = float(cfg.get("poll_sec", 1.0))
        except (TypeError, ValueError):
            v = 1.0
        if v != v:
            v = 1.0
        return max(0.5, min(5.0, v))

    @staticmethod
    def _wrap(text, font, max_w, max_lines=3):
        text = str(text or "").strip()
        if not text:
            return ["(无)"]
        lines = []
        cur = ""
        for ch in text:
            if text_w(font, cur + ch) <= max_w or not cur:
                cur += ch
                continue
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
        if len(lines) < max_lines:
            if cur:
                lines.append(cur)
        else:
            consumed = sum(len(x) for x in lines)
            lines[-1] = ellipsis(font, lines[-1] + text[consumed:], max_w)
        return lines[:max_lines]