import time

from PIL import Image, ImageDraw

from msu2_core import BOTH, LANDSCAPE, Field, Widget, get_font, register


@register
class HelloExternalWidget(Widget):
    name = "hello_external"
    interval = 1.0
    orientations = BOTH
    priority = 200
    actions = {"say": "打个招呼"}
    touch_help = "单击=打招呼，双击=重置计时"
    config_schema = [
        Field("title", "str", "标题", "来自外挂目录", help="显示在屏幕中间的文案"),
    ]

    def setup(self, ctx):
        ctx.state["_hello_start"] = time.monotonic()

    def on_enter(self, ctx):
        ctx.state["_hello_key"] = None

    def on_action(self, action, payload, ctx):
        if action == "say":
            ctx.state["_hello_msg"] = "你好，外部插件"
            ctx.state["_hello_key"] = None
            return {"ok": True, "msg": "已打招呼"}
        if action == "touch_single":
            ctx.state["_hello_msg"] = "单击收到"
            ctx.state["_hello_key"] = None
            return {"ok": True}
        if action == "touch_double":
            ctx.state["_hello_start"] = time.monotonic()
            ctx.state["_hello_msg"] = "计时已重置"
            ctx.state["_hello_key"] = None
            return {"ok": True}
        return {"ok": False, "err": "未知动作: %s" % action}

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        W, H = ctx.size
        cfg = self.get_config(ctx)
        title = str(cfg.get("title") or "来自外挂目录")
        secs = int(time.monotonic() - ctx.state.get("_hello_start", time.monotonic()))
        msg = ctx.state.get("_hello_msg") or "运行 %d 秒" % secs
        key = (ctx.orientation, W, H, title, msg)
        if ctx.state.get("_hello_key") == key:
            return None
        ctx.state["_hello_key"] = key
        img = Image.new("RGBA", (W, H), (16, 20, 28, 255))
        d = ImageDraw.Draw(img)
        if ctx.orientation == LANDSCAPE:
            d.text((W // 2, H * 35 // 100), title, fill="white",
                   font=get_font(15, bold=True, cjk=True), anchor="mm")
            d.text((W // 2, H * 68 // 100), msg, fill="#7ec8ff",
                   font=get_font(11, cjk=True), anchor="mm")
        else:
            d.text((W // 2, H * 30 // 100), title, fill="white",
                   font=get_font(13, bold=True, cjk=True), anchor="mm")
            d.text((W // 2, H * 55 // 100), msg, fill="#7ec8ff",
                   font=get_font(10, cjk=True), anchor="mm")
            d.text((W // 2, H * 78 // 100), "外部插件", fill="#8899aa",
                   font=get_font(9, cjk=True), anchor="mm")
        return img