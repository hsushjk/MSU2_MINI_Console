import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font

log = logging.getLogger("msu2.widgets.crypto")

_API = "https://api.coingecko.com/api/v3/simple/price"
_REFRESH_DEFAULT = 60.0
_REFRESH_MIN = 10.0
_REFRESH_MAX = 3600.0
_CYCLE_DEFAULT = 10.0
_CYCLE_MIN = 2.0
_CYCLE_MAX = 3600.0


def _clamp_float(value, default, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return max(lo, min(hi, v))


def _build_url(ids):
    query = urllib.parse.urlencode(
        {
            "ids": ",".join(ids),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }
    )
    return f"{_API}?{query}"


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


class _Fetcher:
    def __init__(self, ids, interval=60):
        self.ids = list(ids)
        self.interval = _clamp_float(interval, _REFRESH_DEFAULT, _REFRESH_MIN, _REFRESH_MAX)
        self.data = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="CryptoFetcher"
        )

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def refresh(self):
        if self._stop.is_set():
            return
        self._wake.set()

    def get_data(self):
        with self._lock:
            data = self.data
        return data if isinstance(data, dict) else {}

    def _loop(self):
        fails = 0
        while not self._stop.is_set():
            try:
                url = _build_url(self.ids)
                with urllib.request.urlopen(url, timeout=8) as r:
                    data = json.loads(r.read())
                if not isinstance(data, dict):
                    raise ValueError("bad payload")
                with self._lock:
                    self.data = data
                fails = 0
                wait = self.interval
            except Exception as e:
                fails += 1
                backoff = self.interval * (2 ** min(fails, 4))
                cap = max(300.0, self.interval)
                wait = max(self.interval, min(backoff, cap))
                log.debug("crypto fetch: %s", e)
            self._wake.wait(wait)
            self._wake.clear()


