import base64
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from io import BytesIO
from PIL import Image, ImageDraw

from msu2_core import Widget, register, BOTH, LANDSCAPE, get_font

log = logging.getLogger("msu2.widgets.music")

COVER_PX = 128
DEG_PER_SEC = 40

def _send_media_key(name):
    try:
        if sys.platform == "win32":
            import ctypes
            VK = {
                "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1,
                "stop": 0xB2, "volume_up": 0xAF, "volume_down": 0xAE,
                "mute": 0xAD,
            }
            vk = VK.get(name)
            if vk is None:
                return False
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            return True

        if sys.platform == "darwin":
            script = {
                "play_pause": 'tell application "Music" to playpause',
                "next": 'tell application "Music" to next track',
                "prev": 'tell application "Music" to previous track',
            }.get(name)
            if script is None:
                return False
            subprocess.run(["osascript", "-e", script],
                           check=False, timeout=2)
            return True

        if sys.platform.startswith("linux"):
            if name in ("play_pause", "next", "prev", "stop"):
                if shutil.which("playerctl"):
                    cmd = {
                        "play_pause": ["playerctl", "play-pause"],
                        "next":       ["playerctl", "next"],
                        "prev":       ["playerctl", "previous"],
                        "stop":       ["playerctl", "stop"],
                    }[name]
                    subprocess.run(cmd, check=False, timeout=2)
                    return True
                log.warning("未找到 playerctl，无法控制播放器")
                return False
    except Exception as e:
        log.debug("media key 失败: %s", e)
    return False

class _MediaSource:
    def __init__(self, interval=1.5):
        self._interval = interval
        self._data = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._ts = 0.0

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"{self.__class__.__name__}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self):
        while not self._stop.is_set():
            try:
                d = self._fetch()
                with self._lock:
                    self._data = d
                    self._ts = time.time()
            except Exception as e:
                log.debug("%s fetch 异常: %s", self.__class__.__name__, e)
            self._stop.wait(self._interval)

    def get(self):
        with self._lock:
            d = self._data
            ts = self._ts
        if not d:
            return None
        if d.get("playing"):
            d = dict(d)
            d["position"] = d.get("position", 0) + (time.time() - ts)
        return d

    def _fetch(self):
        raise NotImplementedError

_PS_SCRIPT = r'''
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    function Await($task, $type) {
        $asTask = $asTaskGeneric.MakeGenericMethod($type)
        $netTask = $asTask.Invoke($null, @($task))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }

    $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime]
    $mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    $session = $mgr.GetCurrentSession()
    if ($null -eq $session) { Write-Output ''; exit 0 }

    $props = Await ($session.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $tl = $session.GetTimelineProperties()
    $pb = $session.GetPlaybackInfo()

    $coverB64 = ''
    try {
        if ($null -ne $props.Thumbnail) {
            $stream = Await ($props.Thumbnail.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
            $size = [int]$stream.Size
            if ($size -gt 0 -and $size -lt 8MB) {
                $reader = [Windows.Storage.Streams.DataReader]::new($stream)
                Await ($reader.LoadAsync([uint32]$size)) ([uint32]) | Out-Null
                $bytes = New-Object byte[] $size
                $reader.ReadBytes($bytes)
                $reader.Dispose(); $stream.Dispose()
                $coverB64 = [Convert]::ToBase64String($bytes)
            }
        }
    } catch { $coverB64 = '' }

    $result = @{
        ok        = $true
        title     = [string]$props.Title
        artist    = [string]$props.Artist
        playing   = ([int]$pb.PlaybackStatus -eq 4)
        position  = [double]$tl.Position.TotalSeconds
        duration  = [double]$tl.EndTime.TotalSeconds
        cover_b64 = $coverB64
    }
    $json = $result | ConvertTo-Json -Compress
    $bytes2 = [System.Text.Encoding]::UTF8.GetBytes($json)
    Write-Output ([Convert]::ToBase64String($bytes2))
}
catch { Write-Output '' }
'''

def _find_powershell():
    for exe in ("pwsh.exe", "pwsh", "powershell.exe", "powershell"):
        p = shutil.which(exe)
        if p:
            return p
    return None

