import json
import logging
import threading
import time
import urllib.request
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.crypto")

_API = ("https://api.coingecko.com/api/v3/simple/price"
        "?ids={ids}&vs_currencies=usd&include_24hr_change=true")

class _Fetcher:
    def __init__(self, ids, interval=60):
        self.ids = ids
        self.interval = interval
        self.data = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                url = _API.format(ids=",".join(self.ids))
                with urllib.request.urlopen(url, timeout=8) as r:
                    self.data = json.loads(r.read())
            except Exception as e:
                log.debug("crypto fetch: %s", e)
            self._stop.wait(self.interval)

@register
class CryptoWidget(Widget):
    name = "crypto"
    interval = 1.0
    orientations = BOTH
    priority = 45

    config_schema = [
        Field("coins", "list_str", "币种 ID 列表",
              default=["bitcoin", "ethereum"],
              help="CoinGecko ID: bitcoin / ethereum / solana / dogecoin 等"),
    ]

    def setup(self, ctx):
        cfg = self.get_config(ctx)
        ids = tuple(cfg["coins"]) or ("bitcoin",)
        self._key = ids
        f = _Fetcher(list(ids))
        f.start()
        ctx.state["_crypto"] = {"fetcher": f, "key": ids}

    def teardown(self, ctx):
        d = ctx.state.get("_crypto")
        if d and d.get("fetcher"):
            d["fetcher"].stop()

    def render(self, ctx):
        cfg = self.get_config(ctx)
        ids = tuple(cfg["coins"]) or ("bitcoin",)

        cur = ctx.state.get("_crypto")
        if cur is None or cur.get("key") != ids:
            if cur and cur.get("fetcher"):
                cur["fetcher"].stop()
            f = _Fetcher(list(ids))
            f.start()
            ctx.state["_crypto"] = {"fetcher": f, "key": ids}
            cur = ctx.state["_crypto"]

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        data = cur["fetcher"].data
        items = []
        for cid in ids:
            info = data.get(cid, {})
            price = info.get("usd")
            change = info.get("usd_24h_change")
            items.append((cid, price, change))

        if not items:
            d.text((W // 2, H // 2), "loading…",
                   fill="#666666", font=get_font(14), anchor="mm")
            return img

        if ctx.orientation == LANDSCAPE:
            self._draw_list(d, W, H, items, per_row=2)
        else:
            self._draw_list(d, W, H, items, per_row=1)
        return img

    def _draw_list(self, d, W, H, items, per_row):
        cols = per_row
        rows = (len(items) + cols - 1) // cols
        cw, ch = W // cols, H // rows
        for i, (cid, price, change) in enumerate(items):
            x = (i % cols) * cw
            y = (i // cols) * ch
            self._draw_cell(d, x, y, cw, ch, cid, price, change)

    def _draw_cell(self, d, x, y, w, h, cid, price, change):
        f_name = get_font(10, cjk=False)
        f_price = get_font(15, bold=True, cjk=False)
        f_chg = get_font(10, cjk=False)

        short = cid.upper()[:6]
        d.text((x + 4, y + 2), short, fill="#909090", font=f_name)

        if price is None:
            d.text((x + 4, y + 16), "—", fill="#666666", font=f_price)
            return

        if price >= 1000:
            p_txt = f"${price:,.0f}"
        elif price >= 1:
            p_txt = f"${price:.2f}"
        else:
            p_txt = f"${price:.4f}"
        d.text((x + 4, y + 14), p_txt, fill="#e0e0e0", font=f_price)

        if change is not None:
            up = change >= 0
            color = "#22cc66" if up else "#ff5050"
            arrow = "▲" if up else "▼"
            d.text((x + w - 4, y + h - 11),
                   f"{arrow}{abs(change):.1f}%",
                   fill=color, font=f_chg, anchor="ra")
