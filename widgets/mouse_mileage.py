import datetime
import json
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.mouse_mileage")

_WATCH_SEC = 30.0
_START_GRACE = 5.0
_SLEEP_GAP = 60.0
_SAVE_EVERY = 30.0
_SAVE_NAME = "mouse_mileage.json"


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
            px = float(obj.get("px"))
        except (TypeError, ValueError, OverflowError):
            return None
        if px != px or px < 0:
            return None
        return day, px
    except Exception as e:
        log.debug("读取 mouse_mileage 里程失败: %s", e)
        return None


def _save_saved(day_key, px):
    tmp = None
    try:
        try:
            day = [int(day_key[0]), int(day_key[1])]
        except (TypeError, ValueError, IndexError, OverflowError):
            return False
        value = float(px)
        if value != value or value < 0:
            value = 0.0
        data = {"day": day, "px": round(value, 3)}
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
        log.debug("保存 mouse_mileage 里程失败: %s", e)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(str(tmp))
            except Exception:
                pass


@register
class MouseMileageWidget(Widget):
    name = "mouse_mileage"
    interval = 1.0
    orientations = BOTH
    priority = 65

    config_schema = [
        Field("dpi", "int", "鼠标 DPI", default=800, min=200, max=6400),
        Field("unit", "select", "里程单位", default="m",
              options=["m", "cm", "km"]),
    ]

    actions = {"clear": "清零今日"}

    def setup(self, ctx):
        try:
            self._setup(ctx)
        except Exception as e:
            log.warning("mouse_mileage setup 失败: %s", e)
            st = ctx.state.get("_mouse")
            if isinstance(st, dict):
                st["_active"] = False
                self._stop_watch(st)
                self._stop_listener(st)

    def _setup(self, ctx):
        self._dpi_aware()
        st = self._state(ctx)
        st["last_key"] = None
        st["fails"] = 0
        st["retry_at"] = 0.0
        st["backoff"] = 0.0
        st["_started_ts"] = 0.0
        st["_health_ts"] = 0.0
        st["_active"] = True
        self._stop_watch(st)
        self._stop_listener(st)
        day_key = self._day_key(time.localtime())
        lock = st["lock"]
        with lock:
            if st.get("day_key") != day_key:
                st["px"] = 0.0
                st["last"] = None
                st["_dirty"] = False
            st["day_key"] = day_key
            st["key_t"] = 0.0
            if not st.get("_loaded"):
                st["_loaded"] = True
                saved = _load_saved()
                if saved is not None and tuple(saved[0]) == day_key:
                    st["px"] = max(float(st.get("px") or 0.0), saved[1])
            st["_save_ts"] = time.monotonic()
        try:
            from pynput import mouse as mouse_mod
        except Exception as e:
            log.warning("mouse_mileage: 未安装 pynput，插件不可用"
                        "  (pip install pynput): %s", e)
            st["available"] = False
            st["_mouse_mod"] = None
            return
        st["available"] = True
        st["_mouse_mod"] = mouse_mod

        def on_move(x, y):
            try:
                now = time.time()
                with lock:
                    last_ts = float(st.get("last_ts") or 0.0)
                    if last_ts > 0 and now - last_ts > _SLEEP_GAP:
                        st["last"] = (x, y)
                        st["last_ts"] = now
                        return
                    if now - float(st.get("key_t") or 0.0) >= 1.0:
                        st["key_t"] = now
                        key = self._day_key(time.localtime(now))
                        if key != st.get("day_key"):
                            st["px"] = 0.0
                            st["day_key"] = key
                            st["last"] = None
                    last = st.get("last")
                    if last is not None:
                        st["px"] = float(st.get("px") or 0.0) + math.hypot(
                            x - last[0], y - last[1])
                        st["_dirty"] = True
                    st["last"] = (x, y)
                    st["last_ts"] = now
            except Exception as e:
                log.debug("mouse on_move: %s", e)

        st["_on_move"] = on_move
        self._start_listener(st)
        self._start_watch(st)

    def teardown(self, ctx):
        st = ctx.state.get("_mouse")
        if not isinstance(st, dict):
            return
        st["_active"] = False
        try:
            self._stop_watch(st)
        except Exception:
            pass
        try:
            self._stop_listener(st)
        except Exception:
            pass
        try:
            self._maybe_save(st, force=True)
        except Exception:
            pass

    def on_enter(self, ctx):
        try:
            st = self._state(ctx)
            st["last_key"] = None
            st["_health_ts"] = 0.0
            if st.get("_active") and st.get("available") and not self._listener_ok(st):
                self._check_listener(st)
        except Exception as e:
            log.debug("mouse_mileage on_enter 失败: %s", e)

    def on_leave(self, ctx):
        try:
            st = ctx.state.get("_mouse")
            if isinstance(st, dict):
                st["last_key"] = None
                self._maybe_save(st, force=True)
        except Exception as e:
            log.debug("mouse_mileage on_leave 失败: %s", e)

    def on_action(self, action, payload, ctx):
        if action != "clear":
            return {"ok": False, "err": f"未知动作: {action}"}
        try:
            st = self._state(ctx)
            lock = st["lock"]
            day_key = self._day_key(time.localtime())
            with lock:
                st["px"] = 0.0
                st["last"] = None
                st["last_ts"] = 0.0
                st["key_t"] = 0.0
                st["day_key"] = day_key
                st["_dirty"] = False
                st["last_key"] = None
            if not _save_saved(day_key, 0.0):
                log.debug("mouse_mileage 清零后保存失败")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "err": str(e)}

    def render(self, ctx):
        try:
            cfg = self.get_config(ctx)
            st = self._state(ctx)
            W, H = ctx.size
            if not st.get("available"):
                key = (ctx.orientation, "na")
                if st.get("last_key") == key:
                    return None
                st["last_key"] = key
                img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                d.text((W // 2, H // 2 - 8), "需要 pynput",
                       fill="#c05050", font=get_font(13, cjk=True),
                       anchor="mm")
                d.text((W // 2, H // 2 + 10), "pip install pynput",
                       fill="#666666", font=get_font(10, cjk=False),
                       anchor="mm")
                return img
            mono = time.monotonic()
            if st.get("_active") and mono - float(st.get("_health_ts") or 0.0) >= 5.0:
                st["_health_ts"] = mono
                if not self._listener_ok(st):
                    self._check_listener(st)
            lock = st["lock"]
            with lock:
                px = float(st.get("px") or 0.0)
            dpi = self._clamp_int(cfg.get("dpi"), 800, 200, 6400)
            unit = cfg.get("unit")
            if unit not in ("m", "cm", "km"):
                unit = "m"
            meters = px / dpi * 0.0254
            if unit == "cm":
                value, unit_txt = meters * 100, "cm"
            elif unit == "km":
                value, unit_txt = meters / 1000, "km"
            else:
                value, unit_txt = meters, "m"
            s = self._fmt_value(value)
            key = (ctx.orientation, unit_txt, s)
            with lock:
                if st.get("last_key") == key:
                    return None
                st["last_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            if ctx.orientation == LANDSCAPE:
                self._draw_landscape(d, W, H, s, unit_txt)
            else:
                self._draw_portrait(d, W, H, s, unit_txt)
            return img
        except Exception as e:
            log.debug("mouse_mileage render 失败: %s", e)
            return None

    def _state(self, ctx):
        st = ctx.state.get("_mouse")
        if not isinstance(st, dict):
            st = {}
            ctx.state["_mouse"] = st
        if not isinstance(st.get("px"), (int, float)):
            st["px"] = 0.0
        if "last" not in st:
            st["last"] = None
        if not isinstance(st.get("day_key"), (tuple, list)):
            st["day_key"] = self._day_key(time.localtime())
        else:
            st["day_key"] = (int(st["day_key"][0]), int(st["day_key"][1]))
        if "key_t" not in st:
            st["key_t"] = 0.0
        if "last_ts" not in st:
            st["last_ts"] = 0.0
        if not hasattr(st.get("lock"), "acquire"):
            st["lock"] = threading.RLock()
        if not hasattr(st.get("ctrl"), "acquire"):
            st["ctrl"] = threading.RLock()
        if not isinstance(st.get("gen"), int):
            st["gen"] = 0
        return st

    def _start_listener(self, st):
        self._stop_listener(st)
        if not st.get("available"):
            return False
        mouse_mod = st.get("_mouse_mod")
        if mouse_mod is None:
            return False
        gen = int(st.get("gen") or 0) + 1
        st["gen"] = gen
        thread = threading.Thread(
            target=self._listener_loop, args=(st, gen, mouse_mod),
            daemon=True, name="MouseListener")
        st["thread"] = thread
        try:
            thread.start()
        except Exception as e:
            st["thread"] = None
            self._note_fail(st, e)
            return False
        return True

    def _stop_listener(self, st):
        ctrl = st.get("ctrl")
        listener = None
        thread = None
        if hasattr(ctrl, "acquire"):
            with ctrl:
                st["gen"] = int(st.get("gen") or 0) + 1
                listener = st.get("listener")
                thread = st.get("thread")
                st["listener"] = None
                st["thread"] = None
                st["_started_ts"] = 0.0
        else:
            listener = st.get("listener")
            thread = st.get("thread")
            st["listener"] = None
            st["thread"] = None
            st["_started_ts"] = 0.0
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        if (isinstance(thread, threading.Thread) and thread.is_alive()
                and thread is not threading.current_thread()):
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass
        if hasattr(ctrl, "acquire"):
            with ctrl:
                late = st.get("listener")
                if late is not None and late is not listener:
                    st["listener"] = None
                    try:
                        late.stop()
                    except Exception:
                        pass

    def _listener_loop(self, st, gen, mouse_mod):
        try:
            listener = mouse_mod.Listener(on_move=st.get("_on_move"))
            listener.start()
            ctrl = st.get("ctrl")
            stale = False
            if hasattr(ctrl, "acquire"):
                with ctrl:
                    stale = st.get("gen") != gen
                    if not stale:
                        st["listener"] = listener
                        st["_started_ts"] = time.monotonic()
            else:
                stale = st.get("gen") != gen
                if not stale:
                    st["listener"] = listener
                    st["_started_ts"] = time.monotonic()
            if stale:
                try:
                    listener.stop()
                except Exception:
                    pass
                return
            lock = st.get("lock")
            if hasattr(lock, "acquire"):
                with lock:
                    st["last"] = None
                    st["last_ts"] = 0.0
            listener.join()
        except Exception as e:
            if st.get("gen") == gen:
                self._note_fail(st, e)
        finally:
            if st.get("thread") is threading.current_thread():
                st["listener"] = None
                st["thread"] = None

    def _listener_ok(self, st):
        listener = st.get("listener")
        thread = st.get("thread")
        try:
            if listener is not None:
                if bool(getattr(listener, "running", True)):
                    return True
                started = float(st.get("_started_ts") or 0.0)
                return time.monotonic() - started < _START_GRACE
            return isinstance(thread, threading.Thread) and thread.is_alive()
        except Exception:
            return False

    def _check_listener(self, st):
        ctrl = st.get("ctrl")
        if not hasattr(ctrl, "acquire"):
            return
        if not ctrl.acquire(blocking=False):
            return
        try:
            if not st.get("available"):
                return
            mono = time.monotonic()
            if mono < float(st.get("retry_at") or 0.0):
                return
            if self._listener_ok(st):
                st["fails"] = 0
                return
            self._start_listener(st)
        except Exception as e:
            self._note_fail(st, e)
        finally:
            ctrl.release()

    def _note_fail(self, st, err):
        fails = int(st.get("fails") or 0) + 1
        st["fails"] = fails
        backoff = min(300.0, 10.0 * (2 ** min(fails - 1, 5)))
        st["backoff"] = backoff
        st["retry_at"] = time.monotonic() + backoff
        if fails <= 2 or fails % 8 == 0:
            log.warning("mouse_mileage 重建监听失败(%d 次)，%.0f 秒后重试: %s",
                        fails, backoff, err)

    def _maybe_save(self, st, force=False):
        lock = st.get("lock")
        if not hasattr(lock, "acquire"):
            return
        data = None
        with lock:
            mono = time.monotonic()
            if not force:
                if not st.get("_dirty"):
                    return
                if mono - float(st.get("_save_ts") or 0.0) < _SAVE_EVERY:
                    return
            st["_dirty"] = False
            st["_save_ts"] = mono
            data = (st.get("day_key"), float(st.get("px") or 0.0))
        if data is not None:
            _save_saved(data[0], data[1])

    def _start_watch(self, st):
        self._stop_watch(st)
        stop = threading.Event()
        thread = threading.Thread(
            target=self._watch_loop, args=(st, stop),
            daemon=True, name="MouseWatchdog")
        st["watch_stop"] = stop
        st["watch_thread"] = thread
        thread.start()

    def _stop_watch(self, st):
        stop = st.get("watch_stop")
        thread = st.get("watch_thread")
        st["watch_stop"] = None
        st["watch_thread"] = None
        if stop is not None:
            try:
                stop.set()
            except Exception:
                pass
        if (isinstance(thread, threading.Thread) and thread.is_alive()
                and thread is not threading.current_thread()):
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass

    def _watch_loop(self, st, stop):
        while not stop.is_set():
            try:
                self._check_listener(st)
            except Exception as e:
                log.debug("mouse_mileage 看护异常: %s", e)
            try:
                self._maybe_save(st)
            except Exception as e:
                log.debug("mouse_mileage 保存异常: %s", e)
            try:
                stop.wait(_WATCH_SEC)
            except Exception:
                time.sleep(_WATCH_SEC)

    def _draw_landscape(self, d, W, H, s, unit_txt):
        f_title = get_font(10, cjk=True)
        f_unit = get_font(12, cjk=True)
        d.text((6, 4), "今日鼠标里程", fill="#808080", font=f_title)
        f_num = self._num_font(s, W - 64, (32, 28, 24, 20, 16))
        d.text((W // 2 - 8, H // 2 + 2), s,
               fill="#ff6b9d", font=f_num, anchor="mm")
        d.text((W - 6, H // 2 + 12), unit_txt,
               fill="#909090", font=f_unit, anchor="rm")

    def _draw_portrait(self, d, W, H, s, unit_txt):
        f_title = get_font(11, cjk=True)
        f_unit = get_font(14, cjk=True)
        d.text((W // 2, 24), "今日鼠标里程", fill="#808080",
               font=f_title, anchor="mm")
        f_num = self._num_font(s, W - 8, (36, 30, 26, 22, 18))
        d.text((W // 2, 80), s,
               fill="#ff6b9d", font=f_num, anchor="mm")
        d.text((W // 2, 116), unit_txt,
               fill="#909090", font=f_unit, anchor="mm")

    @staticmethod
    def _num_font(text, max_w, sizes):
        for size in sizes:
            font = get_font(size, bold=True, cjk=False)
            try:
                if font.getlength(text) <= max_w:
                    return font
            except Exception:
                return font
        return get_font(sizes[-1], bold=True, cjk=False)

    @staticmethod
    def _fmt_value(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        if value != value or value < 0:
            value = 0.0
        if value < 100:
            return f"{value:.2f}"
        return f"{value:.1f}"

    @staticmethod
    def _dpi_aware():
        if sys.platform != "win32":
            return
        try:
            import ctypes
        except Exception:
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    @staticmethod
    def _clamp_int(value, default, lo, hi):
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    @staticmethod
    def _day_key(lt):
        try:
            day = datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday)
            return (day.year, day.timetuple().tm_yday)
        except (TypeError, ValueError, OverflowError):
            return (lt.tm_year, lt.tm_yday)