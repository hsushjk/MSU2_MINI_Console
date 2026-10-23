import collections
import os
import re
import psutil
from PIL import Image, ImageDraw
from msu2_core import Widget, register, BOTH, LANDSCAPE, Field, get_font
from ._util import ellipsis, row_text, text_w

def _mount_options():
    opts = ["all"]
    seen = {"all"}
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        parts = []
    for p in parts:
        try:
            mp = str(getattr(p, "mountpoint", "") or "")
        except Exception:
            continue
        if mp and mp not in seen:
            seen.add(mp)
            opts.append(mp)
    return opts

@register
class DiskIOWidget(Widget):
    name = "disk_io"
    interval = 1.0
    orientations = BOTH
    priority = 35

    config_schema = [
        Field("disk", "select", "磁盘", default="all", options=["all"]),
        Field("history", "int", "折线保留点数", default=60, min=10, max=160),
    ]

    def setup(self, ctx):
        opts = _mount_options()
        saved = self._saved_disk(ctx, opts)
        self.config_schema = [
            Field("disk", "select", "磁盘", default=saved, options=list(opts)),
            Field("history", "int", "折线保留点数", default=60, min=10, max=160),
        ]
        self._opts = opts
        self._disks = opts[1:]
        self._map = self._build_map()
        maxlen = self._maxlen(ctx)
        ctx.state["_disk"] = {
            "last": None, "t": ctx.now, "sel": saved,
            "last_per": None, "rates": {},
            "r_hist": collections.deque(maxlen=maxlen),
            "w_hist": collections.deque(maxlen=maxlen),
        }

    def on_enter(self, ctx):
        ctx.state.pop("_disk_key", None)

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        cfg = self.get_config(ctx)
        sel = cfg.get("disk")
        opts = getattr(self, "_opts", None) or ["all"]
        if sel not in opts:
            sel = "all"
        maxlen = self._maxlen(ctx)

        st = ctx.state.get("_disk")
        if not isinstance(st, dict):
            st = {"last": None, "t": ctx.now, "sel": sel, "last_per": None,
                  "rates": {},
                  "r_hist": collections.deque(maxlen=maxlen),
                  "w_hist": collections.deque(maxlen=maxlen)}
            ctx.state["_disk"] = st

        r_hist = st.get("r_hist")
        w_hist = st.get("w_hist")
        if st.get("sel") != sel:
            st["sel"] = sel
            st["last"] = None
            st["last_per"] = None
            st["t"] = ctx.now
            st["r_hist"] = collections.deque(maxlen=maxlen)
            st["w_hist"] = collections.deque(maxlen=maxlen)
            r_hist = st["r_hist"]
            w_hist = st["w_hist"]
        if not isinstance(r_hist, collections.deque) or r_hist.maxlen != maxlen:
            st["r_hist"] = collections.deque(r_hist or (), maxlen=maxlen)
            r_hist = st["r_hist"]
        if not isinstance(w_hist, collections.deque) or w_hist.maxlen != maxlen:
            st["w_hist"] = collections.deque(w_hist or (), maxlen=maxlen)
            w_hist = st["w_hist"]

        try:
            now = float(ctx.now)
        except (TypeError, ValueError):
            now = 0.0
        try:
            g = psutil.disk_io_counters()
        except Exception:
            g = None
        try:
            per = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            per = {}

        dt = None
        if st.get("last") is not None:
            try:
                dt = max(0.001, now - float(st.get("t") or now))
            except (TypeError, ValueError):
                dt = 0.001

        snap = None
        if sel == "all":
            if g is not None:
                try:
                    snap = (int(g.read_bytes), int(g.write_bytes))
                except Exception:
                    snap = None
        else:
            c = self._counter_for(sel, per)
            if c is not None:
                try:
                    snap = (int(c.read_bytes), int(c.write_bytes))
                except Exception:
                    snap = None

        r_spd = w_spd = 0.0
        if snap is not None and dt is not None:
            try:
                prev = st.get("last")
                if isinstance(prev, (tuple, list)) and len(prev) >= 2:
                    r_spd = max(0.0, (snap[0] - int(prev[0])) / dt / 1024.0)
                    w_spd = max(0.0, (snap[1] - int(prev[1])) / dt / 1024.0)
            except Exception:
                r_spd = w_spd = 0.0
        if snap is not None:
            st["last"] = snap
            st["t"] = now

        rates = {}
        prev_per = st.get("last_per")
        if not isinstance(prev_per, dict):
            prev_per = {}
        cur_per = {}
        for mp in getattr(self, "_disks", None) or []:
            c = self._counter_for(mp, per)
            if c is None:
                continue
            try:
                cur = (int(c.read_bytes), int(c.write_bytes))
            except Exception:
                continue
            cur_per[mp] = cur
            rr = ww = 0.0
            pv = prev_per.get(mp)
            if isinstance(pv, (tuple, list)) and len(pv) >= 2 and dt:
                try:
                    rr = max(0.0, (cur[0] - int(pv[0])) / dt / 1024.0)
                    ww = max(0.0, (cur[1] - int(pv[1])) / dt / 1024.0)
                except Exception:
                    rr = ww = 0.0
            rates[mp] = (rr, ww)
        st["last_per"] = cur_per
        st["rates"] = rates

        r_hist.append(r_spd)
        w_hist.append(w_spd)

        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        bar_h = min(14, max(1, H // 4))
        self._draw_bar(d, 0, 0, W, bar_h, self._disk_label("R", sel), r_spd, "#22cc66")
        self._draw_bar(d, 0, bar_h, W, bar_h, self._disk_label("W", sel), w_spd, "#ff7a3d")

        ov_h = 0
        ov_y = 0
        if (ctx.orientation == LANDSCAPE and sel == "all"
                and len(getattr(self, "_disks", None) or []) >= 2
                and W >= 100 and H >= 60):
            ov_h = min(12, max(9, H // 7))
            ov_y = bar_h * 2 + 1
        chart_y0 = bar_h * 2 + 2
        if ov_h:
            chart_y0 = ov_y + ov_h + 1
        if ov_h and H - chart_y0 - 2 < 20:
            ov_h = 0
            chart_y0 = bar_h * 2 + 2
        chart_h = max(1, H - chart_y0 - 2)
        self._draw_chart(d, 0, chart_y0, W, chart_h, st)

        if ov_h:
            pieces = []
            for mp in (getattr(self, "_disks", None) or []):
                rw = rates.get(mp)
                if not rw:
                    continue
                pieces.append("%s %s/%s" % (self._short(mp),
                                            self._fmt_short(rw[0]),
                                            self._fmt_short(rw[1])))
            row_text(img, (4, ov_y + ov_h // 2), "  ".join(pieces),
                     get_font(8, cjk=False), "#888888", max(8, W - 8),
                     now, scroll=False)

        key = hash(img.tobytes())
        if ctx.state.get("_disk_key") == key:
            return None
        ctx.state["_disk_key"] = key
        return img

    def _counter_for(self, mp, per):
        if not per:
            return None
        cands = []
        try:
            m = getattr(self, "_map", None)
            if isinstance(m, dict):
                cands.append(m.get(mp))
        except Exception:
            pass
        cands.append(mp)
        for c in cands:
            if c and c in per:
                return per[c]
        return None

    def _build_map(self):
        out = {}
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        try:
            keys = set((psutil.disk_io_counters(perdisk=True) or {}).keys())
        except Exception:
            keys = set()
        for p in parts:
            try:
                mp = str(getattr(p, "mountpoint", "") or "")
                dev = str(getattr(p, "device", "") or "")
            except Exception:
                continue
            if not mp:
                continue
            if os.name == "nt":
                key = self._win_key(mp)
            else:
                key = self._dev_key(dev, keys)
            if key and (not keys or key in keys):
                out[mp] = key
        return out

    @staticmethod
    def _win_key(mp):
        if len(mp) < 2 or mp[1] != ":":
            return None
        path = "\\\\.\\" + mp[:2]
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                wintypes.HANDLE]
            k32.CreateFileW.restype = wintypes.HANDLE
            k32.DeviceIoControl.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            k32.DeviceIoControl.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.CreateFileW(path, 0, 7, None, 3, 0, None)
            if not h or h == wintypes.HANDLE(-1).value:
                return None

            class _Num(ctypes.Structure):
                _fields_ = [("DeviceType", wintypes.DWORD),
                            ("DeviceNumber", wintypes.DWORD),
                            ("PartitionNumber", wintypes.DWORD)]

            num = _Num()
            br = wintypes.DWORD(0)
            ok = k32.DeviceIoControl(h, 0x2D1080, None, 0,
                                     ctypes.byref(num), ctypes.sizeof(num),
                                     ctypes.byref(br), None)
            k32.CloseHandle(h)
            if not ok:
                return None
            return "PhysicalDrive%d" % int(num.DeviceNumber)
        except Exception:
            return None

    @staticmethod
    def _dev_key(dev, keys):
        try:
            base = str(dev or "").rstrip("/").split("/")[-1]
        except Exception:
            return None
        if not base:
            return None
        cands = [base, re.sub(r"p?\d+$", "", base)]
        for c in cands:
            if c and c in keys:
                return c
        return cands[1] if len(cands[1]) > 0 else None

    def _saved_disk(self, ctx, opts):
        try:
            raw = (ctx.config or {}).get("widget_config", {}).get(self.name, {})
        except Exception:
            raw = {}
        try:
            val = raw.get("disk") if isinstance(raw, dict) else None
        except Exception:
            val = None
        if isinstance(val, str) and val in opts:
            return val
        return "all"

    def _maxlen(self, ctx):
        try:
            v = int(float(self.get_config(ctx).get("history")))
        except (TypeError, ValueError):
            v = 60
        return max(10, min(160, v))

    def _disk_label(self, tag, sel):
        if sel == "all":
            return tag
        return "%s %s" % (tag, self._short(sel))

    @staticmethod
    def _short(mp):
        try:
            s = str(mp).rstrip("\\/")
            s = s.replace("/", "\\").split("\\")[-1]
        except Exception:
            s = ""
        if not s:
            return str(mp)[:4]
        return s[:6]

    def _draw_bar(self, d, x, y, w, h, label, kbs, color):
        f_lbl = get_font(10, cjk=False)
        txt = self._fmt(kbs)
        lw = text_w(f_lbl, label)
        avail = max(8, w - 8 - lw - 4)
        f_val = self._fit_val(txt, avail, 12)
        if text_w(f_val, txt) > avail:
            txt = ellipsis(f_val, txt, avail)
        d.text((x + 4, y + h // 2), label, fill="#909090",
               font=f_lbl, anchor="lm")
        d.text((x + w - 4, y + h // 2), txt, fill=color,
               font=f_val, anchor="rm")

    def _draw_chart(self, d, x, y, w, h, st):
        d.rectangle([x, y, max(x, x + w - 1), max(y, y + h - 1)],
                    fill="#0e0e12")
        try:
            r_hist = list(st.get("r_hist") or ())
            w_hist = list(st.get("w_hist") or ())
        except Exception:
            return
        if not r_hist or not w_hist:
            return
        peak = max(max(r_hist), max(w_hist), 1.0)

        def polyline(hist, color):
            n = len(hist)
            if n < 2:
                return
            pts = []
            step = w / max(1, n - 1)
            span = max(0, h - 16)
            for i, v in enumerate(hist):
                px = x + i * step
                py = y + h - 1 - (v / peak) * span
                pts.append((px, py))
            d.line(pts, fill=color, width=1)

        polyline(r_hist, "#22cc66")
        polyline(w_hist, "#ff7a3d")

        peak_txt = "peak %s" % self._fmt(peak)
        f_peak = self._fit_val(peak_txt, max(8, w - 6), 9)
        tw = text_w(f_peak, peak_txt)
        d.rectangle([max(x, x + 1), y,
                     max(x, min(x + w - 1, x + 4 + tw)),
                     max(y, min(y + h - 1, y + 11))],
                    fill="#0e0e12")
        d.text((x + 3, y + 1), peak_txt,
               fill="#606060", font=f_peak)

    @staticmethod
    def _fit_val(text, max_w, max_size):
        for size in range(max_size, 7, -2):
            f = get_font(size, bold=True, cjk=False)
            if text_w(f, text) <= max_w:
                return f
        return get_font(8, bold=True, cjk=False)

    @staticmethod
    def _fmt(kbs):
        try:
            kbs = max(0.0, float(kbs))
        except (TypeError, ValueError):
            return "0"
        if kbs != kbs:
            return "0"
        if kbs < 1:
            return "0"
        if kbs < 1024:
            return "%dK/s" % int(kbs)
        if kbs < 1024 * 1024:
            return "%.1fM/s" % (kbs / 1024.0)
        return "%.1fG/s" % (kbs / 1024.0 / 1024.0)

    @staticmethod
    def _fmt_short(kbs):
        try:
            kbs = max(0.0, float(kbs))
        except (TypeError, ValueError):
            return "0"
        if kbs != kbs or kbs < 1:
            return "0"
        if kbs < 1024:
            return "%dK" % int(kbs)
        if kbs < 1024 * 1024:
            return "%.1fM" % (kbs / 1024.0)
        return "%.1fG" % (kbs / 1024.0 / 1024.0)