class _WindowsSMTC(_MediaSource):
    def __init__(self, interval=1.5):
        super().__init__(interval)
        self._ps = _find_powershell()
        self._script_b64 = base64.b64encode(
            _PS_SCRIPT.encode("utf-16-le")).decode("ascii")
        self._cf = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) \
            if sys.platform == "win32" else 0
        self._last_cover_hash = ""
        self._cached_cover_b64 = ""

    def _fetch(self):
        if not self._ps:
            return None
        try:
            proc = subprocess.run(
                [self._ps, "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", self._script_b64],
                capture_output=True, timeout=8, creationflags=self._cf)
        except Exception as e:
            log.debug("PowerShell: %s", e); return None
        if proc.returncode != 0:
            return None
        txt = proc.stdout.decode("ascii", "ignore").strip()
        txt = "".join(txt.split())
        if not txt:
            return None
        try:
            data = json.loads(base64.b64decode(txt).decode("utf-8"))
        except Exception as e:
            log.debug("SMTC 解码失败: %s", e); return None
        if not data.get("ok"):
            return None

        raw_b64 = str(data.get("cover_b64", "") or "")
        cover_b64 = ""
        if raw_b64:
            h = hashlib.md5(raw_b64.encode()).hexdigest()
            if h == self._last_cover_hash:
                cover_b64 = self._cached_cover_b64
            else:
                try:
                    img = Image.open(BytesIO(base64.b64decode(raw_b64))).convert("RGB")
                    img.thumbnail((COVER_PX, COVER_PX), Image.LANCZOS)
                    out = BytesIO()
                    img.save(out, format="PNG")
                    cover_b64 = base64.b64encode(out.getvalue()).decode("ascii")
                    self._cached_cover_b64 = cover_b64
                    self._last_cover_hash = h
                    log.info("封面已缓存 (%d bytes)", len(cover_b64))
                except Exception as e:
                    log.debug("封面处理失败: %s", e)
                    cover_b64 = ""; self._last_cover_hash = ""
        else:
            self._last_cover_hash = ""

        return {
            "title":     str(data.get("title", "") or ""),
            "artist":    str(data.get("artist", "") or ""),
            "playing":   bool(data.get("playing", False)),
            "position":  float(data.get("position", 0) or 0),
            "duration":  float(data.get("duration", 0) or 0),
            "cover_b64": cover_b64,
        }

class _LinuxMPRIS(_MediaSource):
    def __init__(self, interval=1.0):
        super().__init__(interval)
        self._dbus = None; self._bus = None; self._props = None
        self._cover_art_url = None
        self._cover_cache_b64 = ""

    def _init(self):
        import dbus
        self._dbus = dbus
        self._bus = dbus.SessionBus()

    def _find_player(self):
        try:
            for name in self._bus.list_names():
                if str(name).startswith("org.mpris.MediaPlayer2."):
                    obj = self._bus.get_object(name, "/org/mpris/MediaPlayer2")
                    self._props = self._dbus.Interface(
                        obj, "org.freedesktop.DBus.Properties")
                    return True
        except Exception:
            pass
        return False

    def _fetch(self):
        if self._bus is None:
            try:
                self._init()
            except ImportError:
                return None
        if self._props is None and not self._find_player():
            return None
        try:
            p = self._props
            meta = p.Get("org.mpris.MediaPlayer2.Player", "Metadata")
            status = str(p.Get("org.mpris.MediaPlayer2.Player",
                               "PlaybackStatus"))
            try:
                pos = int(p.Get("org.mpris.MediaPlayer2.Player",
                                "Position")) / 1e6
            except Exception:
                pos = 0.0
            dur = int(meta.get("mpris:length", 0)) / 1e6
            artists = meta.get("xesam:artist", [])
            artist = str(artists[0]) if artists else ""
            art_url = str(meta.get("mpris:artUrl", "") or "")

            if art_url != self._cover_art_url:
                self._cover_art_url = art_url
                self._cover_cache_b64 = self._load_cover(art_url)

            return {
                "title":     str(meta.get("xesam:title", "")),
                "artist":    artist,
                "playing":   status == "Playing",
                "position":  pos,
                "duration":  dur,
                "cover_b64": self._cover_cache_b64,
            }
        except Exception:
            self._props = None
            return None

    def _load_cover(self, url):
        if not url:
            return ""
        try:
            if url.startswith("file://"):
                path = url[7:]
                with open(path, "rb") as f:
                    raw = f.read()
            else:
                return ""
            img = Image.open(BytesIO(raw)).convert("RGB")
            img.thumbnail((COVER_PX, COVER_PX), Image.LANCZOS)
            out = BytesIO(); img.save(out, format="PNG")
            return base64.b64encode(out.getvalue()).decode("ascii")
        except Exception:
            return ""

class _NullSource(_MediaSource):
    def _fetch(self):
        return None

def _pick_source():
    if sys.platform == "win32":
        ps = _find_powershell()
        if ps:
            log.info("音乐插件: PowerShell SMTC (%s)", ps)
            return _WindowsSMTC()
        log.warning("未找到 PowerShell, 音乐插件不可用")
        return _NullSource()
    if sys.platform.startswith("linux"):
        try:
            import dbus
            log.info("音乐插件: D-Bus MPRIS")
            return _LinuxMPRIS()
        except ImportError:
            log.warning("未装 dbus-python, 音乐插件不可用")
            return _NullSource()
    return _NullSource()