@register
class CryptoWidget(Widget):
    name = "crypto"
    interval = 1.0
    orientations = BOTH
    priority = 45

    config_schema = [
        Field(
            "coins",
            "list_str",
            "币种 ID 列表",
            default=["bitcoin", "ethereum"],
            help="CoinGecko ID: bitcoin / ethereum / solana / dogecoin 等",
        ),
        Field(
            "refresh_sec",
            "float",
            "刷新周期(秒)",
            default=_REFRESH_DEFAULT,
            min=_REFRESH_MIN,
            max=_REFRESH_MAX,
            step=5.0,
        ),
        Field("cycle", "bool", "循环浏览", default=False),
        Field(
            "cycle_interval",
            "float",
            "切换周期(秒)",
            default=_CYCLE_DEFAULT,
            min=_CYCLE_MIN,
            max=_CYCLE_MAX,
            step=1.0,
        ),
    ]

    actions = {
        "next": "下一个",
        "prev": "上一个",
    }
    touch_help = "单击=下一个，双击=上一个"

    def setup(self, ctx):
        cfg = self.get_config(ctx)
        ids = self._ids(cfg)
        interval = _clamp_float(
            cfg.get("refresh_sec"), _REFRESH_DEFAULT, _REFRESH_MIN, _REFRESH_MAX
        )
        old = ctx.state.get("_crypto")
        if isinstance(old, dict) and old.get("fetcher") is not None:
            try:
                old["fetcher"].stop()
            except Exception:
                pass
        fetcher = _Fetcher(ids, interval)
        fetcher.start()
        ctx.state["_crypto"] = {
            "fetcher": fetcher,
            "key": ids,
            "interval": interval,
            "offset": 0,
            "last_key": None,
            "_auto_ts": time.monotonic(),
        }

    def teardown(self, ctx):
        st = ctx.state.get("_crypto")
        if isinstance(st, dict) and st.get("fetcher") is not None:
            try:
                st["fetcher"].stop()
            except Exception:
                pass
            st["fetcher"] = None

    def on_enter(self, ctx):
        st = ctx.state.get("_crypto")
        if isinstance(st, dict):
            st["last_key"] = None
            st["_auto_ts"] = time.monotonic()
            fetcher = st.get("fetcher")
            if fetcher is not None:
                try:
                    fetcher.refresh()
                except Exception:
                    pass

    def on_leave(self, ctx):
        pass

    def on_action(self, action, payload, ctx):
        try:
            st = ctx.state.get("_crypto")
            if not isinstance(st, dict):
                return {"ok": False, "err": "未就绪"}
            if action in ("next", "touch_single"):
                delta = 1
            elif action in ("prev", "touch_double"):
                delta = -1
            else:
                return {"ok": False, "err": "未知动作: " + str(action)}
            ids = tuple(st.get("key") or ())
            pages = self._pages(ctx, ids)
            st["offset"] = (int(st.get("offset", 0)) + delta) % pages
            st["_auto_ts"] = time.monotonic()
            st["last_key"] = None
            return {"ok": True}
        except Exception as exc:
            log.warning("crypto 动作 %s 失败: %s", action, exc)
            return {"ok": False, "err": "动作失败"}

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception as exc:
            log.warning("crypto 渲染失败: %s", exc)
            return None

    def _render(self, ctx):
        cfg = self.get_config(ctx)
        ids = self._ids(cfg)
        interval = _clamp_float(
            cfg.get("refresh_sec"), _REFRESH_DEFAULT, _REFRESH_MIN, _REFRESH_MAX
        )

        st = ctx.state.get("_crypto")
        if not isinstance(st, dict):
            st = {
                "fetcher": None,
                "key": None,
                "interval": interval,
                "offset": 0,
                "last_key": None,
                "_auto_ts": time.monotonic(),
            }
            ctx.state["_crypto"] = st

        fetcher = st.get("fetcher")
        if (
            fetcher is None
            or st.get("key") != ids
            or st.get("interval") != interval
        ):
            if fetcher is not None:
                try:
                    threading.Thread(
                        target=fetcher.stop, daemon=True, name="CryptoStop"
                    ).start()
                except Exception:
                    pass
            fetcher = _Fetcher(ids, interval)
            fetcher.start()
            st["fetcher"] = fetcher
            st["key"] = ids
            st["interval"] = interval
            st["last_key"] = None

        data = fetcher.get_data()

        W, H = ctx.size
        cols = 2 if ctx.orientation == LANDSCAPE else 1
        rows = max(1, H // 44)
        per = max(1, cols * rows)
        total = len(ids)
        pages = max(1, (total + per - 1) // per)

        cycle_interval = _clamp_float(
            cfg.get("cycle_interval"), _CYCLE_DEFAULT, _CYCLE_MIN, _CYCLE_MAX
        )
        if bool(cfg.get("cycle")) and pages > 1:
            mono = time.monotonic()
            ts = float(st.get("_auto_ts") or 0.0)
            if ts <= 0.0:
                ts = mono
                st["_auto_ts"] = ts
            elapsed = mono - ts
            if elapsed >= cycle_interval:
                steps = int(elapsed // cycle_interval)
                st["offset"] = (int(st.get("offset", 0)) + steps) % pages
                st["_auto_ts"] = ts + steps * cycle_interval
                st["last_key"] = None
        else:
            st["_auto_ts"] = time.monotonic()

        offset = int(st.get("offset", 0)) % pages
        st["offset"] = offset
        page_ids = ids[offset * per : (offset + 1) * per]

        items = []
        for cid in page_ids:
            info = data.get(cid) if isinstance(data, dict) else None
            if not isinstance(info, dict):
                info = {}
            items.append(
                (cid, _num(info.get("usd")), _num(info.get("usd_24h_change")))
            )

        key = (ctx.orientation, tuple(items), offset, pages, bool(data))
        if st.get("last_key") == key:
            return None

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        if not data:
            d.text(
                (W // 2, H // 2),
                "loading…",
                fill="#666666",
                font=get_font(14),
                anchor="mm",
            )
        else:
            self._draw_list(d, W, H, items, per_row=cols)

        if pages > 1:
            d.text(
                (W - 3, 1),
                "%d/%d" % (offset + 1, pages),
                fill="#606060",
                font=get_font(8, cjk=False),
                anchor="ra",
            )
        st["last_key"] = key
        return img

    def _pages(self, ctx, ids):
        try:
            W, H = ctx.size
        except Exception:
            W, H = 160, 80
        cols = 2 if ctx.orientation == LANDSCAPE else 1
        rows = max(1, H // 44)
        per = max(1, cols * rows)
        total = len(ids)
        return max(1, (total + per - 1) // per)

    def _draw_list(self, d, W, H, items, per_row):
        cols = per_row
        rows = max(1, (len(items) + cols - 1) // cols)
        cw, ch = W // cols, max(1, H // rows)
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

        if change is not None and h >= 40:
            up = change >= 0
            color = "#22cc66" if up else "#ff5050"
            arrow = "▲" if up else "▼"
            d.text(
                (x + w - 4, y + h - 11),
                f"{arrow}{abs(change):.1f}%",
                fill=color,
                font=f_chg,
                anchor="ra",
            )

    @staticmethod
    def _ids(cfg):
        raw = cfg.get("coins")
        out = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                text = str(item or "").strip()
                if text:
                    out.append(text)
        if not out:
            out = ["bitcoin"]
        return tuple(out[:20])