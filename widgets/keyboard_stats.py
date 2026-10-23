import logging
import threading
import time
from collections import deque

from PIL import Image, ImageDraw

from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.keyboard_stats")

_WATCH_SEC = 30.0
_START_GRACE = 5.0


@register
class KeyboardStatsWidget(Widget):
    name = "keyboard_stats"
    interval = 1.0
    orientations = BOTH
    priority = 64

    config_schema = [
        Field("show_kpm", "bool", "显示 KPM", default=True),
        Field("count_repeat", "bool", "统计长按重复", default=False,
              help="关闭时长按同一键只算一次"),
    ]

    actions = {"reset": "清零今日"}

    def setup(self, ctx):
        try:
            self._setup(ctx)
        except Exception as e:
            log.warning("keyboard_stats setup 失败: %s", e)
            st = ctx.state.get("_kbd")
            if isinstance(st, dict):
                st["_active"] = False
                self._stop_watch(st)
                self._stop_listener(st)

    def _setup(self, ctx):
        cfg = self.get_config(ctx)
        st = self._state(ctx)
        st["count_repeat"] = bool(cfg.get("count_repeat", False))
        st["last_key"] = None
        st["fails"] = 0
        st["retry_at"] = 0.0
        st["backoff"] = 0.0
        st["_started_ts"] = 0.0
        st["_health_ts"] = 0.0
        st["_active"] = True
        self._stop_watch(st)
        self._stop_listener(st)
        try:
            from pynput import keyboard as kb_mod
        except Exception as e:
            log.warning("keyboard_stats: 未安装 pynput，插件不可用"
                        "  (pip install pynput): %s", e)
            st["available"] = False
            st["_kb_mod"] = None
            return
        st["available"] = True
        st["_kb_mod"] = kb_mod
        lock = st["lock"]

        def on_press(key):
            try:
                now = time.time()
                lt = time.localtime(now)
                key_id = self._key_id(key)
                with lock:
                    day = (int(lt.tm_year), int(lt.tm_yday))
                    if tuple(st.get("day") or ()) != day:
                        st["today"] = 0
                        st["recent"].clear()
                        st["pressed"].clear()
                        st["day"] = day
                    if not st.get("count_repeat"):
                        if key_id in st["pressed"]:
                            return
                        st["pressed"].add(key_id)
                    st["today"] = int(st.get("today") or 0) + 1
                    st["recent"].append(now)
            except Exception as e:
                log.debug("keyboard on_press: %s", e)

        def on_release(key):
            try:
                with lock:
                    st["pressed"].discard(self._key_id(key))
            except Exception as e:
                log.debug("keyboard on_release: %s", e)

        st["_on_press"] = on_press
        st["_on_release"] = on_release
        self._start_listener(st)
        self._start_watch(st)

    def teardown(self, ctx):
        st = ctx.state.get("_kbd")
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

    def on_enter(self, ctx):
        try:
            st = self._state(ctx)
            st["last_key"] = None
            st["_health_ts"] = 0.0
            if st.get("_active") and st.get("available") and not self._listener_ok(st):
                self._check_listener(st)
        except Exception as e:
            log.debug("keyboard_stats on_enter 失败: %s", e)

    def on_action(self, action, payload, ctx):
        if action != "reset":
            return {"ok": False, "err": f"未知动作: {action}"}
        try:
            st = self._state(ctx)
            lock = st["lock"]
            with lock:
                st["today"] = 0
                recent = st.get("recent")
                if isinstance(recent, deque):
                    recent.clear()
                pressed = st.get("pressed")
                if isinstance(pressed, set):
                    pressed.clear()
                st["last_key"] = None
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
            if st.get("_active") and mono - float(st.get("_health_ts") or 0.0) >= _START_GRACE:
                st["_health_ts"] = mono
                if not self._listener_ok(st):
                    self._check_listener(st)
            st["count_repeat"] = bool(cfg.get("count_repeat", False))
            lock = st["lock"]
            recent = st.get("recent")
            if not isinstance(recent, deque):
                return None
            now = time.time()
            with lock:
                while recent and now - recent[0] > 60:
                    recent.popleft()
                today = int(st.get("today") or 0)
                kpm = len(recent)
                show_kpm = bool(cfg.get("show_kpm", True))
                key = (ctx.orientation, today, kpm if show_kpm else None)
                if st.get("last_key") == key:
                    return None
                st["last_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            if ctx.orientation == LANDSCAPE:
                self._draw_landscape(d, W, H, today, kpm, show_kpm)
            else:
                self._draw_portrait(d, W, H, today, kpm, show_kpm)
            return img
        except Exception as e:
            log.debug("keyboard_stats render 失败: %s", e)
            return None

    def _state(self, ctx):
        st = ctx.state.get("_kbd")
        if not isinstance(st, dict):
            st = {}
            ctx.state["_kbd"] = st
        if not isinstance(st.get("today"), int):
            st["today"] = 0
        day = st.get("day")
        if not (isinstance(day, (tuple, list)) and len(day) == 2):
            st["day"] = (time.localtime().tm_year, time.localtime().tm_yday)
        else:
            st["day"] = (int(day[0]), int(day[1]))
        recent = st.get("recent")
        if not isinstance(recent, deque) or recent.maxlen != 4096:
            try:
                items = list(recent) if recent else []
            except TypeError:
                items = []
            st["recent"] = deque(items[-4096:], maxlen=4096)
        if not hasattr(st.get("lock"), "acquire"):
            st["lock"] = threading.RLock()
        if not hasattr(st.get("ctrl"), "acquire"):
            st["ctrl"] = threading.RLock()
        if not isinstance(st.get("pressed"), set):
            st["pressed"] = set()
        if not isinstance(st.get("gen"), int):
            st["gen"] = 0
        return st

    def _start_listener(self, st):
        self._stop_listener(st)
        if not st.get("available"):
            return False
        kb_mod = st.get("_kb_mod")
        if kb_mod is None:
            return False
        gen = int(st.get("gen") or 0) + 1
        st["gen"] = gen
        thread = threading.Thread(
            target=self._listener_loop, args=(st, gen, kb_mod),
            daemon=True, name="KbdListener")
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

    def _listener_loop(self, st, gen, kb_mod):
        try:
            listener = kb_mod.Listener(
                on_press=st.get("_on_press"), on_release=st.get("_on_release"))
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
            log.warning("keyboard_stats 重建监听失败(%d 次)，%.0f 秒后重试: %s",
                        fails, backoff, err)

    def _start_watch(self, st):
        self._stop_watch(st)
        stop = threading.Event()
        thread = threading.Thread(
            target=self._watch_loop, args=(st, stop),
            daemon=True, name="KbdWatchdog")
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
                log.debug("keyboard_stats 看护异常: %s", e)
            try:
                stop.wait(_WATCH_SEC)
            except Exception:
                time.sleep(_WATCH_SEC)

    def _draw_landscape(self, d, W, H, today, kpm, show_kpm):
        f_title = get_font(10, cjk=True)

        d.text((6, 4), "今日敲击", fill="#808080", font=f_title)

        s = str(today)
        f_num = self._num_font(s, W - 24, (34, 28, 22, 18))
        d.text((W // 2, H // 2 + 2), s,
               fill="#00e0ff", font=f_num, anchor="mm")

        if show_kpm:
            d.text((W - 6, 6), "KPM", fill="#808080", font=f_title, anchor="ra")
            d.text((W - 6, 22), str(kpm),
                   fill="#22cc66", font=get_font(22, bold=True, cjk=False),
                   anchor="ra")

    def _draw_portrait(self, d, W, H, today, kpm, show_kpm):
        f_title = get_font(11, cjk=True)

        d.text((W // 2, 16), "今日敲击", fill="#808080",
               font=f_title, anchor="mm")
        s = str(today)
        f_num = self._num_font(s, W - 8, (40, 34, 28, 22, 18))
        d.text((W // 2, 58), s,
               fill="#00e0ff", font=f_num, anchor="mm")

        if show_kpm:
            d.line([(8, 106), (W - 8, 106)], fill="#303030")
            d.text((W // 2, 118), "KPM", fill="#808080",
                   font=f_title, anchor="mm")
            d.text((W // 2, 140), str(kpm),
                   fill="#22cc66",
                   font=get_font(22, bold=True, cjk=False), anchor="mm")

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
    def _key_id(key):
        try:
            if hasattr(key, "vk"):
                return ("vk", key.vk)
            if hasattr(key, "value"):
                return ("val", str(key.value))
            return ("str", str(key))
        except Exception:
            return ("str", "?")