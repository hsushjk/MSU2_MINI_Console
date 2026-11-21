from __future__ import annotations

import argparse
import collections
import datetime
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if getattr(sys, "frozen", False):
    _APP_DIR = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    _APP_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_HERE = str(_APP_DIR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

def _console(text):
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    _console("缺少 pyserial: pip install pyserial"); sys.exit(1)

try:
    from PIL import Image, ImageDraw
except ImportError:
    _console("缺少 Pillow: pip install pillow"); sys.exit(1)

try:
    import psutil
except ImportError:
    _console("缺少 psutil: pip install psutil"); sys.exit(1)

try:
    import numpy as _np
except Exception:
    _np = None

try:
    from msu2_core import (
        WIDGET_REGISTRY, RenderContext, Widget,
        LANDSCAPE, PORTRAIT, LANDSCAPE_SIZE, PORTRAIT_SIZE,
        probe_fonts, set_user_font, get_font,
        merge_config,
    )
    import widgets as _widgets_pkg
except ImportError as e:
    _console(f"无法导入 msu2_core/widgets: {e}"); sys.exit(1)

SCREEN_W, SCREEN_H = LANDSCAPE_SIZE
SCREEN_BAUD = 19200
MANUAL_COOLDOWN = 0.8
HEARTBEAT_INTERVAL = 2.0

TOUCH_CH = 9
TOUCH_SAMPLE_INTERVAL = 0.06
TOUCH_LONG_PRESS = 0.8
TOUCH_DOUBLE_WINDOW = 0.35
TOUCH_DEFAULT_THRESHOLD = 60

SCREENSHOT_DIR = _APP_DIR / "screenshots"
DEFAULT_GIF_DIR = _APP_DIR / "assets" / "gif"
MAX_REQUEST_BYTES = 32 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
IMAGE_EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp")
TEXT_EXTS = (".md", ".txt")

log = logging.getLogger("msu2")

class Cmd:
    SFR = 0x00; LCD = 0x02; FLASH = 0x03; BULK = 0x04; ADC = 0x08
    LCD_SET_XY = 0x00; LCD_SET_SIZE = 0x01; LCD_SET_COLOR = 0x02
    LCD_DRAW = 0x03
    DRAW_LOAD_ADDR = 0x07; DRAW_DATA = 0x08
    DRAW_DIRECTION = 0x0A
    FILL_COLOR = 0x04

class MiniProtocol:
    @staticmethod
    def _frame(*vals):
        return bytes(v & 0xFF for v in vals)

    @staticmethod
    def lcd_set_xy(x, y):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_SET_XY,
                                   (x >> 8) & 0xFF, x & 0xFF,
                                   (y >> 8) & 0xFF, y & 0xFF)

    @staticmethod
    def lcd_set_size(w, h):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_SET_SIZE,
                                   (w >> 8) & 0xFF, w & 0xFF,
                                   (h >> 8) & 0xFF, h & 0xFF)

    @staticmethod
    def lcd_enter_direct_mode():
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_LOAD_ADDR, 0, 0, 0)

    @staticmethod
    def lcd_set_fill(b0, b1, b2, b3):
        return MiniProtocol._frame(Cmd.LCD, Cmd.FILL_COLOR, b0, b1, b2, b3)

    @staticmethod
    def lcd_data_block(idx, b0, b1, b2, b3):
        return MiniProtocol._frame(Cmd.BULK, idx, b0, b1, b2, b3)

    @staticmethod
    def lcd_segment_end_full():
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_DATA, 0x01, 0x00, 0x00)

    @staticmethod
    def lcd_segment_end_tail(tail):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_DATA, 0x00, tail & 0xFF, 0x00)

    @staticmethod
    def lcd_direction(reverse):
        return MiniProtocol._frame(Cmd.LCD, Cmd.LCD_DRAW,
                                   Cmd.DRAW_DIRECTION, reverse & 0x01, 0, 0)

    @staticmethod
    def read_adc(ch):
        return MiniProtocol._frame(Cmd.ADC, ch, 0, 0, 0, 0)

class SerialTransport:
    def __init__(self, port, baud=SCREEN_BAUD, timeout=0.05):
        self.port = port; self.baud = baud; self.timeout = timeout
        self._ser = None
        self._lock = threading.RLock()
        self._connected = False
        self._handshaked = False
        self.last_error = None
        self.bytes_written = 0

    def open(self):
        with self._lock:
            if self._connected and self._handshaked:
                return True
            if self._ser is None:
                try:
                    self._ser = serial.Serial(self.port, self.baud,
                                              timeout=self.timeout)
                except PermissionError:
                    if sys.platform == "win32":
                        self.last_error = f"{self.port} 被其它程序占用"
                    else:
                        self.last_error = (
                            f"{self.port} 权限不足，"
                            f"试试 sudo usermod -aG dialout $USER")
                    return False
                except FileNotFoundError:
                    self.last_error = f"{self.port} 暂时不存在"
                    return False
                except Exception as e:
                    self.last_error = f"打开 {self.port} 失败: {e}"
                    return False
            self._connected = True
            if not self._handshaked and not self.handshake():
                self.close(); return False
            log.info("串口已打开: %s @ %d", self.port, self.baud)
            return True

    def close(self):
        with self._lock:
            if self._ser:
                try: self._ser.close()
                except Exception: pass
            self._ser = None
            self._connected = False; self._handshaked = False

    @property
    def connected(self):
        return self._connected and self._handshaked

    def write(self, data):
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                self._ser.write(data)
                self.bytes_written += len(data)
                return True
            except Exception as e:
                log.warning("写入失败: %s", e)
                self._connected = False; self._handshaked = False
                return False

    def read_all(self):
        with self._lock:
            if not self._connected or not self._ser:
                return b""
            try:
                n = self._ser.in_waiting
                return self._ser.read(n) if n else b""
            except Exception:
                self._connected = False; self._handshaked = False
                return b""

    def read_adc(self, ch, timeout=0.05):
        with self._lock:
            if not self._connected or not self._ser:
                return None
            t0 = time.monotonic()
            buf = bytearray()
            while time.monotonic() - t0 < timeout:
                try:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                except Exception:
                    self._connected = False; self._handshaked = False
                    return None
                while len(buf) >= 6:
                    if buf[0] == Cmd.ADC and buf[1] == ch:
                        val = (buf[4] << 8) | buf[5]
                        del buf[:6]
                        return val
                    del buf[0]
                time.sleep(0.002)
            return None

    def handshake(self, timeout=2.0):
        if self._handshaked:
            return True
        t0 = time.monotonic(); buf = bytearray()
        while time.monotonic() - t0 < timeout:
            chunk = self.read_all()
            if chunk:
                buf.extend(chunk)
                idx = buf.find(b"\x00MSN")
                if idx >= 0 and len(buf) >= idx + 6:
                    log.info("检测到 MSN 设备, 版本=%s",
                             bytes(buf[idx+4:idx+6]).decode("ascii", "ignore"))
                    self.write(b"\x00MSNCN")
                    time.sleep(0.05); self.read_all()
                    self._handshaked = True; return True
            time.sleep(0.01)
        return False

class FrameEncoder:
    SEG = 256

    def __init__(self):
        self._last_raw = None

    def reset(self):
        self._last_raw = None

    @staticmethod
    def _to_rgb565(img):
        data = img.tobytes()
        if _np is not None:
            a = _np.frombuffer(data, dtype=_np.uint8).reshape(-1, 3)
            a = a.astype(_np.uint16)
            v = ((a[:, 0] >> 3) << 11) | ((a[:, 1] >> 2) << 5) | (a[:, 2] >> 3)
            return v.astype(">u2").tobytes()
        r = data[0::3]
        g = data[1::3]
        b = data[2::3]
        n = len(r)
        raw = bytearray(n * 2)
        for k in range(n):
            v = ((r[k] >> 3) << 11) | ((g[k] >> 2) << 5) | (b[k] >> 3)
            raw[2 * k] = v >> 8
            raw[2 * k + 1] = v & 0xFF
        return bytes(raw)

    def encode(self, img):
        if img.size != (SCREEN_W, SCREEN_H):
            img = img.resize((SCREEN_W, SCREEN_H))
        if img.mode != "RGB":
            img = img.convert("RGB")
        raw = self._to_rgb565(img)
        if raw == self._last_raw:
            return b""
        self._last_raw = raw

        out = bytearray()
        out += MiniProtocol.lcd_set_xy(0, 0)
        out += MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H)
        out += MiniProtocol.lcd_enter_direct_mode()

        total = len(raw)
        n_segs = (total + 255) // 256
        for si in range(n_segs):
            seg = bytearray(raw[si * 256:(si + 1) * 256])
            if len(seg) < 256:
                seg += b"\xff" * (256 - len(seg))
            units = [bytes(seg[k:k + 4]) for k in range(0, 256, 4)]
            counts = {}
            for u in units:
                counts[u] = counts.get(u, 0) + 1
            fill = max(counts, key=counts.get)
            out += MiniProtocol.lcd_set_fill(*fill)
            if sum(1 for u in units if u != fill) > 44:
                for idx, u in enumerate(units):
                    out += MiniProtocol.lcd_data_block(idx, *u)
            else:
                for idx, u in enumerate(units):
                    if u != fill:
                        out += MiniProtocol.lcd_data_block(idx, *u)
            out += MiniProtocol.lcd_segment_end_full()
        return bytes(out)

def _normalize_port(port):
    if sys.platform == "win32":
        return str(port)
    p = str(port)
    if p.startswith("/"):
        return p
    if p.startswith(("tty", "cu.", "serial")):
        return "/dev/" + p
    return p

class MiniDevice:
    def __init__(self, port):
        port = _normalize_port(port)
        self.port = port
        self.transport = SerialTransport(port)
        self.encoder = FrameEncoder()
        self.screen_reversed = False

    def open(self):
        ok = self.transport.open()
        if ok:
            self.encoder.reset()
        return ok

    def close(self):  self.transport.close()

    @property
    def connected(self): return self.transport.connected

    def show_image(self, img):
        frame = self.encoder.encode(img)
        if not frame:
            return True
        SEG = 2048
        for i in range(0, len(frame), SEG):
            if not self.transport.write(frame[i:i+SEG]):
                return False
            time.sleep(0.003)
        time.sleep(0.02)
        self.transport.read_all()
        return True

    def set_reverse(self, reverse):
        self.screen_reversed = bool(reverse)

    def heartbeat(self):
        ok = self.transport.write(MiniProtocol.read_adc(15))
        time.sleep(0.01)
        self.transport.read_all()
        return ok

    def read_touch(self):
        if not self.transport.write(MiniProtocol.read_adc(TOUCH_CH)):
            return None
        return self.transport.read_adc(TOUCH_CH, timeout=0.03)

