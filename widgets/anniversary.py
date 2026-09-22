import datetime as _dt
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, Field, get_font

@register
class AnniversaryWidget(Widget):
    name = "anniversary"
    interval = 1.0
    orientations = BOTH
    priority = 25

    config_schema = [
        Field("title", "str", "标题", default="被霸道总裁爱上"),
        Field("date", "str", "开始日期", default="2020-01-01",
              help="格式: YYYY-MM-DD"),
        Field("unit", "select", "单位", default="天",
              options=["天", "周", "月"]),
    ]

    def render(self, ctx):
        cfg = self.get_config(ctx)
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        try:
            start = _dt.datetime.strptime(cfg["date"], "%Y-%m-%d").date()
        except Exception:
            d.text((W // 2, H // 2), "日期格式错误",
                   fill="#c00", font=get_font(12, cjk=True), anchor="mm")
            return img

        today = _dt.date.today()
        days = (today - start).days
        unit = cfg["unit"]
        if unit == "周":
            value = days // 7
        elif unit == "月":
            value = (today.year - start.year) * 12 + (today.month - start.month)
        else:
            value = days

        self._draw(d, W, H, cfg["title"], str(value), unit,
                   ctx.orientation == "portrait")
        return img

    def _draw(self, d, W, H, title, value_str, unit, is_portrait):
        f_t = get_font(13, cjk=True)
        f_u = get_font(13, cjk=True)

        max_w = W - 12
        f_n = self._fit_font(value_str, max_w, max_size=34)

        d.text((W // 2, H // 2 - 22), title,
               fill="#c0c0c0", font=f_t, anchor="mm")
        d.text((W // 2, H // 2 + 4), value_str,
               fill="#ff6b9d", font=f_n, anchor="mm")
        d.text((W // 2, H // 2 + 26), unit,
               fill="#909090", font=f_u, anchor="mm")

    @staticmethod
    def _fit_font(text, max_w, max_size=34):

        for size in range(max_size, 8, -2):
            f = get_font(size, bold=True, cjk=False)
            try:
                if f.getlength(text) <= max_w:
                    return f
            except Exception:
                pass
        return get_font(8, bold=True, cjk=False)
