import logging
import threading
import time
from collections import deque
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.keyboard_stats")

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

    def setup(self, ctx):
        st = {
            "today": 0,
            "day": time.localtime().tm_yday,
            "recent": deque(),
            "lock": threading.Lock(),
            "listener": None,
            "last_key": None,
        }
        ctx.state["_kbd"] = st

        try:
            from pynput import keyboard
        except ImportError:
            log.warning("keyboard_stats: 未安装 pynput，插件不可用"
                        "  (pip install pynput)")
            st["available"] = False
            return
        st["available"] = True

        count_repeat = bool(self.get_config(ctx).get("count_repeat", False))
        st["count_repeat"] = count_repeat

        def on_press(key):
            now = time.time()
            lt = time.localtime(now)
            with st["lock"]:
                if lt.tm_yday != st["day"]:
                    st["today"] = 0
                    st["recent"].clear()
                    st["day"] = lt.tm_yday

                if not st["count_repeat"]:
                    key_id = self._key_id(key)
                    if key_id == st["last_key"] and st["recent"]:

                        if now - st["recent"][-1] < 0.03:
                            return
                    st["last_key"] = key_id
                st["today"] += 1
                st["recent"].append(now)

        def on_release(key):

            st["last_key"] = None

        def loop():
            try:
                with keyboard.Listener(on_press=on_press,
                                       on_release=on_release) as lst:
                    st["listener"] = lst
                    lst.join()
            except Exception as e:
                log.warning("keyboard listener 退出: %s", e)

        threading.Thread(target=loop, daemon=True,
                         name="KbdListener").start()

    def teardown(self, ctx):
        st = ctx.state.get("_kbd")
        if st and st.get("listener"):
            try: st["listener"].stop()
            except Exception: pass

    def on_enter(self, ctx):
        st = ctx.state.get("_kbd")
        if st:
            st["last_key"] = None

    def render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state.get("_kbd") or {}

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if not st.get("available"):
            d.text((W // 2, H // 2 - 8), "需要 pynput",
                   fill="#c05050", font=get_font(13, cjk=True), anchor="mm")
            d.text((W // 2, H // 2 + 10), "pip install pynput",
                   fill="#666666", font=get_font(10, cjk=False), anchor="mm")
            return img

        with st["lock"]:
            today = st["today"]
            recent = list(st["recent"])

        now = time.time()
        recent = [t for t in recent if now - t <= 60]
        with st["lock"]:
            st["recent"] = deque(recent)
        kpm = len(recent)

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(d, W, H, today, kpm, cfg.get("show_kpm", True))
        else:
            self._draw_portrait(d, W, H, today, kpm, cfg.get("show_kpm", True))
        return img

    def _draw_landscape(self, d, W, H, today, kpm, show_kpm):
        f_title = get_font(10, cjk=True)
        f_num = get_font(34, bold=True, cjk=False)
        f_unit = get_font(12, cjk=True)

        d.text((6, 4), "今日敲击", fill="#808080", font=f_title)

        s = str(today)
        d.text((W // 2, H // 2 + 2), s,
               fill="#00e0ff", font=f_num, anchor="mm")

        if show_kpm:
            d.text((W - 6, 6), "KPM", fill="#808080", font=f_title, anchor="ra")
            d.text((W - 6, 22), str(kpm),
                   fill="#22cc66", font=get_font(22, bold=True, cjk=False),
                   anchor="ra")

    def _draw_portrait(self, d, W, H, today, kpm, show_kpm):
        f_title = get_font(11, cjk=True)
        f_num = get_font(40, bold=True, cjk=False)

        d.text((W // 2, 16), "今日敲击", fill="#808080",
               font=f_title, anchor="mm")
        d.text((W // 2, 58), str(today),
               fill="#00e0ff", font=f_num, anchor="mm")

        if show_kpm:
            d.line([(8, 106), (W - 8, 106)], fill="#303030")
            d.text((W // 2, 118), "KPM", fill="#808080",
                   font=f_title, anchor="mm")
            d.text((W // 2, 140), str(kpm),
                   fill="#22cc66",
                   font=get_font(22, bold=True, cjk=False), anchor="mm")

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