@register
class MusicWidget(Widget):
    name = "music"
    interval = 0.2
    orientations = BOTH
    priority = 40

    def setup(self, ctx):
        src = _pick_source()
        src.start()
        ctx.state["_src"] = src
        ctx.state["_scroll"] = 0.0
        ctx.state["_cover"] = None
        ctx.state["_cover_hash"] = ""

    def teardown(self, ctx):
        src = ctx.state.get("_src")
        if src:
            src.stop()

    def on_action(self, action, payload, ctx):
        if action == "touch_single":
            _send_media_key("next")
            return {"ok": True}
        if action == "touch_double":
            _send_media_key("prev")
            return {"ok": True}
        if action == "touch_long":
            _send_media_key("play_pause")
            return {"ok": True}
        if action == "play_pause":
            _send_media_key("play_pause")
            return {"ok": True}
        if action == "next":
            _send_media_key("next")
            return {"ok": True}
        if action == "prev":
            _send_media_key("prev")
            return {"ok": True}
        return {"ok": False, "err": f"未知动作: {action}"}

    def _update_cover(self, ctx, info):
        b64 = info.get("cover_b64", "")
        if not b64:
            ctx.state["_cover"] = None
            ctx.state["_cover_hash"] = ""
            return
        h = hashlib.md5(b64.encode()).hexdigest()
        if h == ctx.state.get("_cover_hash"):
            return
        try:
            raw = base64.b64decode(b64)
            ctx.state["_cover"] = Image.open(BytesIO(raw)).convert("RGB")
            ctx.state["_cover_hash"] = h
        except Exception as e:
            log.debug("封面加载失败: %s", e)
            ctx.state["_cover"] = None
            ctx.state["_cover_hash"] = ""

    def render(self, ctx):
        W, H = ctx.size
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        src = ctx.state.get("_src")
        info = src.get() if src else None

        if not info or (not info.get("title") and not info.get("artist")):
            d.text((W // 2, H // 2), "No Media",
                   fill="#505050", font=get_font(14), anchor="mm")
            return img

        self._update_cover(ctx, info)
        cover = ctx.state.get("_cover")

        if info["playing"]:
            angle = (ctx.now * DEG_PER_SEC) % 360
        else:
            angle = 0.0

        if ctx.orientation == LANDSCAPE:
            self._draw_landscape(img, d, W, H, info, ctx, cover, angle)
        else:
            self._draw_portrait(img, d, W, H, info, ctx, cover, angle)
        return img

    def _draw_landscape(self, img, d, W, H, info, ctx, cover, angle):
        bar_h = 3
        disc_y = 4
        disc_sz = H - bar_h - 1 - disc_y - 2
        disc_x = 4
        self._draw_disc(img, disc_x, disc_y, disc_sz, cover, angle)

        tx = disc_x + disc_sz + 8
        tw = W - tx - 4

        f_t = get_font(13, bold=True, cjk=True)
        f_a = get_font(10, cjk=True)

        self._draw_play_icon(d, tx, 8, 7, info["playing"])

        title = info["title"] or info["artist"]
        artist = info["artist"] if info["title"] else ""
        text_x = tx + 11
        text_w = tw - 11
        scroll = self._maybe_scroll(ctx, title, f_t, text_w)
        self._draw_marquee_clipped(img, text_x, 5, title, f_t,
                                   (240, 240, 240, 255), text_w, scroll)
        if artist:
            d.text((tx, 24), self._clip(artist, f_a, tw),
                   fill="#888888", font=f_a)

        self._draw_progress(d, W, H, info, bar_h)

    def _draw_portrait(self, img, d, W, H, info, ctx, cover, angle):
        bar_h = 3
        disc_sz = 62
        disc_x = (W - disc_sz) // 2
        disc_y = 8
        self._draw_disc(img, disc_x, disc_y, disc_sz, cover, angle)

        f_t = get_font(13, bold=True, cjk=True)
        f_a = get_font(10, cjk=True)

        self._draw_play_icon(d, W // 2 - 4, disc_y + disc_sz + 8, 7,
                             info["playing"])

        title = info["title"] or info["artist"]
        lines = self._wrap(title, f_t, W - 8, max_lines=2)
        y = disc_y + disc_sz + 24
        for line in lines:
            d.text((W // 2, y), line, fill="#f0f0f0",
                   font=f_t, anchor="mm")
            y += 15

        if info["title"] and info["artist"]:
            a = self._clip(info["artist"], f_a, W - 8)
            if a:
                d.text((W // 2, y + 2), a, fill="#888888",
                       font=f_a, anchor="mm")

        self._draw_progress(d, W, H, info, bar_h)

    def _draw_disc(self, base_img, x, y, size, cover, angle):
        SS = 2
        S = size * SS
        cx = cy = S // 2

        disc = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(disc)

        d.ellipse([0, 0, S - 1, S - 1], fill="#15171b")

        for ratio in (0.90, 0.84, 0.78):
            r = int(S / 2 * ratio)
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      outline="#252a32", width=max(1, S // 128))

        inner_r = int(S * 0.31)
        if cover is not None:
            w, h = cover.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            sq = cover.crop((left, top, left + side, top + side))
            sq = sq.resize((inner_r * 2, inner_r * 2), Image.LANCZOS)

            mask = Image.new("L", (S, S), 0)
            ImageDraw.Draw(mask).ellipse(
                [cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
                fill=255)
            tmp = Image.new("RGB", (S, S), "#15171b")
            tmp.paste(sq, (cx - inner_r, cy - inner_r))
            disc.paste(tmp, (0, 0), mask)
        else:
            d.ellipse([cx - inner_r, cy - inner_r,
                       cx + inner_r, cy + inner_r],
                      fill="#2b5fd9")
            try:
                f = get_font(int(inner_r * 1.15), cjk=False)
                d.text((cx, cy + 1), "♪", fill="#ffffff",
                       font=f, anchor="mm")
            except Exception:
                pass

        d.ellipse([cx - inner_r, cy - inner_r,
                   cx + inner_r, cy + inner_r],
                  outline="#4a7fe0", width=max(1, S // 90))

        if angle:
            disc = disc.rotate(-angle, resample=Image.BICUBIC,
                               center=(cx, cy))
        disc = disc.resize((size, size), Image.LANCZOS)
        base_img.paste(disc, (x, y), disc)

    def _draw_play_icon(self, d, x, y, size, playing):
        color = "#4a7fe0"
        if playing:
            bw = max(2, size // 3)
            d.rectangle([x, y, x + bw, y + size], fill=color)
            d.rectangle([x + size - bw, y, x + size, y + size], fill=color)
        else:
            d.polygon([(x, y), (x, y + size), (x + size, y + size // 2)],
                      fill=color)

    def _draw_progress(self, d, W, H, info, bar_h):
        dur = info.get("duration", 0) or 0
        pos = info.get("position", 0) or 0
        y = H - bar_h - 1
        d.rectangle([0, y, W, y + bar_h], fill="#1c1f24")
        if dur > 0:
            r = max(0.0, min(1.0, pos / dur))
            fw = int(W * r)
            if fw > 0:
                d.rectangle([0, y, fw, y + bar_h], fill="#4a7fe0")

    def _draw_marquee_clipped(self, base_img, x, y, text, font, color,
                              max_w, offset):
        tw = font.getlength(text)
        if tw <= max_w:
            ImageDraw.Draw(base_img).text((x, y), text, fill=color, font=font)
            return
        gap = 40
        total = tw + gap
        off = int(offset) % total
        bbox = font.getbbox(text)
        text_h = bbox[3] - bbox[1] + 4
        canvas_w = int(total) + 4
        canvas = Image.new("RGBA", (canvas_w, text_h), (0, 0, 0, 0))
        cd = ImageDraw.Draw(canvas)
        cd.text((0, -bbox[1]), text, fill=color, font=font)
        cd.text((total, -bbox[1]), text, fill=color, font=font)
        crop = canvas.crop((int(off), 0, int(off) + int(max_w), text_h))
        base_img.paste(crop, (int(x), int(y)), crop)

    def _clip(self, text, font, max_w):
        if not text: return ""
        if font.getlength(text) <= max_w: return text
        ell = "…"; ew = font.getlength(ell); out = ""
        for ch in text:
            if font.getlength(out + ch) + ew > max_w: break
            out += ch
        return out + ell

    def _wrap(self, text, font, max_w, max_lines=3):
        if not text: return [""]
        lines, cur = [], ""
        for ch in text:
            if font.getlength(cur + ch) <= max_w:
                cur += ch
            else:
                lines.append(cur); cur = ch
                if len(lines) >= max_lines: break
        if cur and len(lines) < max_lines: lines.append(cur)
        return lines

    def _maybe_scroll(self, ctx, text, font, max_w):
        if font.getlength(text) <= max_w:
            ctx.state["_scroll"] = 0.0; return 0.0
        ctx.state["_scroll"] = ctx.state.get("_scroll", 0.0) + 1.0
        return ctx.state["_scroll"]
