import logging
import math
import threading
import time
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.mouse_mileage")

@register
class MouseMileageWidget(Widget):
    name = "mouse_mileage"
    interval = 1.0
    orientations = BOTH
    priority = 65

    config_schema = [
        Field("dpi", "int", "鼠标 DPI", default=800, min=200, max=6400),
        Field("reset_hour", "int", "每日重置小时", default=0, min=0, max=23),
        Field("unit", "select", "里程单位", default="m",
              options=["m", "cm", "km"]),
    ]

    def setup(self, ctx):
        reset_h = int(self.get_config(ctx).get("reset_hour", 0))
        st = {
            "px": 0.0,
            "day_key": self._day_key(time.localtime(), reset_h),
            "last": None,
            "lock": threading.Lock(),
            "listener": None,
            "reset_h": reset_h,
        }
        ctx.state["_mouse"] = st

        try:
            from pynput import mouse
        except ImportError:
            log.warning("mouse_mileage: 未安装 pynput")
            st["available"] = False
            return
        st["available"] = True

        def on_move(x, y):
            now = time.time()
            key = self._day_key(time.localtime(now), st["reset_h"])
            with st["lock"]:
                if key != st["day_key"]:
                    st["px"] = 0.0
                    st["day_key"] = key
                    st["last"] = None
                if st["last"] is not None:
                    dx = x - st["last"][0]
                    dy = y - st["last"][1]
                    st["px"] += math.hypot(dx, dy)
                st["last"] = (x, y)

        def loop():
            try:
                with mouse.Listener(on_move=on_move) as lst:
                    st["listener"] = lst
                    lst.join()
            except Exception as e:
                log.warning("mouse listener 退出: %s", e)

        threading.Thread(target=loop, daemon=True,
                         name="MouseListener").start()

    def teardown(self, ctx):
        st = ctx.state.get("_mouse")
        if st and st.get("listener"):
            try: st["listener"].stop()
            except Exception: pass

    def on_enter(self, ctx):
        st = ctx.state.get("_mouse")
        if st:
            st["last"] = None

    def render(self, ctx):
        cfg = self.get_config(ctx)
        st = ctx.state.get("_mouse") or {}

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
            px = st["px"]

        dpi = max(200, int(cfg["dpi"]))
        meters = px / dpi * 0.0254
        unit = cfg["unit"]
        if unit == "cm":
            value, unit_txt = meters * 100, "cm"
        elif unit == "km":
            value, unit_txt = meters / 1000, "km"
        else:
            value, unit_txt = meters, "m"

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(d, W, H, value, unit_txt)
        else:
            self._draw_portrait(d, W, H, value, unit_txt)
        return img

    def _draw_landscape(self, d, W, H, value, unit_txt):
        f_title = get_font(10, cjk=True)
        f_num = get_font(32, bold=True, cjk=False)
        f_unit = get_font(12, cjk=True)
        d.text((6, 4), "今日鼠标里程", fill="#808080", font=f_title)
        s = f"{value:.2f}" if value < 100 else f"{value:.1f}"
        d.text((W // 2 - 8, H // 2 + 2), s,
               fill="#ff6b9d", font=f_num, anchor="mm")
        d.text((W - 6, H // 2 + 12), unit_txt,
               fill="#909090", font=f_unit, anchor="rm")

    def _draw_portrait(self, d, W, H, value, unit_txt):
        f_title = get_font(11, cjk=True)
        f_num = get_font(36, bold=True, cjk=False)
        f_unit = get_font(14, cjk=True)
        d.text((W // 2, 24), "今日鼠标里程", fill="#808080",
               font=f_title, anchor="mm")
        s = f"{value:.2f}" if value < 100 else f"{value:.1f}"
        d.text((W // 2, 80), s,
               fill="#ff6b9d", font=f_num, anchor="mm")
        d.text((W // 2, 116), unit_txt,
               fill="#909090", font=f_unit, anchor="mm")

    @staticmethod
    def _day_key(lt, reset_hour):
        yday = lt.tm_yday
        if lt.tm_hour < reset_hour:
            yday -= 1
        return (lt.tm_year, yday)