class Background:
    MAX_FRAMES = 300
    MODES = ("fit", "fill", "stretch")
    PLAYLIST_SECS = 12.0

    def __init__(self):
        self.path = ""
        self.mode = "fit"
        self.dim = 0
        self.blur = 0
        self.raw = []
        self.frames = []
        self.durations = []
        self.groups = []
        self.group_idx = 0
        self.group_ts = None
        self.idx = 0
        self.t_last = None

    def configure(self, dim=0, blur=0):
        self.dim = _coerce_int(dim, 0, 0, 90)
        self.blur = _coerce_int(blur, 0, 0, 12)
        self.frames = [self._decorate(f) for f in self.raw]

    def _decorate(self, img):
        if not self.raw:
            return img
        out = img
        if self.blur > 0:
            try:
                from PIL import ImageFilter
                out = out.filter(ImageFilter.GaussianBlur(self.blur))
            except Exception:
                pass
        if self.dim > 0:
            try:
                overlay = Image.new("RGB", out.size, "black")
                out = Image.blend(out, overlay, self.dim / 100.0)
            except Exception:
                pass
        return out

    def load_playlist(self, paths, mode=None):
        if mode is not None:
            self.mode = mode if mode in self.MODES else "fit"
        self.path = ";".join(paths)
        self.groups = []
        self.raw = []
        self.frames = []
        self.durations = []
        self.idx = 0
        self.t_last = None
        self.group_idx = 0
        self.group_ts = None
        for i, path in enumerate(paths[:32]):
            if not path:
                continue
            self.raw = []
            self.durations = []
            self._read_path(Path(path))
            if self.raw:
                self.groups.append((list(self.raw), list(self.durations)))
        self.raw = []
        self.durations = []
        if self.groups:
            self.raw, self.durations = list(self.groups[0][0]), list(self.groups[0][1])
            self.frames = [self._decorate(f) for f in self.raw]
        log.info("背景轮播已加载: %d 组 (%s, 模式 %s)", len(self.groups),
                 self.path, self.mode)

    def _read_path(self, p):
        if not p.exists():
            log.warning("背景图不存在: %s", p)
            return
        if p.is_dir():
            try:
                files = sorted(x for x in p.iterdir()
                               if x.is_file() and x.suffix.lower() in IMAGE_EXTS)
            except Exception as e:
                log.warning("扫描图片目录失败: %s", e)
                return
            for f in files[:self.MAX_FRAMES]:
                try:
                    with Image.open(f) as src:
                        self.raw.append(self._frame(src.convert("RGB")))
                    self.durations.append(0.2)
                except Exception as e:
                    log.warning("跳过图片 %s: %s", f.name, e)
            return
        try:
            with Image.open(p) as src:
                if getattr(src, "is_animated", False):
                    for i in range(min(src.n_frames, self.MAX_FRAMES)):
                        src.seek(i)
                        self.raw.append(self._frame(src.convert("RGB")))
                        self.durations.append(
                            max(0.05, src.info.get("duration", 100) / 1000.0))
                else:
                    self.raw.append(self._frame(src.convert("RGB")))
                    self.durations.append(0.0)
        except Exception as e:
            log.warning("背景图处理失败: %s", e)
            self.raw = []
            self.durations = []

    def load(self, path, mode=None):
        self.path = path
        if mode is not None:
            self.mode = mode if mode in self.MODES else "fit"
        self.groups = []
        self.raw = []
        self.frames = []
        self.durations = []
        self.idx = 0
        self.t_last = None
        self.group_ts = None
        if not path:
            return
        self._read_path(Path(path))
        self.frames = [self._decorate(f) for f in self.raw]
        log.info("背景图已加载: %s (%d 帧, 模式 %s)", path, len(self.frames), self.mode)

    def _frame(self, img):
        tw, th = SCREEN_W, SCREEN_H
        mode = self.mode if self.mode in self.MODES else "fit"
        try:
            if mode == "stretch":
                return img.resize((tw, th), Image.LANCZOS)
            scale = (max(tw / img.width, th / img.height) if mode == "fill"
                     else min(tw / img.width, th / img.height))
            nw = max(1, int(img.width * scale))
            nh = max(1, int(img.height * scale))
            img = img.resize((nw, nh), Image.LANCZOS)
            if mode == "fill":
                x = max(0, (nw - tw) // 2)
                y = max(0, (nh - th) // 2)
                return img.crop((x, y, x + tw, y + th))
            canvas = Image.new("RGB", (tw, th), "black")
            canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
            return canvas
        except Exception as e:
            log.warning("背景变换失败: %s", e)
            return Image.new("RGB", (tw, th), "black")

    def tick(self, now):
        if len(self.groups) > 1:
            if self.group_ts is None:
                self.group_ts = now
            elif now - self.group_ts >= self.PLAYLIST_SECS:
                self.group_ts = now
                self.group_idx = (self.group_idx + 1) % len(self.groups)
                self.raw, self.durations = list(self.groups[self.group_idx][0]), list(self.groups[self.group_idx][1])
                self.frames = [self._decorate(f) for f in self.raw]
                self.idx = 0
                self.t_last = None
                log.debug("背景切换到第 %d 组", self.group_idx + 1)
        if not self.frames:
            return None
        if len(self.frames) == 1:
            return self.frames[0]
        if self.t_last is None:
            self.t_last = now
            return self.frames[0]
        if now - self.t_last >= self.durations[self.idx]:
            self.idx = (self.idx + 1) % len(self.frames)
            self.t_last = now
        return self.frames[self.idx]

def _act_screenshot():
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(SCREENSHOT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"shot_{ts}.png"
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab()
            img.save(p)
            log.info("截屏已保存: %s", p)
            return True
        except Exception:
            pass
        if sys.platform == "darwin":
            if shutil.which("screencapture"):
                subprocess.run(["screencapture", str(p)], check=False)
                return p.exists()
        if sys.platform.startswith("linux"):
            for cmd in (["gnome-screenshot", "-f", str(p)],
                        ["scrot", str(p)],
                        ["import", "-window", "root", str(p)]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, check=False)
                    if p.exists():
                        log.info("截屏已保存: %s", p)
                        return True
    except Exception as e:
        log.warning("截屏失败: %s", e)
    return False

def _act_mute_toggle():
    try:
        if sys.platform == "win32":
            import ctypes
            VK_VOLUME_MUTE = 0xAD
            KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)
            return True
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 "set volume output muted not (output muted of (get volume settings))"],
                check=False, timeout=2)
            return True
        if sys.platform.startswith("linux"):
            for cmd in (["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                        ["amixer", "-q", "set", "Master", "toggle"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, check=False, timeout=2)
                    return True
            log.warning("未找到 pactl 或 amixer，无法切换静音")
            return False
    except Exception as e:
        log.debug("静音切换失败: %s", e)
    return False

def _act_send_macro(combo):
    combo = (combo or "").strip()
    if not combo:
        return False
    try:
        from pynput.keyboard import Controller, Key
    except ImportError:
        log.warning("未安装 pynput，无法发送键盘宏")
        return False

    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return False

    key_map = {
        "ctrl": Key.ctrl, "control": Key.ctrl,
        "shift": Key.shift, "alt": Key.alt,
        "win": Key.cmd, "cmd": Key.cmd, "super": Key.cmd,
    }
    special = {
        "enter": Key.enter, "return": Key.enter,
        "esc": Key.esc, "escape": Key.esc,
        "tab": Key.tab, "space": Key.space,
        "backspace": Key.backspace, "delete": Key.delete,
        "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
        "home": Key.home, "end": Key.end,
        "pageup": Key.page_up, "pagedown": Key.page_down,
    }

    mods = []
    main_key = None
    for p in parts:
        if p in key_map:
            mods.append(key_map[p])
        else:
            main_key = p

    ctrl = Controller()
    pressed = []
    try:
        for m in mods:
            ctrl.press(m)
            pressed.append(m)
        if main_key:
            k = None
            if len(main_key) == 1:
                k = main_key
            elif main_key in special:
                k = special[main_key]
            elif main_key.startswith("f") and main_key[1:].isdigit():
                k = getattr(Key, main_key, None)
            if k is not None:
                ctrl.press(k); ctrl.release(k)
        log.info("宏已发送: %s", combo)
        return True
    except Exception as e:
        log.warning("宏发送失败: %s", e)
        return False
    finally:
        for m in reversed(pressed):
            try: ctrl.release(m)
            except Exception: pass

def _act_launch_program(cmd):
    cmd = (cmd or "").strip()
    if not cmd:
        log.warning("启动自定义程序：路径为空")
        return False
    try:
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                cmd, shell=True, close_fds=True,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                cmd, shell=True, close_fds=True,
                start_new_session=True,
            )
        log.info("已启动程序: %s", cmd)
        return True
    except Exception as e:
        log.warning("启动程序失败: %s", e)
        return False

def _coerce_int(value, default, lo=None, hi=None):
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v

def _widget_watch_dirs():
    dirs = []
    try:
        base = getattr(_widgets_pkg, "_pkg_path", None)
        if base is not None:
            dirs.append(Path(base))
        for item in list(getattr(_widgets_pkg, "_ext_dirs", []) or []):
            dirs.append(Path(item))
    except Exception:
        pass
    return dirs

def _build_overlay(text, orientation):
    w, h = (PORTRAIT_SIZE if orientation == PORTRAIT else LANDSCAPE_SIZE)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 6
    font = get_font(12, bold=True, cjk=True)
    max_w = w - pad * 2 - 12
    lines = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and d.textlength(cur + ch, font=font) > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    lines = lines[:6]
    line_h = 16
    box_h = len(lines) * line_h + 12
    box_y = max(3, (h - box_h) // 2)
    d.rounded_rectangle([pad, box_y, w - pad, box_y + box_h], radius=6,
                        fill=(18, 22, 30, 235), outline=(90, 120, 200, 255))
    y = box_y + 6
    for line in lines:
        d.text((pad + 6, y), line, font=font, fill=(240, 244, 255, 255))
        y += line_h
    return img

class Scheduler(threading.Thread):
    def __init__(self, device, config, on_config_changed=None, on_shutdown=None,
                 shared=None):
        super().__init__(daemon=True, name="Scheduler")
        self.device = device
        self.config = config
        self.shared = shared if isinstance(shared, dict) else {}
        self._on_config_changed = on_config_changed
        self._on_shutdown = on_shutdown

        self.orientation = config.get("orientation", LANDSCAPE)
        self.reverse = bool(config.get("reverse", False))
        self.portrait_rotate = _coerce_int(config.get("portrait_rotate", -90), -90)

        bg = config.get("background")
        if not isinstance(bg, dict):
            bg = {}
        self._bg_enabled = bool(bg.get("enabled", False))
        self._bg_path = str(bg.get("path", "") or "")
        self._bg_paths = [p for p in (bg.get("paths") or []) if isinstance(p, str)]
        self._bg_mode = bg.get("mode")
        if self._bg_mode not in Background.MODES:
            self._bg_mode = "fit"
        self._bg_dim = _coerce_int(bg.get("dim"), 0, 0, 90)
        self._bg_blur = _coerce_int(bg.get("blur"), 0, 0, 12)
        self.background = Background()
        self.background.configure(self._bg_dim, self._bg_blur)
        if self._bg_enabled:
            if self._bg_paths:
                self.background.load_playlist(self._bg_paths, self._bg_mode)
            elif self._bg_path:
                self.background.load(self._bg_path, self._bg_mode)

        self._overlay = None
        self._overlay_until = 0.0
        self._overlay_seen = None
        self._frames_window = collections.deque()
        self.metrics = {"frames": 0, "bytes": 0, "reconnects": 0,
                        "render_ms": {}, "touch": 0}
        self._schedule_seen = set()
        self._watch_mtime = 0.0
        self._watch_ts = 0.0

        self._touch_baseline = None
        self._touch_cal = []
        self._touch_pressed = False
        self._touch_press_ts = 0.0
        self._touch_last_sample_ts = 0.0
        self._touch_waiting_double = False
        self._touch_double_ts = 0.0
        self._touch_long_fired = False
        self._locked = False
        self._load_touch_cfg(config)

        self.ctx = RenderContext(
            device=device, now=time.time(),
            orientation=self.orientation, config=self._cfg_view(),
        )
        self._ctxs = {}
        self._last_entered = None
        self._last_imgs = {}
        self._force_send = False
        self._stop = threading.Event()
        self._switch = threading.Event()
        self._ops = collections.deque()
        self._ops_lock = threading.Lock()
        self._widgets = []
        self._current = 0
        self._cooldown_until = 0.0
        self._last_send_mono = 0.0

        self._reload_widgets()

    def _load_touch_cfg(self, config):
        tk = config.get("touch")
        if not isinstance(tk, dict):
            tk = {}
        self._touch_cfg = {
            "single": tk.get("single", "next_page"),
            "double": tk.get("double", "toggle_orientation"),
            "long":   tk.get("long",   "toggle_reverse"),
            "macro_single": str(tk.get("macro_single", "") or ""),
            "macro_double": str(tk.get("macro_double", "") or ""),
            "macro_long":   str(tk.get("macro_long", "") or ""),
            "program_single": str(tk.get("program_single", "") or ""),
            "program_double": str(tk.get("program_double", "") or ""),
            "program_long":   str(tk.get("program_long", "") or ""),
            "threshold": _coerce_int(tk.get("threshold", TOUCH_DEFAULT_THRESHOLD),
                                     TOUCH_DEFAULT_THRESHOLD, 5, 2000),
        }
        self._touch_baseline = None
        self._touch_cal = []
        self._touch_pressed = False
        self._touch_press_ts = 0.0
        self._touch_last_sample_ts = 0.0
        self._touch_waiting_double = False
        self._touch_double_ts = 0.0
        self._touch_long_fired = False

    def set_touch_config(self, new_cfg):
        if not isinstance(new_cfg, dict):
            new_cfg = {}
        tk = self.config.get("touch")
        if not isinstance(tk, dict):
            tk = {}
            self.config["touch"] = tk
        for k in ("single", "double", "long",
                  "macro_single", "macro_double", "macro_long",
                  "program_single", "program_double", "program_long"):
            if k not in new_cfg:
                continue
            v = new_cfg[k]
            if v is None:
                v = ""
            elif not isinstance(v, str):
                v = str(v)
            tk[k] = v[:512].strip()
            if k in ("single", "double", "long") and tk[k] not in _TOUCH_ACTIONS:
                tk[k] = "passthrough"
        if "threshold" in new_cfg:
            tk["threshold"] = _coerce_int(new_cfg["threshold"],
                                          TOUCH_DEFAULT_THRESHOLD, 5, 2000)
        self._load_touch_cfg(self.config)
        log.info("触摸配置已更新: %s", self._touch_cfg)

    def _allow(self, name="操作", from_touch=False):
        if not from_touch:
            self._cooldown_until = time.monotonic() + MANUAL_COOLDOWN
            return True
        now = time.monotonic()
        if now < self._cooldown_until:
            log.info("%s 被冷却拒绝 (还剩 %.2fs)",
                     name, self._cooldown_until - now)
            return False
        self._cooldown_until = now + MANUAL_COOLDOWN
        return True

    def _cfg_view(self):
        view = dict(self.shared) if self.shared else {}
        view.update(self.config)
        if self.shared:
            view["widget_config"] = self.shared.get("widget_config") or {}
            view["gif_dir"] = self.shared.get("gif_dir") or ""
            view["font"] = self.shared.get("font") or ""
        return view

    def _ctx_for(self, w):
        ctx = self._ctxs.get(w.name)
        if ctx is None:
            ctx = RenderContext(device=self.device, now=time.time(),
                                orientation=self.orientation,
                                state={}, config=self._cfg_view())
            self._ctxs[w.name] = ctx
        return ctx

    def post_op(self, op, *args):
        with self._ops_lock:
            self._ops.append((op, args))

    def _drain_ops(self):
        while True:
            with self._ops_lock:
                if not self._ops:
                    return
                op, args = self._ops.popleft()
            try:
                if op == "reload":
                    self._reload_widgets()
                elif op == "restart":
                    self._restart_widget(*args)
                elif op == "background":
                    self._apply_background()
            except Exception as e:
                log.warning("后台任务 %s 失败: %s", op, e)

    def _teardown_all(self):
        for w in list(self._widgets):
            try: w.teardown(self._ctx_for(w))
            except Exception as e:
                log.warning("插件 %s teardown 失败: %s", w.name, e)
        self._ctxs.clear()
        self._last_imgs.clear()
        self._last_entered = None

    def _reload_widgets(self):
        self._teardown_all()

        names = []
        for name in self.config.get("pages") or []:
            if name in names:
                log.warning("重复的页面已忽略: %s", name)
                continue
            names.append(name)

        new_widgets = []
        for name in names:
            cls = WIDGET_REGISTRY.get(name)
            if not cls:
                log.warning("未知插件: %s", name); continue
            w = cls()
            try: w.setup(self._ctx_for(w))
            except Exception as e:
                log.warning("插件 %s setup 失败: %s", name, e)
            new_widgets.append(w)

        saved = self.config.get("current_page", "")
        new_current = 0
        if saved:
            for i, w in enumerate(new_widgets):
                if w.name == saved:
                    new_current = i
                    break
        new_current = max(0, min(new_current, max(0, len(new_widgets) - 1)))

        self._widgets = new_widgets
        self._current = new_current
        self._switch.set()

        log.info("已加载 %d 个插件: %s (当前: %s)",
                 len(self._widgets),
                 [w.name for w in self._widgets],
                 self._widgets[self._current].name if self._widgets else "-")
        self._ensure_supported()

    def _save_config(self):
        if self._on_config_changed:
            try: self._on_config_changed()
            except Exception as e:
                log.warning("保存配置失败: %s", e)

    def _persist_current(self):
        if not self._widgets:
            return
        name = self._widgets[self._current].name
        if self.config.get("current_page") != name:
            self.config["current_page"] = name
            self._save_config()

    def find_widget(self, name):
        for w in self._widgets:
            if w.name == name:
                return w
        return None

    def _ensure_supported(self):
        if not self._widgets:
            return False
        if self.orientation in self._widgets[self._current].orientations:
            return True
        if not self._find_supported(self._current, +1):
            log.warning("没有插件支持 %s 朝向", self.orientation)
            return False
        return True

    def _find_supported(self, start, direction):
        n = len(self._widgets)
        if n == 0:
            return False
        for i in range(n):
            idx = (start + i * direction) % n
            if self.orientation in self._widgets[idx].orientations:
                if idx != self._current:
                    self._current = idx
                    self._persist_current()
                return True
        return False

    def reload(self):
        self.post_op("reload")

    def next_page(self, from_touch=False):
        if from_touch and self._locked:
            log.info("已锁定，忽略触摸翻页")
            return False
        if not self._allow("next_page", from_touch): return False
        if not self._widgets: return False
        self._find_supported(self._current + 1, +1)
        self._switch.set()
        log.info("切页 -> %s", self._widgets[self._current].name)
        return True

    def prev_page(self, from_touch=False):
        if from_touch and self._locked:
            log.info("已锁定，忽略触摸翻页")
            return False
        if not self._allow("prev_page", from_touch): return False
        if not self._widgets: return False
        self._find_supported(self._current - 1, -1)
        self._switch.set()
        log.info("切页 -> %s", self._widgets[self._current].name)
        return True

    def toggle_reverse(self, from_touch=False):
        if not self._allow("toggle_reverse", from_touch): return False
        self.reverse = not self.reverse
        self.device.set_reverse(self.reverse)
        self.config["reverse"] = self.reverse
        self._save_config()
        self._switch.set()
        log.info("翻转 -> %s", self.reverse)
        return True

    def toggle_orientation(self, from_touch=False):
        if not self._allow("toggle_orientation", from_touch): return False
        self.orientation = (PORTRAIT if self.orientation == LANDSCAPE
                            else LANDSCAPE)
        self.ctx.orientation = self.orientation
        self.config["orientation"] = self.orientation
        self._save_config()
        self._ensure_supported()
        self._switch.set()
        log.info("朝向 -> %s", self.orientation)
        return True

    def toggle_lock(self, from_touch=False):
        self._locked = not self._locked
        log.info("锁定 -> %s", self._locked)
        return True

    def set_pages(self, page_names):
        seen = set()
        pages = []
        for name in page_names:
            if not isinstance(name, str) or name in seen:
                continue
            if name not in WIDGET_REGISTRY:
                continue
            seen.add(name)
            pages.append(name)
        self.config["pages"] = pages
        self.post_op("reload")
        log.info("页面已更新: %s", pages)
        return pages

    def set_background(self, enabled, path="", mode=None, paths=None, dim=None,
                       blur=None):
        self._bg_enabled = bool(enabled)
        self._bg_path = str(path or "")
        if paths is not None:
            self._bg_paths = [p for p in paths if isinstance(p, str)][:32]
        if mode is not None:
            self._bg_mode = mode if mode in Background.MODES else "fit"
        if dim is not None:
            self._bg_dim = _coerce_int(dim, self._bg_dim, 0, 90)
        if blur is not None:
            self._bg_blur = _coerce_int(blur, self._bg_blur, 0, 12)
        self.post_op("background")
        return True

    def _apply_background(self):
        self.background.configure(self._bg_dim, self._bg_blur)
        if self._bg_enabled and self._bg_paths:
            self.background.load_playlist(self._bg_paths, self._bg_mode)
        elif self._bg_enabled and self._bg_path:
            self.background.load(self._bg_path, self._bg_mode)
        else:
            self.background.load("", self._bg_mode)
        self._switch.set()
        log.info("背景 -> enabled=%s mode=%s dim=%d blur=%d path=%s",
                 self._bg_enabled, self._bg_mode, self._bg_dim, self._bg_blur,
                 self._bg_path)

    def notify(self, text, secs=5):
        text = str(text or "").strip()
        if not text:
            return False
        try:
            secs = _coerce_int(secs, 5, 1, 60)
            img = _build_overlay(text, self.orientation)
        except Exception as e:
            log.warning("通知生成失败: %s", e)
            return False
        self._overlay = img
        self._overlay_until = time.monotonic() + secs
        self._overlay_seen = None
        self._force_send = True
        log.info("通知: %s (%ds)", text, secs)
        return True

    def _count_frame(self, mono):
        self.metrics["frames"] = int(self.metrics.get("frames", 0)) + 1
        total = 0
        try:
            total = int(getattr(self.device.transport, "bytes_written", 0))
        except Exception:
            total = 0
        last = int(self.metrics.get("_bytes_last", 0))
        delta = total - last if total >= last else 0
        self.metrics["_bytes_last"] = total
        self.metrics["bytes"] = int(self.metrics.get("bytes", 0)) + delta
        self._frames_window.append((mono, delta))

    def _check_schedules(self, now):
        items = self.shared.get("schedules") if self.shared else None
        if not items:
            return
        t = time.localtime(now)
        hhmm = "%02d:%02d" % (t.tm_hour, t.tm_min)
        day_key = time.strftime("%Y%m%d", t)
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if str(item.get("at") or "") != hhmm:
                continue
            days = item.get("days")
            if days and t.tm_wday not in days:
                continue
            key = (day_key, i)
            if key in self._schedule_seen:
                continue
            self._schedule_seen.add(key)
            action = str(item.get("action") or "")
            log.info("定时任务触发: %s -> %s", hhmm, action)
            try:
                self._exec_action(action)
            except Exception as e:
                log.warning("定时任务执行失败: %s", e)

    def _watch_widgets(self, mono):
        if mono - self._watch_ts < 1.5:
            return
        self._watch_ts = mono
        stamp = 0.0
        for d in _widget_watch_dirs():
            try:
                entries = list(d.iterdir())
            except Exception:
                continue
            for f in entries:
                try:
                    if f.is_dir():
                        target = f / "__init__.py"
                        if not target.exists():
                            continue
                    elif f.suffix == ".py":
                        target = f
                    else:
                        continue
                    m = target.stat().st_mtime
                except Exception:
                    continue
                if m > stamp:
                    stamp = m
        if self._watch_mtime == 0.0:
            self._watch_mtime = stamp
            return
        if stamp > self._watch_mtime:
            self._watch_mtime = stamp
            log.info("检测到插件文件变化，正在重新加载")
            self.post_op("reload")

    def metrics_snapshot(self):
        now = time.monotonic()
        while self._frames_window and now - self._frames_window[0][0] > 10.0:
            self._frames_window.popleft()
        span = 0.0
        if self._frames_window:
            span = max(0.001, now - self._frames_window[0][0])
        fps = len(self._frames_window) / span if span > 0.001 else 0.0
        bps = sum(b for _, b in self._frames_window) / span if span > 0.001 else 0.0
        render = dict(self.metrics.get("render_ms") or {})
        top = sorted(render.items(), key=lambda kv: kv[1], reverse=True)[:6]
        return {
            "fps": round(fps, 2),
            "bytes_per_sec": int(bps),
            "frames": int(self.metrics.get("frames", 0)),
            "bytes": int(self.metrics.get("bytes", 0)),
            "reconnects": int(self.metrics.get("reconnects", 0)),
            "touch": int(self.metrics.get("touch", 0)),
            "render_ms": {k: round(v, 2) for k, v in top},
            "background": {"enabled": self._bg_enabled, "mode": self._bg_mode,
                           "dim": self._bg_dim, "blur": self._bg_blur,
                           "frames": len(self.background.frames)},
        }

    def switch_to(self, name):
        for i, w in enumerate(self._widgets):
            if w.name == name:
                if i != self._current:
                    self._current = i
                    self._persist_current()
                    self._switch.set()
                    log.info("切页 -> %s", name)
                return True
        return False

    def restart_widget(self, name):
        self.post_op("restart", name)
        return True

    def _restart_widget(self, name):
        w = self.find_widget(name)
        if not w:
            log.warning("重启失败，未找到插件: %s", name)
            return False
        try: w.teardown(self._ctx_for(w))
        except Exception: pass
        self._ctxs.pop(name, None)
        self._last_imgs.pop(name, None)
        try:
            w.setup(self._ctx_for(w))
            self._switch.set()
            return True
        except Exception as e:
            log.warning("插件 %s 重启失败: %s", name, e)
            return False

    def stop(self):
        self._stop.set()

    def _poll_touch(self, now):
        v = self.device.read_touch()
        if v is None:
            return

        if self._touch_baseline is None:
            self._touch_cal.append(v)
            if len(self._touch_cal) >= 6:
                self._touch_baseline = min(self._touch_cal)
                log.info("触摸基准 = %d", self._touch_baseline)
            return

        thr = self._touch_cfg["threshold"]
        pressed = v < (self._touch_baseline - thr)
        log.debug("touch adc=%d base=%d thr=%d pressed=%s",
                  v, self._touch_baseline, thr, pressed)

        if pressed and not self._touch_pressed:
            self._touch_press_ts = now
            self._touch_pressed = True
            self._touch_long_fired = False
        elif pressed and self._touch_pressed:
            if (not self._touch_long_fired and
                    now - self._touch_press_ts >= TOUCH_LONG_PRESS):
                self._touch_long_fired = True
                self._touch_waiting_double = False
                log.info("触摸: 长按")
                self._dispatch_touch("long")
        elif not pressed and self._touch_pressed:
            self._touch_pressed = False
            if self._touch_long_fired:
                pass
            elif (self._touch_waiting_double and
                    now - self._touch_double_ts <= TOUCH_DOUBLE_WINDOW):
                self._touch_waiting_double = False
                log.info("触摸: 双击")
                self._dispatch_touch("double")
            else:
                self._touch_waiting_double = True
                self._touch_double_ts = now

        if self._touch_waiting_double and \
                now - self._touch_double_ts > TOUCH_DOUBLE_WINDOW:
            self._touch_waiting_double = False
            log.info("触摸: 单击")
            self._dispatch_touch("single")

    def _dispatch_touch(self, gesture):
        action = self._touch_cfg.get(gesture, "passthrough")
        if action not in _TOUCH_ACTIONS:
            action = "passthrough"
        macro = self._touch_cfg.get(f"macro_{gesture}", "")
        program = self._touch_cfg.get(f"program_{gesture}", "")
        if action == "passthrough":
            w = self._widgets[self._current] if self._widgets else None
            if w is None:
                return
            try:
                r = w.on_action(f"touch_{gesture}", {}, self._ctx_for(w))
                if isinstance(r, dict) and r.get("ok"):
                    self._switch.set()
            except Exception as e:
                log.warning("插件 %s touch_%s 失败: %s", w.name, gesture, e)
            return
        try:
            self._exec_action(action, macro, program, from_touch=True)
        except Exception as e:
            log.exception("执行触摸动作 %s/%s 失败: %s", gesture, action, e)

    def _exec_action(self, action, macro="", program="", from_touch=False):
        if action == "next_page":
            self.next_page(from_touch=from_touch)
        elif action == "prev_page":
            self.prev_page(from_touch=from_touch)
        elif action == "flip":
            self.toggle_reverse(from_touch=from_touch)
        elif action == "toggle_orientation":
            self.toggle_orientation(from_touch=from_touch)
        elif action == "lock":
            self.toggle_lock(from_touch=from_touch)
        elif action == "screenshot":
            threading.Thread(target=_act_screenshot, daemon=True).start()
        elif action == "mute_toggle":
            threading.Thread(target=_act_mute_toggle, daemon=True).start()
        elif action == "macro":
            threading.Thread(target=_act_send_macro, args=(macro,),
                             daemon=True).start()
        elif action == "launch":
            threading.Thread(target=_act_launch_program, args=(program,),
                             daemon=True).start()
        elif action == "shutdown":
            log.info("触摸触发关闭程序")
            if callable(self._on_shutdown):
                self._on_shutdown()
        elif action == "blank":
            size = PORTRAIT_SIZE if self.orientation == PORTRAIT else LANDSCAPE_SIZE
            self._overlay = Image.new("RGBA", size, (0, 0, 0, 255))
            self._overlay_until = time.monotonic() + 5.0
            self._overlay_seen = None
            self._force_send = True
            log.info("屏幕已清空 5 秒")
        else:
            log.warning("未知触摸动作: %s", action)

    def _composite(self, widget_img, mono):
        bg = self.background.tick(mono)
        if bg is None:
            canvas = Image.new("RGBA", (SCREEN_W, SCREEN_H), "black")
        else:
            canvas = bg.convert("RGBA")
        if widget_img is None:
            out = canvas.convert("RGB")
        else:
            if self.orientation == PORTRAIT:
                widget_img = widget_img.rotate(self.portrait_rotate, expand=True)
            if widget_img.size != (SCREEN_W, SCREEN_H):
                widget_img = widget_img.resize((SCREEN_W, SCREEN_H))
            if widget_img.mode != "RGBA":
                widget_img = widget_img.convert("RGBA")
            out = Image.alpha_composite(canvas, widget_img).convert("RGB")
        if self.reverse:
            out = out.rotate(180)
        return out

    def _handle_switch(self):
        widgets = self._widgets
        current = self._current
        cur = widgets[current] if 0 <= current < len(widgets) else None
        prev = self._last_entered
        if prev is not None and prev is not cur:
            try: prev.on_leave(self._ctx_for(prev))
            except Exception as e:
                log.warning("on_leave %s 失败: %s", prev.name, e)
            if all(x is not prev for x in widgets):
                self._ctxs.pop(prev.name, None)
        if cur is not None:
            self._last_entered = cur
            try: cur.on_enter(self._ctx_for(cur))
            except Exception as e:
                log.warning("on_enter %s 失败: %s", cur.name, e)
        self._force_send = True

    def run(self):
        log.info("Scheduler 启动, %d 页, 朝向 %s, 翻转 %s",
                 len(self._widgets), self.orientation, self.reverse)
        last_mono = 0.0
        last_bg_ts = 0.0
        try:
            while not self._stop.is_set():
                if not self.device.connected:
                    if not self.device.open():
                        time.sleep(1.0); continue
                    self.device.set_reverse(self.reverse)
                    self.metrics["reconnects"] = int(self.metrics.get("reconnects", 0)) + 1
                    last_mono = 0.0
                    last_bg_ts = 0.0

                self._drain_ops()
                self._watch_widgets(time.monotonic())
                self._check_schedules(time.time())

                widgets = self._widgets
                current = self._current
                if not widgets or current >= len(widgets):
                    time.sleep(0.5); continue

                if self._switch.is_set():
                    self._switch.clear()
                    last_mono = 0.0
                    last_bg_ts = 0.0
                    self._handle_switch()

                w = widgets[current]
                now = time.time()
                mono = time.monotonic()
                self.ctx.now = now
                self.ctx.orientation = self.orientation
                wctx = self._ctx_for(w)
                wctx.now = now
                wctx.orientation = self.orientation

                need_render = (mono - last_mono >= w.interval)
                need_bg = (self._bg_enabled and
                           len(self.background.frames) > 1 and
                           mono - last_bg_ts >= 0.1)
                need_heartbeat = (not need_render and not need_bg and
                                  mono - self._last_send_mono >= HEARTBEAT_INTERVAL)

                if self._overlay is not None:
                    if mono >= self._overlay_until:
                        self._overlay = None
                        self._overlay_seen = None
                        self._force_send = True
                        overlay = None
                    else:
                        overlay = self._overlay
                else:
                    overlay = None

                if overlay is not None:
                    key = id(overlay)
                    if key != self._overlay_seen or self._force_send:
                        self._overlay_seen = key
                        if self.device.show_image(self._composite(overlay, mono)):
                            self._last_send_mono = mono
                            self._count_frame(mono)
                        self._force_send = False
                elif need_render:
                    last_mono = mono
                    t0 = time.perf_counter()
                    try:
                        img = w.render(wctx)
                    except Exception as e:
                        log.exception("插件 %s 渲染异常: %s", w.name, e)
                        img = None
                        time.sleep(0.3)
                    t1 = time.perf_counter()
                    cost = (t1 - t0) * 1000.0
                    old = self.metrics["render_ms"].get(w.name, cost)
                    self.metrics["render_ms"][w.name] = old * 0.8 + cost * 0.2
                    if img is not None:
                        self._last_imgs[w.name] = img
                    if need_bg:
                        last_bg_ts = mono
                    if img is not None or need_bg or self._force_send:
                        frame = img if img is not None else self._last_imgs.get(w.name)
                        if frame is not None or need_bg:
                            if self.device.show_image(self._composite(frame, mono)):
                                self._last_send_mono = mono
                                self._count_frame(mono)
                            self._force_send = False
                    log.debug("渲染 %s: render=%.1fms total=%.1fms",
                              w.name, cost, (time.perf_counter() - t0) * 1000.0)
                elif need_bg:
                    last_bg_ts = mono
                    if self.device.show_image(
                            self._composite(self._last_imgs.get(w.name), mono)):
                        self._last_send_mono = mono
                        self._count_frame(mono)
                elif need_heartbeat:
                    if self.device.heartbeat():
                        self._last_send_mono = mono

                if mono - self._touch_last_sample_ts >= TOUCH_SAMPLE_INTERVAL:
                    self._touch_last_sample_ts = mono
                    try:
                        self._poll_touch(mono)
                    except Exception as e:
                        log.debug("触摸采样异常: %s", e)

                time.sleep(0.005)
        finally:
            self._teardown_all()
            log.info("Scheduler 退出")

DEFAULT_CONFIG_PATH = str(_APP_DIR / "config.json")

_CONFIG_LOCK = threading.RLock()

_TOUCH_ACTIONS = ("passthrough", "next_page", "prev_page", "flip",
                  "toggle_orientation", "lock", "screenshot",
                  "mute_toggle", "macro", "launch", "shutdown")

_TOUCH_DEFAULTS = {
    "single": "passthrough",
    "double": "passthrough",
    "long":   "passthrough",
    "macro_single": "",
    "macro_double": "",
    "macro_long":   "",
    "program_single": "",
    "program_double": "",
    "program_long":   "",
    "threshold": TOUCH_DEFAULT_THRESHOLD,
}

def _default_pages():
    names = list(WIDGET_REGISTRY.keys())
    names.sort(key=lambda n: (WIDGET_REGISTRY[n].priority, n))
    return names

def _device_default(dev_id="dev1", port="auto"):
    return {
        "id": dev_id,
        "port": port,
        "pages": _default_pages(),
        "current_page": "",
        "orientation": "landscape",
        "reverse": False,
        "portrait_rotate": -90,
        "background": {"enabled": False, "path": "", "mode": "fit",
                       "dim": 0, "blur": 0, "paths": []},
        "touch": dict(_TOUCH_DEFAULTS),
    }


def _base_default_config():
    return {
        "devices": [_device_default()],
        "gif_dir": str(DEFAULT_GIF_DIR),
        "widget_config": {},
        "font": "",
        "web": {"host": "0.0.0.0", "port": 8765},
        "log_level": "INFO",
        "schedules": [],
        "hotkeys": False,
    }

def _widget_meta():
    meta = {}
    for name, cls in sorted(WIDGET_REGISTRY.items()):
        try:
            doc = (cls.__doc__ or "").strip().splitlines()
            desc = doc[0].strip() if doc else ""
            meta[name] = {
                "description": desc,
                "orientations": list(cls.orientations),
                "interval": cls.interval,
                "priority": cls.priority,
                "has_config": bool(getattr(cls, "config_schema", [])),
                "actions": dict(getattr(cls, "actions", {}) or {}),
                "source": "external" if str(getattr(cls, "__module__", "")).startswith("msu2_ext_widgets") else "builtin",
            }
        except Exception:
            meta[name] = {}
    return meta

def _deep_fill(default, user):
    out = dict(user) if isinstance(user, dict) else {}
    for k, dv in default.items():
        if k not in out:
            out[k] = dv
            continue
        uv = out[k]
        if isinstance(dv, dict):
            out[k] = _deep_fill(dv, uv) if isinstance(uv, dict) else dict(dv)
    return out

def _sanitize_touch(tk):
    if not isinstance(tk, dict):
        tk = {}
    touch = {}
    for k, dv in _TOUCH_DEFAULTS.items():
        v = tk.get(k, dv)
        if k == "threshold":
            v = _coerce_int(v, TOUCH_DEFAULT_THRESHOLD, 5, 2000)
        else:
            v = v if isinstance(v, str) else ("" if v is None else str(v))
            v = v.strip()
        if k in ("single", "double", "long") and v not in _TOUCH_ACTIONS:
            v = "passthrough"
        touch[k] = v
    return touch


def _sanitize_device(dev, index):
    if not isinstance(dev, dict):
        dev = {}
    out = _device_default("dev%d" % (index + 1))
    dev_id = dev.get("id")
    if isinstance(dev_id, str) and dev_id.strip():
        out["id"] = dev_id.strip()[:32]
    out["port"] = str(dev.get("port") or "auto")
    raw_pages = dev.get("pages")
    pages = []
    for name in raw_pages if isinstance(raw_pages, list) else []:
        if isinstance(name, str) and name in WIDGET_REGISTRY and name not in pages:
            pages.append(name)
    out["pages"] = pages or _default_pages()
    cur = dev.get("current_page") or ""
    out["current_page"] = cur if isinstance(cur, str) and cur in out["pages"] else ""
    if dev.get("orientation") in (LANDSCAPE, PORTRAIT):
        out["orientation"] = dev["orientation"]
    out["reverse"] = bool(dev.get("reverse", False))
    rotate = _coerce_int(dev.get("portrait_rotate"), -90)
    out["portrait_rotate"] = rotate if rotate in (90, -90) else -90
    bg = dev.get("background")
    if not isinstance(bg, dict):
        bg = {}
    paths = []
    for p in bg.get("paths") if isinstance(bg.get("paths"), list) else []:
        if isinstance(p, str) and p.strip():
            paths.append(p.strip())
    out["background"] = {
        "enabled": bool(bg.get("enabled", False)),
        "path": str(bg.get("path") or ""),
        "paths": paths,
        "mode": bg.get("mode") if bg.get("mode") in Background.MODES else "fit",
        "dim": _coerce_int(bg.get("dim"), 0, 0, 90),
        "blur": _coerce_int(bg.get("blur"), 0, 0, 12),
    }
    out["touch"] = _sanitize_touch(dev.get("touch"))
    return out


def _sanitize_devices(cfg):
    raw = cfg.get("devices")
    if not isinstance(raw, list) or not raw:
        legacy = _device_default()
        for key in ("port", "pages", "current_page", "orientation", "reverse",
                    "portrait_rotate", "background", "touch"):
            if key in cfg:
                legacy[key] = cfg[key]
        raw = [legacy]
    devices = []
    seen_ids = set()
    for i, dev in enumerate(raw[:8]):
        item = _sanitize_device(dev, i)
        base_id = item["id"]
        n = 2
        while item["id"] in seen_ids:
            item["id"] = "%s-%d" % (base_id, n)
            n += 1
        seen_ids.add(item["id"])
        devices.append(item)
    for key in ("port", "pages", "current_page", "orientation", "reverse",
                "portrait_rotate", "background", "touch"):
        cfg.pop(key, None)
    cfg["devices"] = devices
    return devices


def _sanitize_schedules(items):
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        at = it.get("at")
        if not isinstance(at, str):
            continue
        parts = at.strip().split(":")
        if len(parts) != 2:
            continue
        try:
            hh, mm = int(parts[0]), int(parts[1])
        except Exception:
            continue
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        action = it.get("action")
        if not isinstance(action, str):
            continue
        url = action.strip()
        if not url:
            continue
        days = it.get("days")
        if isinstance(days, list):
            days = [d for d in (_coerce_int(x, -1) for x in days) if 0 <= d <= 6]
        else:
            days = []
        out.append({"at": "%02d:%02d" % (hh, mm), "action": url[:120],
                    "days": days})
    return out[:32]


def _sanitize_config(cfg):
    gif_dir = cfg.get("gif_dir")
    cfg["gif_dir"] = gif_dir if isinstance(gif_dir, str) and gif_dir else str(DEFAULT_GIF_DIR)
    _sanitize_devices(cfg)
    wc = cfg.get("widget_config")
    if not isinstance(wc, dict):
        wc = {}
    cfg["widget_config"] = {k: dict(v) for k, v in wc.items()
                            if k in WIDGET_REGISTRY and isinstance(v, dict)}
    font = cfg.get("font")
    cfg["font"] = font if isinstance(font, str) else ""
    web = cfg.get("web")
    if not isinstance(web, dict):
        web = {}
    host = web.get("host")
    cfg["web"] = {"host": host if isinstance(host, str) and host else "0.0.0.0",
                  "port": _coerce_int(web.get("port"), 8765, 1, 65535)}
    lvl = str(cfg.get("log_level") or "INFO").upper()
    cfg["log_level"] = lvl if lvl in ("DEBUG", "INFO", "WARNING", "ERROR",
                                      "CRITICAL") else "INFO"
    cfg["schedules"] = _sanitize_schedules(cfg.get("schedules"))
    cfg["hotkeys"] = bool(cfg.get("hotkeys", False))
    return cfg

def _write_config_raw(cfg, path):
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    data = json.dumps(cfg, indent=2, ensure_ascii=False)
    with _CONFIG_LOCK:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, "utf-8")
            os.replace(tmp, p)
            return True
        except Exception as e:
            log.warning("写入配置失败 %s: %s", p, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

def _default_config_path():
    p = Path(DEFAULT_CONFIG_PATH)
    if p.exists():
        return str(p)
    legacy = Path("./config.json")
    try:
        if legacy.exists() and legacy.resolve() != p.resolve():
            log.info("使用当前目录的配置文件: %s", legacy)
            return str(legacy)
    except Exception:
        pass
    return str(p)

def ensure_config_file(path=None):
    p = Path(path or DEFAULT_CONFIG_PATH)
    meta = _widget_meta()
    if not p.exists():
        cfg = _base_default_config()
        cfg["_help"] = {
            "port": "串口号。auto=自动扫描；也可指定 COM15 / /dev/ttyUSB0 / ttyACM0",
            "pages": "启用的插件及显示顺序，可在 Web 里调整",
            "current_page": "上次停留的插件名，启动时自动恢复",
            "orientation": "landscape / portrait",
            "reverse": "true=上下翻转（180°）",
            "portrait_rotate": "竖屏转横屏时的旋转角，-90 或 90",
            "gif_dir": "读取图片序列的目录",
            "background": {"enabled": "背景图开关",
                           "path": "背景图路径（图片、GIF 或图片序列目录）",
                           "mode": "显示方式：fit=适应 / fill=填充 / stretch=拉伸"},
            "widget_config": "每个插件自己的配置（Web 页面该行的 ⋯ 菜单里点「设置」修改）",
            "font": "自定义字体路径",
            "web": {"host": "WebUI 监听地址", "port": "WebUI 端口"},
            "log_level": "DEBUG / INFO / WARNING / ERROR",
            "touch": "全局触控动作。选 passthrough 的手势会透传给插件",
        }
        cfg["_available_widgets"] = meta
        _write_config_raw(cfg, p)
        log.info("已生成默认配置: %s", p)
        log.info("  可用插件: %s", ", ".join(meta.keys()))
        return cfg
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("文件过大")
        user_cfg = json.loads(p.read_text("utf-8"))
        if not isinstance(user_cfg, dict):
            raise ValueError("顶层必须是对象")
    except Exception as e:
        backup = p.with_name(p.name + ".bad")
        try:
            shutil.copy2(p, backup)
            log.warning("配置解析失败 (%s)，已备份到 %s", e, backup)
        except Exception:
            log.warning("配置解析失败 (%s)，无法备份", e)
        cfg = _base_default_config(); cfg["_available_widgets"] = meta
        _write_config_raw(cfg, p)
        return cfg

    merged = _sanitize_config(_deep_fill(_base_default_config(), user_cfg))
    merged["_available_widgets"] = meta
    if merged != user_cfg:
        _write_config_raw(merged, p)
        log.info("配置已规范化: %s", p)
    return merged

def save_config(cfg, path=None):
    _write_config_raw(cfg, path or DEFAULT_CONFIG_PATH)

def discover_and_open():
    ports = list(serial.tools.list_ports.comports())
    log.info("发现 %d 个串口: %s", len(ports), [p.device for p in ports])
    if not ports:
        return None
    for p in ports:
        dev = MiniDevice(p.device)
        if dev.open():
            return dev
        if dev.transport.last_error:
            log.info("  %s 不可用: %s", p.device, dev.transport.last_error)
        dev.close()
    log.error("未找到 MSU2_MINI。可能原因: 被其它程序占用 / 需提权")
    return None

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>MSU2_MINI_Console</title>
<style>
body { font-family: Consolas, Menlo, monospace; max-width: 820px;
       margin: 2em auto; padding: 0 1em; color: #222; font-size: 14px;
       background: #fafafa; }
h1 { font-size: 1.15em; margin: 0 0 .8em; }
h2 { font-size: 1em; margin: 1.2em 0 .5em; color: #444;
     border-bottom: 1px solid #ddd; padding-bottom: 4px; }
button { font-family: inherit; font-size: 13px;
         padding: 3px 10px; margin: 2px 4px 2px 0;
         background: #eee; border: 1px solid #aaa;
         border-radius: 3px; cursor: pointer; }
button:hover { background: #ddd; }
button:disabled { color: #999; cursor: not-allowed; }
button.active { background: #cde; border-color: #678; }
button.mini { padding: 1px 7px; font-size: 12px; }
button.danger { background: #d33; color: #fff; border-color: #a11;
                padding: 6px 18px; font-size: 14px; }
button.danger:hover { background: #b22; }
input[type=text], input[type=number], select, textarea {
    font-family: inherit; font-size: 13px;
    padding: 3px 6px; border: 1px solid #aaa;
    border-radius: 3px; box-sizing: border-box;
}
textarea { font-size: 12px; width: 22em; }
label { display: inline-block; margin: 2px 8px 2px 0; }
pre { background: #fff; border: 1px solid #ccc; padding: .6em;
      font-size: 12px; overflow: auto; max-height: 15em;
      white-space: pre-wrap; }
.msg { font-size: 12px; color: #080; margin-left: .6em; }
.hint { font-size: 12px; color: #444; background: #f2f5fa; border: 1px solid #dde3ee;
        border-radius: 6px; padding: 6px 8px; margin: 0 0 10px; display: none; }
.tiny { font-size: 11px; color: #999; margin: 4px 0 0; }
.msg.err { color: #c00; }
.plist { border: 1px solid #ddd; background: #fff; border-radius: 3px;
         padding: 4px 6px; }
.pitem { padding: 3px 2px; border-bottom: 1px solid #eee;
         display: flex; align-items: center; flex-wrap: wrap; }
.pitem:last-child { border-bottom: 0; }
.pitem .name { flex: 1; padding-left: 6px; }
.pitem .tags { color: #888; font-size: 11px; margin-left: 6px; }
.pitem .tag-ext { background: #eef3ff; color: #2b5fd9; border: 1px solid #b9cdf5;
  border-radius: 3px; font-size: 10px; padding: 0 4px; margin-left: 4px; }
.pitem.empty { color: #999; font-style: italic; padding: 6px 8px; }
.pitem.current { background: #e8f0ff; }
.pitem.current .name { font-weight: bold; color: #2b5fd9; }
.pitem.current .name::before { content: '▶ '; color: #2b5fd9; }
.pitem .actbtn { margin-left: 4px; padding: 0 6px; line-height: 17px; }
.actpop { position: absolute; z-index: 90; min-width: 130px;
          background: #fff; border: 1px solid #aaa; border-radius: 4px;
          box-shadow: 0 4px 14px rgba(0,0,0,.18); padding: 4px; }
.actpop .ttl { font-size: 11px; color: #888; padding: 2px 6px 4px;
               border-bottom: 1px solid #eee; margin-bottom: 3px; }
.actpop button { display: block; width: 100%; text-align: left;
                 margin: 2px 0; padding: 4px 10px; font-size: 13px;
                 border: 0; background: #f4f4f4; }
.actpop button:hover { background: #e4e4e4; }
.card { background: #fff; border: 1px solid #ddd; border-radius: 4px;
        padding: 10px 12px; margin-bottom: 12px; }
.field { margin: 8px 0; }
.field label { display: block; font-size: 12px; color: #666;
               margin-bottom: 3px; }
.field .help { color: #999; font-size: 11px; margin-left: 6px; }
.shutdown-card { border-color: #f0b0b0; background: #fff8f8; }
.shutdown-card h2 { color: #a11; border-color: #f0c0c0; }
.touch-row { display: grid; grid-template-columns: 50px 200px 1fr 1fr;
             gap: 8px; align-items: center; margin: 6px 0; }
.touch-row select { width: 200px; }
.touch-row input[type=text] { width: 100%; }
.touch-row .lbl { font-size: 13px; color: #444; }
.hint { font-size: 11px; color: #888; margin: 4px 0 8px; }
.thumb { width: 48px; height: 24px; border: 1px solid #ccc; border-radius: 2px;
         vertical-align: middle; margin-right: 6px; image-rendering: pixelated; }
</style>
</head>
<body>

<h1>MSU2_MINI_Console</h1>
<h3>本项目开源，采用 PolyForm Noncommercial License 1.0.0 </h3>

<div class="card">
  <h2>设备</h2>
  <label style="font-size:13px">当前设备
    <select id="dev-select" onchange="selectDevice()"></select>
  </label>
  <span class="msg" id="msg-dev"></span>
  <div class="tiny" id="dev-info"></div>
</div>

<div class="card">
  <h2>通知</h2>
  <input type="text" id="notify-text" placeholder="推送到屏幕上的文字" style="width:22em">
  <input type="number" id="notify-secs" value="5" min="1" max="60" style="width:4em"> 秒
  <button onclick="sendNotify()">推送</button>
  <span class="msg" id="msg-notify"></span>
</div>

<div class="card">
  <h2>显示控制</h2>
  <button onclick="post('/api/prev')">上一页</button>
  <button onclick="post('/api/next')">下一页</button>
  <button id="btn-orient" onclick="post('/api/orientation')">切换朝向</button>
  <button id="btn-rev" onclick="post('/api/reverse')">上下翻转</button>
  <span class="msg" id="msg-display"></span>
</div>

<div class="card">
  <h2>全局触控</h2>
  <div class="hint">
    选"未定义（透传给插件）"的手势会直接交给当前插件处理，各插件支持的手势见它的设置面板<br>
    自定义程序：支持完整路径和环境变量<br>
    键盘宏示例：<code>ctrl+shift+s</code>、<code>alt+f4</code>、<code>win+r</code>
  </div>
  <div class="touch-row">
    <span class="lbl">单击</span>
    <select id="tk-single" onchange="updateArgVisibility()"></select>
    <input type="text" id="tk-macro-single" placeholder="宏，例: ctrl+shift+s">
    <input type="text" id="tk-program-single" placeholder="程序路径">
  </div>
  <div class="touch-row">
    <span class="lbl">双击</span>
    <select id="tk-double" onchange="updateArgVisibility()"></select>
    <input type="text" id="tk-macro-double" placeholder="宏，例: ctrl+shift+s">
    <input type="text" id="tk-program-double" placeholder="程序路径">
  </div>
  <div class="touch-row">
    <span class="lbl">长按</span>
    <select id="tk-long" onchange="updateArgVisibility()"></select>
    <input type="text" id="tk-macro-long" placeholder="宏，例: ctrl+shift+s">
    <input type="text" id="tk-program-long" placeholder="程序路径">
  </div>
  <div style="margin-top:8px">
    <button onclick="saveTouch()">应用触控</button>
    <span class="msg" id="msg-touch"></span>
  </div>
</div>

<div class="card">
  <h2>页面顺序</h2>
  <div class="plist" id="pages-list"></div>
  <div class="tiny">拖拽页面行可排序；双击页面名可立即切换到该页。</div>
  <div style="margin-top:.4em">
    <button onclick="loadPages()">刷新列表</button>
    <button onclick="post('/api/reload')">重载插件</button>
    <span class="msg" id="msg-pages"></span>
  </div>
</div>

<div class="card" id="config-panel" style="display:none">
  <h2 id="config-title">配置</h2>
  <div id="config-hint" class="hint"></div>
  <div id="config-body"></div>
  <div style="margin-top:.8em">
    <button onclick="saveConfig()">保存</button>
    <button onclick="closeConfig()">关闭</button>
    <span class="msg" id="msg-config"></span>
  </div>
</div>

<div class="card">
  <h2>背景图</h2>
  <div>
    <input type="text" id="bg-path" placeholder="path/to/image.gif|png|jpg" style="width:22em">
  </div>
  <div style="margin-top:4px">
    <textarea id="bg-paths" rows="3" style="width:100%"
      placeholder="多张背景：一行一个路径，每 12 秒轮播一张"></textarea>
  </div>
  <div style="margin-top:4px">
    <label style="font-size:12px;color:#666">显示方式
      <select id="bg-mode" onchange="setBackgroundMode()">
        <option value="fit">适应</option>
        <option value="fill">填充</option>
        <option value="stretch">拉伸</option>
      </select>
    </label>
    <label style="font-size:12px;color:#666;margin-left:12px">暗化
      <input type="range" id="bg-dim" min="0" max="90" step="5" value="0"
             oninput="setBgDimLabel()" onchange="applyBgLook()">
      <span id="bg-dim-v" class="tiny">0%</span>
    </label>
    <label style="font-size:12px;color:#666;margin-left:12px">模糊
      <input type="range" id="bg-blur" min="0" max="12" step="1" value="0"
             oninput="setBgBlurLabel()" onchange="applyBgLook()">
      <span id="bg-blur-v" class="tiny">0</span>
    </label>
  </div>
  <div style="margin-top:4px">
    <button id="btn-bg" onclick="toggleBackground()">启用背景</button>
    <span class="msg" id="msg-bg"></span>
  </div>
</div>

<div class="card">
  <h2>定时任务</h2>
  <div class="tiny">每行一条，格式 <code>HH:MM|动作</code>，动作可写 next_page / prev_page / flip / toggle_orientation / screenshot / mute_toggle / blank / shutdown</div>
  <textarea id="sched-text" rows="4" style="width:100%"></textarea>
  <div style="margin-top:4px">
    <button onclick="saveSchedules()">保存定时任务</button>
    <span class="msg" id="msg-sched"></span>
  </div>
</div>

<div class="card">
  <h2>状态</h2>
  <div class="tiny" id="metrics-line"></div>
  <pre id="status">加载中...</pre>
</div>

<div class="card shutdown-card">
  <h2>关闭程序</h2>
  <div style="color:#666; font-size:12px; margin-bottom:.6em">
    关闭后上位机会退出，WebUI 会断开。需要重新启动程序才能继续使用。
  </div>
  <button class="danger" onclick="shutdownApp()">关闭程序</button>
  <span class="msg" id="msg-shutdown"></span>
</div>

<script>
let currentPages = [];
let allWidgets = {};
let allBgEnabled = false;
let configWidgetName = '';
let currentWidgetName = '';

const TOUCH_ACTIONS = [
  ["passthrough", "未定义（透传给插件）"],
  ["next_page", "下一页"],
  ["prev_page", "上一页"],
  ["flip", "上下翻转"],
  ["toggle_orientation", "横竖屏切换"],
  ["lock", "锁定/解锁当前页"],
  ["screenshot", "截屏"],
  ["mute_toggle", "静音 / 解除"],
  ["macro", "自定义键盘宏"],
  ["launch", "自定义程序"],
  ["shutdown", "关闭程序"],
];

function fillTouchSelect(sel) {
  sel.innerHTML = '';
  TOUCH_ACTIONS.forEach(([v, l]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = l;
    sel.appendChild(o);
  });
}

function syncTouchSelects(t) {
  ['single', 'double', 'long'].forEach(k => {
    const sel = document.getElementById('tk-' + k);
    if (!sel || !sel.options.length) return;
    if (document.activeElement === sel) return;
    const v = t[k];
    if (v && sel.value !== v) sel.value = v;
  });
}

function updateArgVisibility() {
  const triples = [
    ['tk-single', 'tk-macro-single', 'tk-program-single'],
    ['tk-double', 'tk-macro-double', 'tk-program-double'],
    ['tk-long',   'tk-macro-long',   'tk-program-long'],
  ];
  triples.forEach(([sid, mid, pid]) => {
    const v = document.getElementById(sid).value;
    document.getElementById(mid).style.visibility =
      (v === 'macro') ? 'visible' : 'hidden';
    document.getElementById(pid).style.visibility =
      (v === 'launch') ? 'visible' : 'hidden';
  });
}

async function loadTouch() {
  try {
    const j = await getJSON('/api/touch_config');
    if (!j.ok) return;
    fillTouchSelect(document.getElementById('tk-single'));
    fillTouchSelect(document.getElementById('tk-double'));
    fillTouchSelect(document.getElementById('tk-long'));
    document.getElementById('tk-single').value = j.values.single;
    document.getElementById('tk-double').value = j.values.double;
    document.getElementById('tk-long').value = j.values.long;
    document.getElementById('tk-macro-single').value = j.values.macro_single || '';
    document.getElementById('tk-macro-double').value = j.values.macro_double || '';
    document.getElementById('tk-macro-long').value = j.values.macro_long || '';
    document.getElementById('tk-program-single').value = j.values.program_single || '';
    document.getElementById('tk-program-double').value = j.values.program_double || '';
    document.getElementById('tk-program-long').value = j.values.program_long || '';
    updateArgVisibility();
  } catch(e) {
    console.error(e);
  }
}

async function saveTouch() {
  const values = {
    single: document.getElementById('tk-single').value,
    double: document.getElementById('tk-double').value,
    long: document.getElementById('tk-long').value,
    macro_single: document.getElementById('tk-macro-single').value.trim(),
    macro_double: document.getElementById('tk-macro-double').value.trim(),
    macro_long: document.getElementById('tk-macro-long').value.trim(),
    program_single: document.getElementById('tk-program-single').value.trim(),
    program_double: document.getElementById('tk-program-double').value.trim(),
    program_long: document.getElementById('tk-program-long').value.trim(),
  };
  setMsg('msg-touch', '保存中...', false);
  try {
    const j = await getJSON('/api/touch_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({values})
    });
    if (j.ok) setMsg('msg-touch', '已应用', false);
    else setMsg('msg-touch', j.err || '保存失败', true);
  } catch(e) {
    setMsg('msg-touch', '失败: ' + e, true);
  }
}

async function getJSON(url, opts) {
  const r = await fetch(url, Object.assign({cache:'no-store'}, opts||{}));
  if(!r.ok) throw new Error(r.status + ' ' + r.statusText);
  return r.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  }[c]));
}

function setMsg(id, text, isErr) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.toggle('err', !!isErr);
}

async function loadDevices() {
  try {
    const j = await getJSON('/api/devices');
    const sel = document.getElementById('dev-select');
    const active = j.active || '';
    sel.innerHTML = '';
    (j.devices || []).forEach(d => {
      const o = document.createElement('option');
      o.value = d.id;
      o.textContent = `${d.id}  ${d.port || 'auto'}  ${d.connected ? '已连接' : '未连接'}  ${d.current || ''}`;
      sel.appendChild(o);
    });
    if (active) sel.value = active;
    const info = (j.devices || []).map(d =>
      `${d.id}: ${d.port || 'auto'} ${d.connected ? 'OK' : '--'} ${d.current || ''} (${d.pages} 页)`
    ).join('   ·   ');
    document.getElementById('dev-info').textContent = info;
  } catch(e) {}
}

async function selectDevice() {
  const id = document.getElementById('dev-select').value;
  const j = await postJSON('/api/select', { id });
  document.getElementById('msg-dev').textContent = j.ok ? '已切换' : (j.err || '失败');
  if (j.ok) {
    await refresh();
    await loadPages();
  }
}

async function sendNotify() {
  const text = document.getElementById('notify-text').value;
  const secs = parseInt(document.getElementById('notify-secs').value || '5', 10);
  const j = await postJSON('/api/notify', { text, secs });
  document.getElementById('msg-notify').textContent =
    j.ok ? '已推送' : (j.err || '失败');
}

function setBgDimLabel() {
  document.getElementById('bg-dim-v').textContent =
    document.getElementById('bg-dim').value + '%';
}

function setBgBlurLabel() {
  document.getElementById('bg-blur-v').textContent =
    document.getElementById('bg-blur').value;
}

async function applyBgLook() {
  const dim = parseInt(document.getElementById('bg-dim').value, 10);
  const blur = parseInt(document.getElementById('bg-blur').value, 10);
  const j = await postJSON('/api/background', {
    enabled: allBgEnabled,
    path: document.getElementById('bg-path').value.trim(),
    paths: bgPaths(),
    mode: document.getElementById('bg-mode').value,
    dim, blur,
  });
  document.getElementById('msg-bg').textContent =
    j.ok ? `暗化 ${j.dim}% 模糊 ${j.blur}` : (j.err || '失败');
}

function bgPaths() {
  return document.getElementById('bg-paths').value
    .split('\n').map(s => s.trim()).filter(s => s.length > 0);
}

async function loadSchedules() {
  try {
    const j = await getJSON('/api/schedules');
    const lines = (j.schedules || []).map(s => `${s.at}|${s.action}`);
    document.getElementById('sched-text').value = lines.join('\n');
  } catch(e) {}
}

async function saveSchedules() {
  const items = [];
  document.getElementById('sched-text').value.split('\n').forEach(line => {
    const t = line.trim();
    if (!t) return;
    const p = t.split('|');
    if (p.length < 2) return;
    items.push({ at: p[0].trim(), action: p[1].trim() });
  });
  const j = await postJSON('/api/schedules', { schedules: items });
  document.getElementById('msg-sched').textContent =
    j.ok ? `已保存 ${(j.schedules || []).length} 条` : (j.err || '失败');
}

async function refresh() {
  try {
    const j = await getJSON('/api/status');
    document.getElementById('status').textContent =
      JSON.stringify(j, null, 2);
    document.getElementById('btn-orient').textContent =
      j.orientation === 'portrait' ? '切换到横屏' : '切换到竖屏';
    document.getElementById('btn-rev').textContent =
      j.reverse ? '取消翻转' : '上下翻转';
    if (document.activeElement !== document.getElementById('bg-path')) {
      document.getElementById('bg-path').value = j.background_path || '';
    }
    const bgBtn = document.getElementById('btn-bg');
    bgBtn.textContent = j.background_enabled ? '关闭背景' : '启用背景';
    bgBtn.classList.toggle('active', !!j.background_enabled);
    allBgEnabled = !!j.background_enabled;
    const bgModeEl = document.getElementById('bg-mode');
    if (bgModeEl && document.activeElement !== bgModeEl) {
      bgModeEl.value = j.background_mode || 'fit';
    }
    const dimEl = document.getElementById('bg-dim');
    if (dimEl && document.activeElement !== dimEl) {
      dimEl.value = j.background_dim || 0;
      setBgDimLabel();
    }
    const blurEl = document.getElementById('bg-blur');
    if (blurEl && document.activeElement !== blurEl) {
      blurEl.value = j.background_blur || 0;
      setBgBlurLabel();
    }
    const pathsEl = document.getElementById('bg-paths');
    if (pathsEl && document.activeElement !== pathsEl) {
      const cur = (j.background_paths || []).join('\n');
      if (pathsEl.value.trim() !== cur.trim()) pathsEl.value = cur;
    }
    const sel = document.getElementById('dev-select');
    if (sel && j.active && document.activeElement !== sel) {
      sel.value = j.active;
    }
    const info = (j.devices || []).map(d =>
      `${d.id}: ${d.port || 'auto'} ${d.connected ? 'OK' : '--'} ${d.current || ''} (${d.pages} 页)`
    ).join('   ·   ');
    document.getElementById('dev-info').textContent = info;
    const m = j.metrics || {};
    if (m.fps !== undefined) {
      const rk = Object.keys(m.render_ms || {})
        .map(k => `${k} ${m.render_ms[k]}ms`).join('  ');
      document.getElementById('metrics-line').textContent =
        `FPS ${m.fps}   吞吐 ${m.bytes_per_sec} B/s   帧 ${m.frames}   ` +
        `重连 ${m.reconnects}   渲染 ${rk}`;
    }
    syncTouchSelects(j.touch || {});

    currentWidgetName = j.current_widget || '';
    document.querySelectorAll('#pages-list .pitem').forEach(el => {
      el.classList.toggle('current', el.dataset.widget === currentWidgetName);
    });
  } catch(e) {
    document.getElementById('status').textContent = '状态获取失败: ' + e;
  }
}

async function loadPages() {
  const j = await getJSON('/api/widgets');
  allWidgets = {};
  (j.widgets || []).forEach(w => { allWidgets[w.name] = w; });
  currentPages = (j.active || []).slice();
  renderPages();
}

function renderPages() {
  const box = document.getElementById('pages-list');
  closeActMenu();
  box.innerHTML = '';

  if (currentPages.length === 0) {
    const e = document.createElement('div');
    e.className = 'pitem empty';
    e.textContent = '未启用任何页面';
    box.appendChild(e);
  }

  currentPages.forEach((name, i) => {
    const w = allWidgets[name] || { orientations: [], has_config: false, actions: {} };
    const row = document.createElement('div');
    row.className = 'pitem';
    row.dataset.widget = name;
    row.draggable = true;
    if (name === currentWidgetName) row.classList.add('current');

    row.addEventListener('dragstart', ev => {
      dragIndex = i;
      ev.dataTransfer.effectAllowed = 'move';
      try { ev.dataTransfer.setData('text/plain', name); } catch(e) {}
    });
    row.addEventListener('dragend', () => { dragIndex = null; });
    row.addEventListener('dragover', ev => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = 'move';
      if (dragIndex !== null && dragIndex !== i) row.style.borderTop = '2px solid #2b5fd9';
    });
    row.addEventListener('dragleave', () => { row.style.borderTop = ''; });
    row.addEventListener('drop', ev => {
      ev.preventDefault();
      row.style.borderTop = '';
      dragMove(i);
    });
    row.addEventListener('dblclick', ev => {
      if (ev.target.closest('button')) return;
      switchTo(name);
    });

    const cfgBtn = '';
    const actCount = (w.has_config ? 1 : 0) +
                     Object.keys(w.actions || {}).length;
    const actBtn = actCount > 0
      ? `<button class="mini actbtn" title="插件动作" ` +
        `onclick="toggleActMenu(event,'${esc(name)}')">⋯</button>`
      : '';

    const nameTag = w.source === 'external'
      ? `<span class="name" title="双击切换到该页">${esc(name)}</span>` +
        `<span class="tag-ext" title="外挂插件">外挂</span>`
      : `<span class="name" title="双击切换到该页">${esc(name)}</span>`;

    row.innerHTML =
      `<button class="mini" onclick="moveUp(${i})" ${i===0?'disabled':''}>↑</button>` +
      `<button class="mini" onclick="moveDown(${i})" ${i===currentPages.length-1?'disabled':''}>↓</button>` +
      `<button class="mini" onclick="removeAt(${i})">×</button>` +
      `<img class="thumb" src="/api/preview?name=${encodeURIComponent(name)}&t=${Date.now()}" alt="">` +
      cfgBtn +
      nameTag +
      actBtn +
      `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
    box.appendChild(row);
  });

  const disabled = Object.keys(allWidgets).filter(n => !currentPages.includes(n));
  if (disabled.length > 0) {
    const sep = document.createElement('div');
    sep.className = 'pitem';
    sep.style.borderTop = '1px dashed #ccc';
    sep.style.color = '#999';
    sep.style.fontSize = '11px';
    sep.textContent = '已停用';
    box.appendChild(sep);
    disabled.forEach(name => {
      const w = allWidgets[name] || { orientations: [] };
      const row = document.createElement('div');
      row.className = 'pitem';
      row.style.opacity = '0.6';
      row.innerHTML =
        `<button class="mini" onclick="addWidget('${esc(name)}')">+</button>` +
        `<span class="name">${esc(name)}</span>` +
        `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
      box.appendChild(row);
    });
  }
}

let actMenu = null;
let dragIndex = null;

function closeActMenu() {
  if (!actMenu) return;
  actMenu.remove();
  actMenu = null;
}

function placeActMenu(box, btn) {
  const r = btn.getBoundingClientRect();
  const bw = box.offsetWidth, bh = box.offsetHeight;
  const sx = window.scrollX || window.pageXOffset || 0;
  const sy = window.scrollY || window.pageYOffset || 0;
  let left = r.right + sx + 6;
  if (left + bw > sx + window.innerWidth - 8) left = r.left + sx - bw - 6;
  if (left < sx + 8) left = sx + Math.max(8, window.innerWidth - bw - 8);
  let top = r.top + sy + r.height / 2 - bh / 2;
  if (top < sy + 8) top = sy + 8;
  box.style.left = left + 'px';
  box.style.top = top + 'px';
}

function toggleActMenu(ev, name) {
  ev.stopPropagation();
  if (ev.preventDefault) ev.preventDefault();
  const btn = ev.currentTarget;
  const same = actMenu && actMenu.dataset.widget === name;
  closeActMenu();
  if (same) return;
  const w = allWidgets[name] || {};
  const entries = [];
  if (w.has_config) entries.push(['__cfg__', '设置']);
  Object.entries(w.actions || {}).forEach(([a, label]) => entries.push([a, label]));
  if (!entries.length) return;
  const box = document.createElement('div');
  box.className = 'actpop';
  box.dataset.widget = name;
  const ttl = document.createElement('div');
  ttl.className = 'ttl';
  ttl.textContent = name;
  box.appendChild(ttl);
  entries.forEach(([act, label]) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => {
      if (act === '__cfg__') openConfig(name); else doAction(name, act);
    };
    box.appendChild(b);
  });
  document.body.appendChild(box);
  placeActMenu(box, btn);
  actMenu = box;
}

async function pushPages(newPages) {
  try {
    const j = await getJSON('/api/pages', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pages: newPages})
    });
    if (j.ok) {
      currentPages = j.pages.slice();
      renderPages();
      setMsg('msg-pages', '已更新', false);
    } else {
      setMsg('msg-pages', j.err || '被拒绝', true);
    }
  } catch(e) {
    setMsg('msg-pages', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

function moveUp(i) {
  if (i <= 0) return;
  const p = currentPages.slice();
  [p[i-1], p[i]] = [p[i], p[i-1]];
  pushPages(p);
}
function moveDown(i) {
  if (i >= currentPages.length - 1) return;
  const p = currentPages.slice();
  [p[i+1], p[i]] = [p[i], p[i+1]];
  pushPages(p);
}
function removeAt(i) {
  const p = currentPages.slice();
  p.splice(i, 1);
  pushPages(p);
}
function addWidget(name) {
  const p = currentPages.slice();
  p.push(name);
  pushPages(p);
}
function dragMove(target) {
  if (dragIndex === null || target === null) return;
  if (dragIndex === target) return;
  if (dragIndex < 0 || dragIndex >= currentPages.length) return;
  const p = currentPages.slice();
  const [item] = p.splice(dragIndex, 1);
  p.splice(target, 0, item);
  dragIndex = null;
  pushPages(p);
}
async function switchTo(name) {
  try {
    const j = await getJSON('/api/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name})
    });
    if (j.ok) setMsg('msg-pages', '已切换到 ' + name, false);
    else setMsg('msg-pages', j.err || '切换失败', true);
  } catch(e) {
    setMsg('msg-pages', '切换失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}
async function setBackgroundMode() {
  const modeEl = document.getElementById('bg-mode');
  const mode = modeEl ? modeEl.value : 'fit';
  const path = (document.getElementById('bg-path') || {}).value || '';
  const enabled = !!allBgEnabled;
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled, path: path.trim(), mode: mode})
    });
    if (j.ok) setMsg('msg-bg', '显示方式已更新', false);
    else setMsg('msg-bg', j.err || '被拒绝', true);
  } catch(e) {
    setMsg('msg-bg', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

async function doAction(name, action) {
  setMsg('msg-pages', '执行中...', false);
  try {
    const j = await getJSON('/api/widget_action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, action, payload: {}})
    });
    if (j.ok) setMsg('msg-pages', '已执行', false);
    else setMsg('msg-pages', j.err || '失败', true);
  } catch(e) {
    setMsg('msg-pages', '失败: ' + e, true);
  }
  setTimeout(refresh, 200);
}

async function openConfig(name) {
  try {
    const j = await getJSON(`/api/widget_config?name=${encodeURIComponent(name)}`);
    if (!j.ok) { alert(j.err || '加载失败'); return; }
    configWidgetName = name;

    document.getElementById('config-title').textContent = `配置: ${name}`;
    const hint = document.getElementById('config-hint');
    if (hint) {
      const parts = [];
      if (j.touch_help) parts.push('透传触控：' + j.touch_help);
      hint.textContent = parts.length ? parts.join('　|　') : '';
      hint.style.display = parts.length ? 'block' : 'none';
    }
    const box = document.getElementById('config-body');
    box.innerHTML = '';

    if (!j.schema || j.schema.length === 0) {
      box.innerHTML = '<div style="color:#999">此插件暂无配置项</div>';
      document.getElementById('config-panel').style.display = 'block';
      return;
    }

    j.schema.forEach(f => {
      const v = j.values[f.key];
      const div = document.createElement('div');
      div.className = 'field';

      const lbl = document.createElement('label');
      lbl.textContent = f.label;
      if (f.help) {
        const h = document.createElement('span');
        h.className = 'help';
        h.textContent = '(' + f.help + ')';
        lbl.appendChild(h);
      }
      div.appendChild(lbl);

      let input;
      if (f.type === 'bool') {
        input = document.createElement('input');
        input.type = 'checkbox'; input.checked = !!v;
      } else if (f.type === 'select') {
        input = document.createElement('select');
        (f.options || []).forEach(o => {
          const opt = document.createElement('option');
          opt.value = o; opt.textContent = o;
          if (o === v) opt.selected = true;
          input.appendChild(opt);
        });
      } else if (f.type === 'list_str') {
        input = document.createElement('textarea');
        input.rows = 4;
        input.value = (Array.isArray(v) ? v : []).join('\n');
      } else if (f.type === 'int' || f.type === 'float') {
        input = document.createElement('input');
        input.type = 'number';
        input.value = v ?? '';
        if (f.min != null) input.min = f.min;
        if (f.max != null) input.max = f.max;
        if (f.step != null) input.step = f.step;
      } else {
        input = document.createElement('input');
        input.type = 'text';
        input.value = v ?? '';
      }
      input.dataset.key = f.key;
      input.dataset.type = f.type;
      div.appendChild(input);
      box.appendChild(div);
    });

    document.getElementById('config-panel').style.display = 'block';
    document.getElementById('config-panel').scrollIntoView({
      behavior: 'smooth', block: 'start' });
    setMsg('msg-config', '', false);
  } catch(e) {
    alert('加载配置失败: ' + e);
  }
}

async function saveConfig() {
  if (!configWidgetName) return;
  const box = document.getElementById('config-body');
  const values = {};
  box.querySelectorAll('input,select,textarea').forEach(el => {
    const k = el.dataset.key;
    const t = el.dataset.type;
    if (t === 'bool') values[k] = el.checked;
    else if (t === 'int') values[k] = parseInt(el.value || 0, 10);
    else if (t === 'float') values[k] = parseFloat(el.value || 0);
    else if (t === 'list_str') {
      values[k] = el.value.split('\n').map(s => s.trim()).filter(Boolean);
    } else {
      values[k] = el.value;
    }
  });
  setMsg('msg-config', '保存中...', false);
  try {
    const j = await getJSON('/api/widget_config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: configWidgetName, values})
    });
    if (j.ok) setMsg('msg-config', '已保存', false);
    else setMsg('msg-config', j.err || '保存失败', true);
  } catch(e) {
    setMsg('msg-config', '失败: ' + e, true);
  }
}

function closeConfig() {
  document.getElementById('config-panel').style.display = 'none';
}

async function postJSON(path, body) {
  try {
    return await getJSON(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
  } catch(e) {
    return {ok: false, err: String(e)};
  }
}

async function post(path) {
  try {
    const j = await getJSON(path, {method:'POST'});
    if (j.ok === false) setMsg('msg-display', j.err || '拒绝', true);
    else setMsg('msg-display', '已应用', false);
  } catch(e) {
    setMsg('msg-display', '失败: ' + e, true);
  }
  setTimeout(refresh, 250);
}

async function toggleBackground() {
  const btn = document.getElementById('btn-bg');
  const currentlyOn = btn.classList.contains('active');
  const next = !currentlyOn;
  const path = document.getElementById('bg-path').value.trim();
  const modeEl = document.getElementById('bg-mode');
  const mode = modeEl ? modeEl.value : 'fit';
  const paths = bgPaths();
  const dim = parseInt(document.getElementById('bg-dim').value, 10);
  const blur = parseInt(document.getElementById('bg-blur').value, 10);

  if (next && !path && paths.length === 0) {
    setMsg('msg-bg', '请先填写图片路径', true);
    return;
  }
  setMsg('msg-bg', '应用中...', false);
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: next, path, paths, mode, dim, blur})
    });
    if (j.ok) setMsg('msg-bg', next ? '已启用' : '已关闭', false);
    else setMsg('msg-bg', j.err || '被拒绝', true);
  } catch(e) {
    setMsg('msg-bg', '失败: ' + e, true);
  }
  setTimeout(refresh, 250);
}

async function shutdownApp() {
  if (!confirm('确定要关闭 MSU2_MINI 上位机吗？\n关闭后需要重新启动才能继续使用。')) {
    return;
  }
  setMsg('msg-shutdown', '正在关闭...', false);
  try {
    const r = await fetch('/api/shutdown', {method:'POST'});
    const j = await r.json();
    if (j.ok) {
      setMsg('msg-shutdown', '已发送关闭指令，窗口即将失效', false);
      setTimeout(() => {
        document.body.innerHTML =
          '<div style="text-align:center; margin-top:8em; color:#666;' +
          ' font-family:Consolas,monospace">' +
          '<h1 style="color:#a11">程序已关闭</h1>' +
          '<p>可以关闭此页面，或重新启动 MSU2_MINI 后再刷新。</p></div>';
      }, 800);
    } else {
      setMsg('msg-shutdown', j.err || '失败', true);
    }
  } catch(e) {
    setMsg('msg-shutdown', '程序已关闭', false);
    setTimeout(() => {
      document.body.innerHTML =
        '<div style="text-align:center; margin-top:8em; color:#666;' +
        ' font-family:Consolas,monospace">' +
        '<h1 style="color:#a11">程序已关闭</h1>' +
        '<p>可以关闭此页面，或重新启动 MSU2_MINI 后再刷新。</p></div>';
    }, 800);
  }
}

loadTouch();
loadDevices();
loadSchedules();
loadPages();
refresh();
setInterval(refresh, 1000);
setInterval(loadDevices, 5000);
</script>
</body>
</html>
"""

def _check_path(path, exts, max_bytes):
    try:
        p = Path(str(path)).expanduser()
    except Exception:
        return False, "路径无效"
    try:
        if not p.exists():
            return True, ""
        if p.is_dir():
            for f in list(p.iterdir())[:200]:
                if f.is_file() and f.suffix.lower() not in exts:
                    return False, "目录含不支持的文件: %s" % f.name
            return True, ""
        if p.suffix.lower() not in exts:
            return False, "不支持的扩展名: %s" % (p.suffix or "无")
        if p.stat().st_size > max_bytes:
            return False, "文件过大: %s" % p.name
    except Exception:
        return False, "无法读取文件"
    return True, ""

def _check_widget_values(schema, values):
    for f in schema:
        key = getattr(f, "key", None)
        if not isinstance(key, str) or not isinstance(values, dict):
            continue
        if not (key == "path" or key.endswith("_path") or key.endswith("_dir")):
            continue
        v = values.get(key)
        if not isinstance(v, str) or not v:
            continue
        if key.endswith("_dir") or Path(v).suffix.lower() in IMAGE_EXTS:
            ok, err = _check_path(v, IMAGE_EXTS, MAX_RESOURCE_BYTES)
        else:
            ok, err = _check_path(v, TEXT_EXTS + IMAGE_EXTS, MAX_TEXT_BYTES)
        if not ok:
            label = getattr(f, "label", "") or key
            return False, "%s: %s" % (label, err)
    return True, ""

def _pick_free_port(used):
    try:
        ports = [p.device for p in serial.tools.list_ports.comports()]
    except Exception:
        return ""
    for name in ports:
        if name in used:
            continue
        probe = MiniDevice(name)
        ok = False
        try:
            ok = probe.open()
        except Exception:
            ok = False
        finally:
            probe.close()
        if ok:
            return name
    return ""

class OfflineDevice:
    def __init__(self):
        self.port = "offline"
        self.connected = True
        self.last = None
        self.frames = 0
        self.transport = type("T", (), {"bytes_written": 0})()

    def open(self):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    def show_image(self, img):
        self.last = img.copy()
        self.frames += 1
        self.transport.bytes_written += img.size[0] * img.size[1] * 2
        return True

    def set_reverse(self, reverse):
        pass

    def read_touch(self):
        return 0

    def heartbeat(self):
        return True

class DeviceManager:
    def __init__(self, cfg, cfg_path, offline=False):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.offline = bool(offline)
        self.lock = threading.RLock()
        self.schedulers = {}
        self.order = []
        self.active_id = None
        self._build()

    def _build(self):
        items = self.cfg.get("devices") or []
        used = set()
        for i, dev in enumerate(items):
            if not isinstance(dev, dict):
                continue
            did = str(dev.get("id") or ("dev%d" % (i + 1)))
            port = str(dev.get("port") or "auto")
            if port.lower() in ("auto", ""):
                port = _pick_free_port(used) or ""
            if port:
                used.add(port)
            device = OfflineDevice() if self.offline else MiniDevice(port or "auto")
            sched = Scheduler(device, dev, on_config_changed=self._persist,
                              shared=self.cfg)
            self.schedulers[did] = sched
            self.order.append(did)
        if self.order:
            self.active_id = self.order[0]

    def _persist(self):
        try:
            save_config(self.cfg, self.cfg_path)
        except Exception as e:
            log.warning("保存配置失败: %s", e)

    def start(self):
        for did in self.order:
            self.schedulers[did].start()

    def stop(self):
        for did in self.order:
            self.schedulers[did].stop()
        for did in self.order:
            try:
                self.schedulers[did].join(timeout=3.0)
            except Exception:
                pass

    def active(self):
        with self.lock:
            return self.schedulers.get(self.active_id)

    def select(self, did):
        with self.lock:
            if did not in self.schedulers:
                return False
            self.active_id = did
            return True

    def describe(self):
        out = []
        for did in self.order:
            s = self.schedulers[did]
            widgets = s._widgets
            cur = s._current
            name = widgets[cur].name if 0 <= cur < len(widgets) else None
            out.append({"id": did, "port": s.device.port,
                        "connected": s.device.connected,
                        "current": name, "pages": len(widgets)})
        return out

class WebUI(threading.Thread):
    def __init__(self, manager, cfg, cfg_path, on_shutdown=None):
        super().__init__(daemon=True, name="WebUI")
        self.manager = manager
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.on_shutdown = on_shutdown
        web = cfg.get("web")
        if not isinstance(web, dict):
            web = {}
        self.host = web.get("host") or "0.0.0.0"
        self.port = _coerce_int(web.get("port"), 8765, 1, 65535)
        self._stop = threading.Event()

    @property
    def scheduler(self):
        return self.manager.active() if self.manager else None

    def stop(self):
        self._stop.set()

    def run(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        cfg_path = self.cfg_path
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, body, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj):
                self._send(json.dumps(obj, ensure_ascii=False).encode(),
                           "application/json")

            def _html(self, text):
                self._send(text.encode(), "text/html; charset=utf-8")

            def _read_json(self):
                n = _coerce_int(self.headers.get("Content-Length"), 0, 0)
                if n <= 0: return {}
                if n > MAX_REQUEST_BYTES:
                    raise ValueError("请求体过大: %d 字节" % n)
                try:
                    self.connection.settimeout(5.0)
                    raw = self.rfile.read(n)
                except Exception:
                    return {}
                if len(raw) != n: return {}
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}
                return obj if isinstance(obj, dict) else {}

            def _dev(self):
                sched = outer.manager.active()
                if sched is None:
                    return None, {}
                return sched, sched.config

            def _preview(self, name):
                cls = WIDGET_REGISTRY.get(name)
                if not cls:
                    return None
                sched, dcfg = self._dev()
                orientation = sched.orientation if sched else LANDSCAPE
                size = PORTRAIT_SIZE if orientation == PORTRAIT else LANDSCAPE_SIZE
                ctx = RenderContext(device=None, now=time.time(),
                                    orientation=orientation, state={},
                                    config=dict(outer.cfg or {}))
                inst = None
                try:
                    for w in (sched._widgets if sched else []):
                        if w.name == name:
                            inst = w
                            ctx = sched._ctx_for(w)
                            break
                    own = inst is None
                    if own:
                        inst = cls()
                        inst.setup(ctx)
                    ctx.now = time.time()
                    ctx.orientation = orientation
                    img = inst.render(ctx)
                except Exception as e:
                    log.warning("缩略图渲染失败 %s: %s", name, e)
                    img = None
                finally:
                    if inst is not None and own:
                        try:
                            inst.teardown(ctx)
                        except Exception:
                            pass
                if img is None:
                    img = Image.new("RGBA", size, (0, 0, 0, 255))
                if img.size != size:
                    img = img.resize(size, Image.LANCZOS)
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                bg = Image.new("RGBA", size, (0, 0, 0, 255))
                bg.alpha_composite(img)
                buf = io.BytesIO()
                bg.convert("RGB").save(buf, format="PNG")
                return buf.getvalue()

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path

                if path in ("/", "/index.html"):
                    self._html(INDEX_HTML); return

                if path == "/api/devices":
                    self._json({"ok": True,
                                "active": outer.manager.active_id,
                                "devices": outer.manager.describe()}); return

                sched, dcfg = self._dev()

                if path == "/api/status":
                    if sched is None:
                        self._json({"connected": False, "port": "",
                                    "current_widget": None, "widgets": [],
                                    "orientation": LANDSCAPE, "reverse": False,
                                    "locked": False, "active": None,
                                    "devices": [], "touch": dict(_TOUCH_DEFAULTS)})
                        return
                    widgets = sched._widgets
                    cur = sched._current
                    cur_name = widgets[cur].name if 0 <= cur < len(widgets) else None
                    self._json({
                        "connected": sched.device.connected,
                        "port": sched.device.port,
                        "device": str(dcfg.get("id") or ""),
                        "active": outer.manager.active_id,
                        "devices": outer.manager.describe(),
                        "current_widget": cur_name,
                        "widgets": [w.name for w in widgets],
                        "orientation": sched.orientation,
                        "reverse": sched.reverse,
                        "locked": sched._locked,
                        "background_enabled": sched._bg_enabled,
                        "background_path": sched._bg_path,
                        "background_paths": list(sched._bg_paths),
                        "background_mode": sched._bg_mode,
                        "background_dim": sched._bg_dim,
                        "background_blur": sched._bg_blur,
                        "touch": dict(sched._touch_cfg),
                        "metrics": sched.metrics_snapshot(),
                    }); return

                if path == "/api/metrics":
                    if sched is None:
                        self._json({"ok": False, "err": "没有可用设备"}); return
                    data = sched.metrics_snapshot()
                    data["ok"] = True
                    data["port"] = sched.device.port
                    self._json(data); return

                if path == "/api/schedules":
                    self._json({"ok": True,
                                "schedules": list(outer.cfg.get("schedules") or [])})
                    return

                if path == "/api/preview":
                    q = parse_qs(parsed.query)
                    name = (q.get("name") or [""])[0]
                    data = self._preview(name)
                    if data is None:
                        self.send_error(404); return
                    self._send(data, "image/png"); return

                if path == "/api/widgets":
                    self._json({
                        "ok": True,
                        "widgets": [
                            {"name": n,
                             "orientations": list(c.orientations),
                             "interval": c.interval,
                             "priority": c.priority,
                             "has_config": bool(getattr(c, "config_schema", [])),
                             "actions": dict(getattr(c, "actions", {}) or {}),
                             "touch_help": str(getattr(c, "touch_help", "") or ""),
                             "source": "external" if str(getattr(c, "__module__", "")).startswith("msu2_ext_widgets") else "builtin"}
                            for n, c in sorted(WIDGET_REGISTRY.items())
                        ],
                        "active": list(dcfg.get("pages") or []),
                    }); return

                if path == "/api/widget_config":
                    q = parse_qs(parsed.query)
                    name = (q.get("name") or [""])[0]
                    cls = WIDGET_REGISTRY.get(name)
                    if not cls:
                        self._json({"ok": False, "err": "未知插件"}); return
                    obj = cls
                    for w in (sched._widgets if sched else []):
                        if w.name == name:
                            obj = w
                            break
                    schema = getattr(obj, "config_schema", [])
                    user = outer.cfg.get("widget_config", {}).get(name, {})
                    values = merge_config(schema, user)
                    self._json({
                        "ok": True, "name": name,
                        "schema": [f.to_dict() for f in schema],
                        "values": values,
                        "touch_help": str(getattr(obj, "touch_help", "") or ""),
                        "actions": dict(getattr(obj, "actions", {}) or {}),
                    }); return

                if path == "/api/touch_config":
                    tk = dcfg.get("touch", {}) or {}
                    vals = {}
                    for k, dv in _TOUCH_DEFAULTS.items():
                        vals[k] = tk.get(k, dv)
                    self._json({"ok": True, "values": vals}); return

                self.send_error(404)

            def do_POST(self):
                try:
                    sched, dcfg = self._dev()
                    if self.path == "/api/select":
                        body = self._read_json()
                        did = str(body.get("id", ""))
                        ok = outer.manager.select(did)
                        self._json({"ok": ok, "active": outer.manager.active_id,
                                    "err": "" if ok else "设备不存在"})
                    elif self.path == "/api/schedules":
                        body = self._read_json()
                        items = body.get("schedules", [])
                        outer.cfg["schedules"] = _sanitize_schedules(items)
                        save_config(outer.cfg, cfg_path)
                        for s in outer.manager.schedulers.values():
                            s._schedule_seen.clear()
                        self._json({"ok": True,
                                    "schedules": outer.cfg["schedules"]})
                    elif sched is None:
                        self._json({"ok": False, "err": "没有可用设备"})
                    elif self.path == "/api/next":
                        ok = sched.next_page()
                        self._json({"ok": ok, "err": "" if ok else "冷却中"})
                    elif self.path == "/api/prev":
                        ok = sched.prev_page()
                        self._json({"ok": ok, "err": "" if ok else "冷却中"})
                    elif self.path == "/api/reload":
                        sched.reload(); self._json({"ok": True})
                    elif self.path == "/api/notify":
                        body = self._read_json()
                        text = str(body.get("text", ""))
                        secs = body.get("secs", 5)
                        ok = sched.notify(text, secs)
                        self._json({"ok": ok, "err": "" if ok else "内容为空"})
                    elif self.path == "/api/reverse":
                        ok = sched.toggle_reverse()
                        if ok:
                            dcfg["reverse"] = sched.reverse
                            save_config(outer.cfg, cfg_path)
                        self._json({"ok": ok, "reverse": sched.reverse,
                                    "err": "" if ok else "冷却中"})
                    elif self.path == "/api/orientation":
                        ok = sched.toggle_orientation()
                        if ok:
                            dcfg["orientation"] = sched.orientation
                            save_config(outer.cfg, cfg_path)
                        self._json({"ok": ok, "orientation": sched.orientation,
                                    "err": "" if ok else "冷却中"})
                    elif self.path == "/api/pages":
                        body = self._read_json()
                        pages = [p for p in body.get("pages", [])
                                 if p in WIDGET_REGISTRY]
                        applied = sched.set_pages(pages)
                        dcfg["pages"] = applied
                        save_config(outer.cfg, cfg_path)
                        self._json({"ok": True, "pages": applied, "err": ""})
                    elif self.path == "/api/background":
                        body = self._read_json()
                        enabled = bool(body.get("enabled", False))
                        path = str(body.get("path", "") or "")
                        mode = body.get("mode")
                        if mode not in Background.MODES:
                            mode = None
                        paths = body.get("paths")
                        if paths is not None and not isinstance(paths, list):
                            paths = None
                        if paths is not None:
                            paths = [str(p) for p in paths if isinstance(p, str)]
                        dim = body.get("dim")
                        blur = body.get("blur")
                        bg = dcfg.get("background")
                        if not isinstance(bg, dict):
                            bg = {}
                            dcfg["background"] = bg
                        if not enabled and not path and not paths:
                            path = str(bg.get("path", "") or "")
                        if path:
                            vok, verr = _check_path(path, IMAGE_EXTS,
                                                    MAX_RESOURCE_BYTES)
                            if not vok:
                                self._json({"ok": False, "err": verr}); return
                        for item in paths or []:
                            vok, verr = _check_path(item, IMAGE_EXTS,
                                                    MAX_RESOURCE_BYTES)
                            if not vok:
                                self._json({"ok": False, "err": verr}); return
                        sched.set_background(enabled, path, mode, paths, dim, blur)
                        bg["enabled"] = enabled
                        bg["path"] = path
                        if paths is not None:
                            bg["paths"] = paths
                        if mode:
                            bg["mode"] = mode
                        bg["dim"] = sched._bg_dim
                        bg["blur"] = sched._bg_blur
                        save_config(outer.cfg, cfg_path)
                        self._json({"ok": True, "enabled": enabled, "path": path,
                                    "paths": list(sched._bg_paths),
                                    "mode": bg.get("mode", "fit"),
                                    "dim": sched._bg_dim,
                                    "blur": sched._bg_blur})
                    elif self.path == "/api/switch":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        ok = sched.switch_to(name)
                        self._json({"ok": ok, "current": name,
                                    "err": "" if ok else "插件未启用"})
                    elif self.path == "/api/widget_config":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        values = body.get("values", {}) or {}
                        cls = WIDGET_REGISTRY.get(name)
                        if not cls:
                            self._json({"ok": False, "err": "未知插件"}); return
                        live = sched.find_widget(name)
                        schema = getattr(live or cls, "config_schema", [])
                        if not isinstance(values, dict):
                            values = {}
                        merged = merge_config(schema, values)
                        vok, verr = _check_widget_values(schema, merged)
                        if not vok:
                            self._json({"ok": False, "err": verr}); return
                        outer.cfg.setdefault("widget_config", {})
                        outer.cfg["widget_config"][name] = merged
                        save_config(outer.cfg, cfg_path)
                        for s in outer.manager.schedulers.values():
                            s.restart_widget(name)
                        self._json({"ok": True, "values": merged})
                    elif self.path == "/api/widget_action":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        action = str(body.get("action", ""))
                        payload = body.get("payload", {}) or {}
                        w = sched.find_widget(name)
                        if not w:
                            self._json({"ok": False,
                                        "err": "插件未启用"}); return
                        try:
                            result = w.on_action(action, payload, sched._ctx_for(w))
                            if not isinstance(result, dict):
                                result = {"ok": True}
                            if result.get("ok"):
                                sched._switch.set()
                            self._json(result)
                        except Exception as e:
                            log.exception("插件 %s on_action 异常", name)
                            self._json({"ok": False, "err": str(e)})
                    elif self.path == "/api/touch_config":
                        body = self._read_json()
                        values = body.get("values", {}) or {}
                        try:
                            sched.set_touch_config(values)
                            save_config(outer.cfg, cfg_path)
                            self._json({"ok": True, "values": sched._touch_cfg})
                        except Exception as e:
                            log.exception("保存触控配置失败")
                            self._json({"ok": False, "err": str(e)})
                    elif self.path == "/api/shutdown":
                        self._json({"ok": True})
                        log.info("收到 Web 关闭指令")
                        if callable(outer.on_shutdown):
                            threading.Timer(
                                0.3, outer.on_shutdown
                            ).start()
                    else:
                        self.send_error(404)
                except Exception as e:
                    log.exception("POST %s 异常", self.path)
                    self._json({"ok": False, "err": str(e)})

        try:
            srv = ThreadingHTTPServer((self.host, self.port), Handler)
            srv.daemon_threads = True
            log.info("Web UI 已启动: http://%s:%d", self.host, self.port)

            def watch():
                while not self._stop.is_set():
                    time.sleep(0.2)
                srv.shutdown()

            threading.Thread(target=watch, daemon=True).start()
            srv.serve_forever(poll_interval=0.3)
            srv.server_close()
        except Exception as e:
            log.warning("Web UI 启动失败: %s", e)

_NEW_WIDGET_TEMPLATE = '''from PIL import Image, ImageDraw

from msu2_core import BOTH, Field, Widget, get_font


class {cls}(Widget):
    name = "{name}"
    interval = 1.0
    orientations = BOTH
    priority = 200

    actions = {{}}
    touch_help = ""

    config_schema = [
        Field("title", "str", "标题", default="{name}"),
    ]

    def render(self, ctx):
        cfg = self.get_config(ctx)
        w, h = ctx.size
        img = Image.new("RGBA", (w, h), (18, 20, 26, 255))
        d = ImageDraw.Draw(img)
        f1 = get_font(12, bold=True, cjk=True)
        f2 = get_font(9, cjk=True)
        d.text((8, h // 2 - 12), str(cfg.get("title") or self.name),
               font=f1, fill=(240, 244, 255, 255))
        d.text((8, h // 2 + 4), "%.1f" % ctx.now, font=f2,
               fill=(140, 160, 200, 255))
        return img
'''


def _widget_class_name(name):
    parts = [p for p in str(name).replace("-", "_").split("_") if p]
    base = "".join(p[:1].upper() + p[1:] for p in parts) or "New"
    if base.endswith("Widget"):
        return base
    return base + "Widget"


def cmd_new_widget(name):
    raw = str(name or "").strip()
    if not raw:
        _console("请提供一个插件名，例如 --new-widget my_clock")
        return 1
    slug = "".join(c if (c.isalnum() or c == "_") else "_" for c in raw).strip("_")
    if not slug:
        _console("插件名只能包含字母、数字和下划线")
        return 1
    if slug[0].isdigit():
        slug = "w_" + slug
    ext_dirs = []
    try:
        ext_dirs = list(getattr(_widgets_pkg, "_ext_dirs", []) or [])
    except Exception:
        ext_dirs = []
    target_dir = Path(ext_dirs[0]) if ext_dirs else (Path(_APP_DIR) / "widgets_ext")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _console("无法创建目录 %s: %s" % (target_dir, e))
        return 1
    path = target_dir / (slug + ".py")
    if path.exists():
        _console("文件已存在: %s" % path)
        return 1
    body = _NEW_WIDGET_TEMPLATE.format(cls=_widget_class_name(slug), name=slug)
    try:
        path.write_text(body, encoding="utf-8")
    except Exception as e:
        _console("写入失败: %s" % e)
        return 1
    _console("已生成插件: %s" % path)
    _console("放到该目录后重启程序（或等 2 秒热重载），即可在 Web 页面的“已停用”里找到它")
    return 0


def _start_hotkeys(manager):
    try:
        from pynput import keyboard
    except Exception as e:
        log.warning("全局热键不可用（缺少 pynput）: %s", e)
        return None

    def act(fn):
        def _run():
            try:
                fn()
            except Exception as e:
                log.warning("热键动作失败: %s", e)
        return _run

    def _sched():
        return manager.active()

    def _next():
        s = _sched()
        if s: s.next_page()

    def _prev():
        s = _sched()
        if s: s.prev_page()

    def _shot():
        threading.Thread(target=_act_screenshot, daemon=True).start()

    def _notify():
        s = _sched()
        if s: s.notify("热键测试", 3)

    try:
        hk = keyboard.GlobalHotKeys({
            "<ctrl>+<alt>+n": act(_next),
            "<ctrl>+<alt>+p": act(_prev),
            "<ctrl>+<alt>+s": act(_shot),
            "<ctrl>+<alt>+m": act(_notify),
        })
        hk.daemon = True
        hk.start()
        log.info("全局热键已启用: Ctrl+Alt+N 下一页 / P 上一页 / S 截图 / M 通知")
        return hk
    except Exception as e:
        log.warning("启动全局热键失败: %s", e)
        return None


def cmd_list():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("未发现串口。"); return
    for p in ports:
        print(f"{p.device:<30} {p.description}")

def cmd_plugins():
    if not WIDGET_REGISTRY:
        print("未发现任何插件。")
        return
    try:
        ext = _widgets_pkg.external_report()
    except Exception:
        ext = {}
    for folder in ext.get("dirs") or []:
        print(f"外挂插件目录: {folder}")
    if ext.get("failed"):
        print(f"外挂插件加载失败 {len(ext['failed'])} 个: {ext['failed']}")
    print(f"{'NAME':<18} {'ORI':<22} {'PRI':<5} {'SRC':<6} {'DESC'}")
    print("-" * 86)
    rows = sorted(WIDGET_REGISTRY.items(),
                  key=lambda kv: (kv[1].priority, kv[0]))
    for name, cls in rows:
        ori = "/".join(cls.orientations)
        doc = (cls.__doc__ or "").strip().splitlines()
        desc = doc[0].strip() if doc else ""
        src = "外挂" if str(getattr(cls, "__module__", "")).startswith("msu2_ext_widgets") else "内置"
        print(f"{name:<18} {ori:<22} {cls.priority:<5} {src:<6} {desc}")

def _setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_offline_window(manager, stop_event):
    try:
        import tkinter as tk
        from PIL import ImageTk
    except Exception as e:
        _console("离线模拟屏不可用（需要 tkinter 与 Pillow）: %s" % e)
        return 1
    root = tk.Tk()
    root.title("MSU2_MINI 离线模拟屏")
    root.resizable(False, False)
    canvas = tk.Canvas(root, width=SCREEN_W * 3, height=SCREEN_H * 3,
                       bg="black", highlightthickness=0, bd=0)
    canvas.pack()
    bar = tk.Frame(root)
    bar.pack(fill="x")
    holder = {"photo": None}

    def act(fn):
        def _run():
            s = manager.active()
            if s is not None:
                fn(s)
        return _run

    tk.Button(bar, text="上一页", command=act(lambda s: s.prev_page())).pack(side="left")
    tk.Button(bar, text="下一页", command=act(lambda s: s.next_page())).pack(side="left")
    tk.Button(bar, text="切换朝向",
              command=act(lambda s: s.toggle_orientation())).pack(side="left")
    tk.Button(bar, text="上下翻转",
              command=act(lambda s: s.toggle_reverse())).pack(side="left")
    tk.Button(bar, text="关闭", command=stop_event.set).pack(side="right")

    def tick():
        sched = manager.active()
        dev = getattr(sched, "device", None) if sched is not None else None
        img = getattr(dev, "last", None) if dev is not None else None
        if img is not None:
            img = img.convert("RGB")
            w, h = img.size
            scale = 3
            if (canvas.winfo_width(), canvas.winfo_height()) != (w * scale, h * scale):
                canvas.config(width=w * scale, height=h * scale)
            photo = ImageTk.PhotoImage(
                img.resize((w * scale, h * scale), Image.NEAREST))
            holder["photo"] = photo
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=photo)
        if stop_event.is_set():
            try:
                root.destroy()
            except Exception:
                pass
            return
        root.after(60, tick)

    root.protocol("WM_DELETE_WINDOW", stop_event.set)
    root.after(60, tick)
    root.mainloop()
    return 0


def main(argv=None):
    _setup_stdio()
    ap = argparse.ArgumentParser(description="MSU2_MINI_Console")
    ap.add_argument("--config")
    ap.add_argument("--init-config", action="store_true")
    ap.add_argument("--port")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--new-widget", default=None,
                    help="生成一个外挂插件骨架后退出")
    ap.add_argument("--offline", action="store_true",
                    help="不开串口，用窗口模拟屏幕")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--plugins", action="store_true",
                    help="列出所有已发现的插件后退出")
    ap.add_argument("--log", default=None)
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args(argv)

    if args.list:
        cmd_list(); return 0
    if args.plugins:
        cmd_plugins(); return 0
    if args.new_widget:
        return cmd_new_widget(args.new_widget)

    stream = sys.stderr
    if stream is None:
        stream = sys.stdout
    if stream is None:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            stream = None
    handlers = [logging.StreamHandler(stream)]
    if args.log_file:
        try:
            handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
        except Exception:
            _console("无法写入日志文件: %s" % args.log_file)

    logging.basicConfig(
        level=getattr(logging, (args.log or args.log_level or "INFO").upper(),
                      logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )

    cfg_path = args.config or _default_config_path()
    if args.init_config:
        p = Path(cfg_path)
        if p.exists():
            try: p.unlink()
            except Exception: pass

    cfg = ensure_config_file(cfg_path)
    level_name = (args.log or args.log_level
                  or cfg.get("log_level", "INFO")).upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))

    fi = probe_fonts()
    log.info("字体目录: %s", fi["font_dirs"])
    log.info("常规字重: %s", fi["cjk"])
    log.info("粗体字重: %s", fi["latin"])
    if cfg.get("font"):
        set_user_font(cfg["font"])

    log.info("已注册插件: %s", list(WIDGET_REGISTRY.keys()))
    try:
        ext = _widgets_pkg.external_report()
    except Exception:
        ext = {}
    for folder in ext.get("dirs") or []:
        log.info("外部插件目录: %s", folder)
    if ext.get("loaded"):
        log.info("外部插件加载 %d 个: %s", len(ext["loaded"]),
                 ", ".join(ext["loaded"]))
    if ext.get("failed"):
        log.warning("外部插件加载失败 %d 个: %s", len(ext["failed"]),
                    ext["failed"])

    if args.port:
        devs = cfg.get("devices") or []
        if devs and isinstance(devs[0], dict):
            devs[0]["port"] = args.port

    stop_event = threading.Event()

    def _save():
        save_config(cfg, cfg_path)

    def _request_shutdown():
        log.info("收到关闭请求，准备退出…")
        try:
            _save()
        except Exception:
            pass
        stop_event.set()

    manager = DeviceManager(cfg, cfg_path, offline=bool(args.offline))
    if not manager.order:
        log.error("配置里没有任何设备")
        return 2
    for did in manager.order:
        s = manager.schedulers[did]
        s._on_shutdown = _request_shutdown
        log.info("设备 %s -> %s", did, s.device.port or "auto")
    manager.start()

    hotkeys = None
    if cfg.get("hotkeys"):
        hotkeys = _start_hotkeys(manager)

    web = None
    if not args.headless:
        web = WebUI(manager, cfg, cfg_path, on_shutdown=_request_shutdown)
        web.start()
    else:
        log.info("无头模式: Ctrl+C 退出")

    def _sig(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig)

    try:
        if args.offline:
            run_offline_window(manager, stop_event)
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        log.info("正在退出…")
        try: _save()
        except Exception: pass
        manager.stop()
        if web: web.stop()
        for did in manager.order:
            try:
                manager.schedulers[did].device.close()
            except Exception:
                pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
