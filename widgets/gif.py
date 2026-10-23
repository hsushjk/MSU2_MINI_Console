import logging

from PIL import Image

from msu2_core import Widget, register, BOTH

log = logging.getLogger("msu2.widgets.gif")


@register
class GifWidget(Widget):
    name = "gif"
    interval = 1.0
    orientations = BOTH
    priority = 90

    def on_enter(self, ctx):
        try:
            ctx.state.pop("_gif_done", None)
        except Exception:
            pass

    def render(self, ctx):
        try:
            if ctx.state.get("_gif_done") == ctx.size:
                return None
            ctx.state["_gif_done"] = ctx.size
            W, H = ctx.size
            return Image.new("RGBA", (W, H), (0, 0, 0, 0))
        except Exception as e:
            log.debug("gif render 失败: %s", e)
            return None