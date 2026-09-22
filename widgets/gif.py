from PIL import Image
from msu2_core import Widget, register, BOTH

@register
class GifWidget(Widget):
    name = "gif"
    interval = 0.1
    orientations = BOTH
    priority = 90

    def render(self, ctx):
        W, H = ctx.size
        return Image.new("RGBA", (W, H), (0, 0, 0, 0))
