import base64
import json
import locale
import logging
import shutil
import subprocess
import sys
import threading
import time
from PIL import Image, ImageDraw

from msu2_core import Widget, register, BOTH, LANDSCAPE, get_font
from ._util import ellipsis, row_text, text_w

log = logging.getLogger("msu2.widgets.music")

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

_PS_PRELUDE = r'''
$ProgressPreference = 'SilentlyContinue'
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
function Get-SmtcSession {
    $mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
    return $mgr.GetCurrentSession()
}
'''

_PS_SCRIPT = r'''
try {
''' + _PS_PRELUDE + r'''
    $session = Get-SmtcSession
    if ($null -eq $session) { Write-Output ''; exit 0 }

    $props = Await ($session.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $tl = $session.GetTimelineProperties()
    $pb = $session.GetPlaybackInfo()

    $result = @{
        ok         = $true
        title      = [string]$props.Title
        artist     = [string]$props.Artist
        playing    = ([int]$pb.PlaybackStatus -eq 4)
        position   = [double]$tl.Position.TotalSeconds
        duration   = [double]$tl.EndTime.TotalSeconds
    }
    $json = $result | ConvertTo-Json -Compress
    $bytes2 = [System.Text.Encoding]::UTF8.GetBytes($json)
    Write-Output ([Convert]::ToBase64String($bytes2))
}
catch {
    [Console]::Error.WriteLine('smtc: ' + $_.Exception.Message)
    Write-Output ''
}
'''

def _find_powershell():
    for exe in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
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
        self._empty = 0
        self._last_err = ""

    def _run_ps(self, script_b64):
        proc = subprocess.run(
            [self._ps, "-NoLogo", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", script_b64],
            capture_output=True, timeout=10, creationflags=self._cf)
        out = proc.stdout.decode("ascii", "ignore").strip()
        out = "".join(out.split())
        err = self._decode_err(proc.stderr)
        return proc.returncode, out, err

    def _decode_err(self, raw):
        text = ""
        for enc in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = raw.decode("utf-8", "replace")
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line == "#< CLIXML" or line.startswith("<Objs"):
                continue
            lines.append(line)
        return "\n".join(lines).strip()[-500:]

    def _fail(self, err=""):
        self._empty += 1
        if err:
            self._last_err = err[-300:]
        if self._empty == 3:
            log.warning("SMTC 连续 %d 次无结果 (PowerShell: %s): %s",
                        self._empty, self._ps, self._last_err or "无 stderr")
        return None

    def _fetch(self):
        if not self._ps:
            return None
        try:
            code, out, err = self._run_ps(self._script_b64)
        except Exception as e:
            log.debug("PowerShell: %s", e)
            return self._fail(str(e))
        if code != 0:
            return self._fail(err)
        if not out:
            return self._fail(err)
        try:
            data = json.loads(base64.b64decode(out).decode("utf-8"))
        except Exception as e:
            log.debug("SMTC 解码失败: %s", e)
            return self._fail(str(e))
        if not data.get("ok"):
            return self._fail("")

        self._empty = 0
        self._last_err = ""

        return {
            "title":     str(data.get("title", "") or ""),
            "artist":    str(data.get("artist", "") or ""),
            "playing":   bool(data.get("playing", False)),
            "position":  float(data.get("position", 0) or 0),
            "duration":  float(data.get("duration", 0) or 0),
        }

class _LinuxMPRIS(_MediaSource):
    def __init__(self, interval=1.0):
        super().__init__(interval)
        self._dbus = None; self._bus = None; self._props = None

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
            if isinstance(artists, (str, bytes)):
                artist = str(artists)
            elif artists:
                artist = str(artists[0])
            else:
                artist = ""

            return {
                "title":     str(meta.get("xesam:title", "")),
                "artist":    artist,
                "playing":   status == "Playing",
                "position":  pos,
                "duration":  dur,
            }
        except Exception:
            self._props = None
            return None

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
    actions = {
        "play_pause": "播放/暂停",
        "next": "下一首",
        "prev": "上一首",
    }

    def setup(self, ctx):
        old = ctx.state.get("_src")
        if old:
            old.stop()
        src = _pick_source()
        src.start()
        ctx.state["_src"] = src
        ctx.state["_disc"] = None
        ctx.state["_disc_key"] = None
        ctx.state["_last_key"] = None

    def teardown(self, ctx):
        src = ctx.state.get("_src")
        if src:
            src.stop()
        ctx.state.pop("_src", None)
        ctx.state.pop("_disc", None)

    def on_enter(self, ctx):
        ctx.state["_last_key"] = None

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

    def render(self, ctx):
        try:
            return self._render(ctx)
        except Exception:
            return None

    def _render(self, ctx):
        W, H = ctx.size
        src = ctx.state.get("_src")
        info = src.get() if src else None

        if not info or (not info.get("title") and not info.get("artist")):
            key = f"nomedia|{W}x{H}|{ctx.orientation}"
            if ctx.state.get("_last_key") == key:
                return None
            ctx.state["_last_key"] = key
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((W // 2, H // 2), "No Media",
                                     fill="#505050", font=get_font(14),
                                     anchor="mm")
            return img

        title, artist = self._texts(info)
        playing = bool(info.get("playing"))
        try:
            pos = int(float(info.get("position", 0) or 0))
        except (TypeError, ValueError):
            pos = 0
        try:
            dur = int(float(info.get("duration", 0) or 0))
        except (TypeError, ValueError):
            dur = 0
        angle = (ctx.now * DEG_PER_SEC) % 360 if playing else 0.0

        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if ctx.orientation == LANDSCAPE:
            scrolling = self._draw_landscape(img, d, W, H, info, ctx,
                                            angle)
        else:
            scrolling = self._draw_portrait(img, d, W, H, info, ctx,
                                           angle)

        tb = int(ctx.now * 5) if (playing or scrolling) else 0
        key = (W, H, ctx.orientation, playing, tb, pos, dur, title, artist)
        if ctx.state.get("_last_key") == key:
            return None
        ctx.state["_last_key"] = key
        return img

    @staticmethod
    def _texts(info):
        title = str(info.get("title") or "").strip()
        artist = str(info.get("artist") or "").strip()
        if not title:
            title, artist = artist, ""
        return title, artist

    @staticmethod
    def _text_height(font):
        try:
            b = font.getbbox("Ay")
            return max(1, int(b[3] - b[1]))
        except Exception:
            return int(getattr(font, "size", 12)) + 2

    def _draw_landscape(self, img, d, W, H, info, ctx, angle):
        bar_h = 3
        disc_y = 4
        disc_x = 4
        disc_sz = max(16, H - bar_h - 7)
        self._draw_disc(img, disc_x, disc_y, disc_sz, angle, ctx)

        tx = disc_x + disc_sz + 8
        tag = 2
        bottom = H - bar_h - tag
        f_t = get_font(12, bold=True, cjk=True)
        f_a = get_font(9, cjk=True)
        title, artist = self._texts(info)
        title_h = self._text_height(f_t)
        artist_h = self._text_height(f_a)
        gap = 3
        icon = 7
        text_x = tx + icon + 4
        tw = max(12, W - text_x - 4)
        block = title_h + (gap + artist_h if artist else 0)
        avail_h = max(1, bottom - tag)
        ty = tag + max(0, (avail_h - block) // 2) + title_h // 2

        self._draw_icon(d, tx, ty, icon, bool(info.get("playing")))
        row_text(img, (text_x, ty), title, f_t, "#f0f0f0", tw,
                 ctx.now, scroll=True)
        scrolling = text_w(f_t, title) > tw
        if artist:
            ay = ty + title_h // 2 + gap + artist_h // 2
            row_text(img, (text_x, ay), artist, f_a, "#888888", tw,
                     ctx.now, scroll=False)

        self._draw_progress(d, W, H, info, bar_h)
        return scrolling

    def _draw_portrait(self, img, d, W, H, info, ctx, angle):
        bar_h = 3
        disc_sz = min(62, max(24, W - 8))
        disc_x = (W - disc_sz) // 2
        disc_y = 8
        self._draw_disc(img, disc_x, disc_y, disc_sz, angle, ctx)

        f_t = get_font(13, bold=True, cjk=True)
        f_a = get_font(10, cjk=True)
        title, artist = self._texts(info)
        icon = 7
        icon_cy = disc_y + disc_sz + 11
        self._draw_icon(d, W // 2 - icon // 2, icon_cy, icon,
                        bool(info.get("playing")))

        title_h = self._text_height(f_t)
        lines = self._wrap(title, f_t, W - 8, max_lines=2)
        y = icon_cy + 13
        for line in lines:
            d.text((W // 2, y), line, fill="#f0f0f0", font=f_t,
                   anchor="mm")
            y += title_h + 1

        if artist:
            a = ellipsis(f_a, artist, W - 8)
            if a:
                d.text((W // 2, y + 1), a, fill="#888888", font=f_a,
                       anchor="mm")

        self._draw_progress(d, W, H, info, bar_h)
        return False

    def _draw_disc(self, base_img, x, y, size, angle, ctx):
        SS = 2
        S = size * SS
        base = self._base_disc(ctx, size, S)
        if angle:
            disc = base.rotate(-angle, resample=Image.BICUBIC,
                               center=(S // 2, S // 2))
        else:
            disc = base
        disc = disc.resize((size, size), Image.LANCZOS)
        base_img.paste(disc, (x, y), disc)

    def _base_disc(self, ctx, size, S):
        key = size
        cached = ctx.state.get("_disc")
        if cached is not None and ctx.state.get("_disc_key") == key:
            return cached

        cx = cy = S // 2

        disc = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(disc)

        d.ellipse([0, 0, S - 1, S - 1], fill="#15171b")

        for ratio in (0.90, 0.84, 0.78):
            r = int(S / 2 * ratio)
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      outline="#252a32", width=max(1, S // 128))

        inner_r = int(S * 0.31)
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

        ctx.state["_disc"] = disc
        ctx.state["_disc_key"] = key
        return disc

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

    def _draw_icon(self, d, x, cy, size, playing):
        self._draw_play_icon(d, x, cy - size // 2, size, playing)

    def _wrap(self, text, font, max_w, max_lines=3):
        if not text: return [""]
        lines, cur, width = [], [], 0.0
        for ch in text:
            try:
                cw = font.getlength(ch)
            except Exception:
                cw = 8.0
            if width + cw <= max_w:
                cur.append(ch); width += cw
            else:
                lines.append("".join(cur)); cur = [ch]; width = cw
                if len(lines) >= max_lines: break
        if cur and len(lines) < max_lines: lines.append("".join(cur))
        return lines
