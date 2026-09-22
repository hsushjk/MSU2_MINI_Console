from __future__ import annotations

import argparse
import datetime
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

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("缺少 pyserial: pip install pyserial", file=sys.stderr); sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("缺少 Pillow: pip install pillow", file=sys.stderr); sys.exit(1)

try:
    import psutil
except ImportError:
    print("缺少 psutil: pip install psutil", file=sys.stderr); sys.exit(1)

try:
    from msu2_core import (
        WIDGET_REGISTRY, RenderContext, Widget,
        LANDSCAPE, PORTRAIT, LANDSCAPE_SIZE, PORTRAIT_SIZE,
        probe_fonts, set_user_font,
        merge_config,
    )
    import widgets as _widgets_pkg
except ImportError as e:
    print(f"无法导入 msu2_core/widgets: {e}", file=sys.stderr); sys.exit(1)

SCREEN_W, SCREEN_H = LANDSCAPE_SIZE
SCREEN_BAUD = 19200
MANUAL_COOLDOWN = 0.8
HEARTBEAT_INTERVAL = 2.0

TOUCH_CH = 9
TOUCH_SAMPLE_INTERVAL = 0.06
TOUCH_LONG_PRESS = 0.8
TOUCH_DOUBLE_WINDOW = 0.35
TOUCH_DEFAULT_THRESHOLD = 60

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
                self._ser.write(data); return True
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

    def encode(self, img):
        if img.size != (SCREEN_W, SCREEN_H):
            img = img.resize((SCREEN_W, SCREEN_H))
        img = img.convert("RGB")
        raw = bytearray(SCREEN_W * SCREEN_H * 2)
        px = img.load(); i = 0
        for yy in range(SCREEN_H):
            for xx in range(SCREEN_W):
                r, g, b = px[xx, yy]
                v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                raw[i] = v >> 8; raw[i+1] = v & 0xFF; i += 2

        out = bytearray()
        out += MiniProtocol.lcd_set_xy(0, 0)
        out += MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H)
        out += MiniProtocol.lcd_enter_direct_mode()

        total = len(raw)
        n_segs = (total + 255) // 256
        tail = total % 256
        for si in range(n_segs):
            seg = bytes(raw[si*256:(si+1)*256])
            if len(seg) < 256:
                seg += b"\xff" * (256 - len(seg))
            units = [seg[k:k+4] for k in range(0, 256, 4)]
            counts = {}
            for u in units:
                counts[u] = counts.get(u, 0) + 1
            fill, _ = max(counts.items(), key=lambda kv: kv[1])
            out += MiniProtocol.lcd_set_fill(*fill)
            diff = sum(1 for u in units if u != fill)
            if diff / 64 > 0.7:
                for i, u in enumerate(units):
                    out += MiniProtocol.lcd_data_block(i, *u)
            else:
                for i, u in enumerate(units):
                    if u != fill:
                        out += MiniProtocol.lcd_data_block(i, *u)
            if si == n_segs - 1 and tail != 0:
                out += MiniProtocol.lcd_segment_end_tail(tail)
            else:
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

    def open(self):   return self.transport.open()
    def close(self):  self.transport.close()

    @property
    def connected(self): return self.transport.connected

    def show_image(self, img):
        frame = self.encoder.encode(img)
        SEG = 2048
        for i in range(0, len(frame), SEG):
            if not self.transport.write(frame[i:i+SEG]):
                return False
            time.sleep(0.003)
        time.sleep(0.02)
        self.transport.read_all()
        return True

    def set_reverse(self, reverse):
        self.screen_reversed = reverse
        self.transport.write(MiniProtocol.lcd_direction(1 if reverse else 0))

    def heartbeat(self):
        ok = self.transport.write(MiniProtocol.read_adc(15))
        time.sleep(0.01)
        self.transport.read_all()
        return ok

    def read_touch(self):
        if not self.transport.write(MiniProtocol.read_adc(TOUCH_CH)):
            return None
        time.sleep(0.015)
        rsp = self.transport.read_all()
        if len(rsp) >= 6:
            return (rsp[4] << 8) | rsp[5]
        return None

class Background:
    def __init__(self):
        self.path = ""
        self.frames = []
        self.durations = []
        self.idx = 0
        self.t_last = 0.0

    def load(self, path):
        self.path = path
        self.frames = []; self.durations = []
        self.idx = 0; self.t_last = 0.0
        if not path:
            return
        p = Path(path)
        if not p.exists():
            log.warning("背景图不存在: %s", p); return
        try:
            src = Image.open(p)
        except Exception as e:
            log.warning("背景图加载失败: %s", e); return

        try:
            if getattr(src, "is_animated", False):
                for i in range(src.n_frames):
                    src.seek(i)
                    self.frames.append(self._letterbox(src.convert("RGB")))
                    self.durations.append(
                        max(0.05, src.info.get("duration", 100) / 1000.0))
            else:
                self.frames = [self._letterbox(src.convert("RGB"))]
                self.durations = [0.0]
        except Exception as e:
            log.warning("背景图处理失败: %s", e)
            self.frames = []
        log.info("背景图已加载: %s (%d 帧)", path, len(self.frames))

    @staticmethod
    def _letterbox(img):
        tw, th = SCREEN_W, SCREEN_H
        scale = min(tw / img.width, th / img.height)
        nw = max(1, int(img.width * scale))
        nh = max(1, int(img.height * scale))
        img = img.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGB", (tw, th), "black")
        canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
        return canvas

    def tick(self, now):
        if not self.frames:
            return None
        if len(self.frames) == 1:
            return self.frames[0]
        dur = self.durations[self.idx]
        if now - self.t_last >= dur:
            self.idx = (self.idx + 1) % len(self.frames)
            self.t_last = now
        return self.frames[self.idx]

def _act_screenshot():
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("./screenshots")
        out_dir.mkdir(exist_ok=True)
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
    try:
        for m in mods:
            ctrl.press(m)
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
        for m in reversed(mods):
            ctrl.release(m)
        log.info("宏已发送: %s", combo)
        return True
    except Exception as e:
        log.warning("宏发送失败: %s", e)
        return False

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

class Scheduler(threading.Thread):
    def __init__(self, device, config, on_config_changed=None, on_shutdown=None):
        super().__init__(daemon=True, name="Scheduler")
        self.device = device
        self.config = config
        self._on_config_changed = on_config_changed
        self._on_shutdown = on_shutdown

        self.orientation = config.get("orientation", LANDSCAPE)
        self.reverse = bool(config.get("reverse", False))
        self.portrait_rotate = int(config.get("portrait_rotate", -90))

        bg = config.get("background", {})
        self._bg_enabled = bool(bg.get("enabled", False))
        self._bg_path = str(bg.get("path", "") or "")
        self.background = Background()
        if self._bg_enabled and self._bg_path:
            self.background.load(self._bg_path)

        self._load_touch_cfg(config)

        self.ctx = RenderContext(
            device=device, now=time.time(),
            orientation=self.orientation, config=config,
        )
        self._stop = threading.Event()
        self._switch = threading.Event()
        self._widgets = []
        self._current = 0
        self._cooldown_until = 0.0
        self._last_send_ts = 0.0

        self._reload_widgets()

    def _load_touch_cfg(self, config):
        tk = config.get("touch", {}) or {}
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
            "threshold": int(tk.get("threshold", TOUCH_DEFAULT_THRESHOLD)),
        }
        self._touch_baseline = None
        self._touch_cal = []
        self._touch_pressed = False
        self._touch_press_ts = 0.0
        self._touch_last_sample_ts = 0.0
        self._touch_waiting_double = False
        self._touch_double_ts = 0.0
        self._locked = False

    def set_touch_config(self, new_cfg):
        tk = self.config.setdefault("touch", {})
        for k in ("single", "double", "long",
                  "macro_single", "macro_double", "macro_long",
                  "program_single", "program_double", "program_long",
                  "threshold"):
            if k in new_cfg:
                tk[k] = new_cfg[k]
        self._load_touch_cfg(self.config)
        log.info("触摸配置已更新: %s", self._touch_cfg)

    def _allow(self, name="操作"):
        now = time.monotonic()
        if now < self._cooldown_until:
            log.info("%s 被冷却拒绝 (还剩 %.2fs)",
                     name, self._cooldown_until - now)
            return False
        self._cooldown_until = now + MANUAL_COOLDOWN
        return True

    def _reload_widgets(self):
        for w in self._widgets:
            try: w.teardown(self.ctx)
            except Exception: pass

        new_widgets = []
        for name in self.config.get("pages", []):
            cls = WIDGET_REGISTRY.get(name)
            if not cls:
                log.warning("未知插件: %s", name); continue
            w = cls()
            try: w.setup(self.ctx)
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

        log.info("已加载 %d 个插件: %s (当前: %s)",
                 len(self._widgets),
                 [w.name for w in self._widgets],
                 self._widgets[self._current].name if self._widgets else "-")
        self._ensure_supported()

    def _persist_current(self):
        if not self._widgets:
            return
        name = self._widgets[self._current].name
        if self.config.get("current_page") != name:
            self.config["current_page"] = name
            if self._on_config_changed:
                try: self._on_config_changed()
                except Exception as e:
                    log.warning("保存 current_page 失败: %s", e)

    def find_widget(self, name):
        for w in self._widgets:
            if w.name == name:
                return w
        return None

    def _ensure_supported(self):
        if not self._widgets: return
        if self.orientation in self._widgets[self._current].orientations:
            return
        self._find_supported(self._current, +1)

    def _find_supported(self, start, direction):
        n = len(self._widgets)
        if n == 0: return False
        for i in range(n):
            idx = (start + i * direction) % n
            if self.orientation in self._widgets[idx].orientations:
                self._current = idx
                self._persist_current()
                return True
        return False

    def reload(self):
        self._reload_widgets()
        self._switch.set()

    def next_page(self, from_touch=False):
        if from_touch and self._locked:
            log.info("已锁定，忽略触摸翻页")
            return False
        if not self._allow("next_page"): return False
        if not self._widgets: return False
        self._find_supported(self._current + 1, +1)
        self._switch.set()
        log.info("切页 -> %s", self._widgets[self._current].name)
        return True

    def prev_page(self, from_touch=False):
        if from_touch and self._locked:
            log.info("已锁定，忽略触摸翻页")
            return False
        if not self._allow("prev_page"): return False
        if not self._widgets: return False
        self._find_supported(self._current - 1, -1)
        self._switch.set()
        log.info("切页 -> %s", self._widgets[self._current].name)
        return True

    def toggle_reverse(self, from_touch=False):
        if not self._allow("toggle_reverse"): return False
        self.reverse = not self.reverse
        self.device.set_reverse(self.reverse)
        self._switch.set()
        log.info("翻转 -> %s", self.reverse)
        return True

    def toggle_orientation(self, from_touch=False):
        if not self._allow("toggle_orientation"): return False
        self.orientation = (PORTRAIT if self.orientation == LANDSCAPE
                            else LANDSCAPE)
        self.ctx.orientation = self.orientation
        self._ensure_supported()
        self._switch.set()
        log.info("朝向 -> %s", self.orientation)
        return True

    def toggle_lock(self, from_touch=False):
        self._locked = not self._locked
        log.info("锁定 -> %s", self._locked)
        return True

    def set_pages(self, page_names):
        self.config["pages"] = list(page_names)
        self._reload_widgets()
        self._switch.set()
        log.info("页面已更新: %s", page_names)
        return True

    def set_background(self, enabled, path=""):
        self._bg_enabled = bool(enabled)
        self._bg_path = str(path or "")
        if self._bg_enabled and self._bg_path:
            self.background.load(self._bg_path)
        else:
            self.background.load("")
        self._switch.set()
        log.info("背景 -> enabled=%s path=%s", self._bg_enabled, self._bg_path)
        return True

    def restart_widget(self, name):
        w = self.find_widget(name)
        if not w:
            return False
        try: w.teardown(self.ctx)
        except Exception: pass
        try:
            w.setup(self.ctx)
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

        if pressed and not self._touch_pressed:
            self._touch_press_ts = now
            self._touch_pressed = True
            if self._touch_waiting_double:
                pass
        elif not pressed and self._touch_pressed:
            self._touch_pressed = False
            hold = now - self._touch_press_ts

            if hold >= TOUCH_LONG_PRESS:
                self._touch_waiting_double = False
                log.info("触摸: 长按 (%.2fs)", hold)
                self._dispatch_touch("long")
            else:
                if self._touch_waiting_double and \
                        now - self._touch_double_ts <= TOUCH_DOUBLE_WINDOW:
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

    def _all_passthrough(self):
        return (self._touch_cfg.get("single") == "passthrough" and
                self._touch_cfg.get("double") == "passthrough" and
                self._touch_cfg.get("long") == "passthrough")

    def _dispatch_touch(self, gesture):
        if self._all_passthrough():
            w = self._widgets[self._current] if self._widgets else None
            if w:
                try:
                    r = w.on_action(f"touch_{gesture}", {}, self.ctx)
                    if isinstance(r, dict) and r.get("ok"):
                        self._switch.set()
                except Exception as e:
                    log.warning("插件 %s touch_%s 失败: %s",
                                w.name, gesture, e)
            return

        action = self._touch_cfg.get(gesture, "next_page")
        macro = self._touch_cfg.get(f"macro_{gesture}", "")
        program = self._touch_cfg.get(f"program_{gesture}", "")
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
        else:
            log.warning("未知触摸动作: %s", action)

    def _composite(self, widget_img):
        if self.orientation == PORTRAIT:
            widget_img = widget_img.rotate(self.portrait_rotate, expand=True)
        if widget_img.size != (SCREEN_W, SCREEN_H):
            widget_img = widget_img.resize((SCREEN_W, SCREEN_H))
        if widget_img.mode != "RGBA":
            widget_img = widget_img.convert("RGBA")
        bg = self.background.tick(time.time())
        if bg is None:
            bg = Image.new("RGB", (SCREEN_W, SCREEN_H), "black")
        return Image.alpha_composite(bg.convert("RGBA"), widget_img).convert("RGB")

    def run(self):
        log.info("Scheduler 启动, %d 页, 朝向 %s, 翻转 %s",
                 len(self._widgets), self.orientation, self.reverse)
        last_ts = 0.0
        while not self._stop.is_set():
            if not self.device.connected:
                if not self.device.open():
                    time.sleep(1.0); continue
                self.device.set_reverse(self.reverse)
                last_ts = 0.0

            widgets = self._widgets
            current = self._current
            if not widgets or current >= len(widgets):
                time.sleep(0.5); continue

            if self._switch.is_set():
                self._switch.clear()
                last_ts = 0.0
                widgets = self._widgets
                current = self._current
                if 0 <= current < len(widgets):
                    try:
                        widgets[current].on_enter(self.ctx)
                    except Exception as e:
                        log.warning("on_enter %s 失败: %s",
                                    widgets[current].name, e)

            w = widgets[current]
            now = time.time()
            self.ctx.now = now
            self.ctx.orientation = self.orientation

            need_render = (now - last_ts >= w.interval)
            need_heartbeat = (not need_render and
                              now - self._last_send_ts >= HEARTBEAT_INTERVAL)

            if need_render:
                last_ts = now
                try:
                    img = w.render(self.ctx)
                    if img is None:
                        pass
                    else:
                        final = self._composite(img)
                        if self.device.show_image(final):
                            self._last_send_ts = time.time()
                except Exception as e:
                    log.exception("插件 %s 渲染异常: %s", w.name, e)
                    time.sleep(0.3)
            elif need_heartbeat:
                if self.device.heartbeat():
                    self._last_send_ts = time.time()

            if now - self._touch_last_sample_ts >= TOUCH_SAMPLE_INTERVAL:
                self._touch_last_sample_ts = now
                try:
                    self._poll_touch(now)
                except Exception as e:
                    log.debug("触摸采样异常: %s", e)

            time.sleep(0.005)
        log.info("Scheduler 退出")

DEFAULT_CONFIG_PATH = "./config.json"

def _default_pages():
    names = list(WIDGET_REGISTRY.keys())
    names.sort(key=lambda n: (WIDGET_REGISTRY[n].priority, n))
    return names

def _base_default_config():
    return {
        "port": "auto",
        "pages": _default_pages(),
        "current_page": "",
        "orientation": "landscape",
        "reverse": False,
        "portrait_rotate": -90,
        "gif_dir": "./assets/gif",
        "background": {"enabled": False, "path": ""},
        "widget_config": {},
        "font": "",
        "web": {"host": "0.0.0.0", "port": 8765},
        "log_level": "INFO",
        "touch": {
            "single": "next_page",
            "double": "toggle_orientation",
            "long":   "toggle_reverse",
            "macro_single": "",
            "macro_double": "",
            "macro_long":   "",
            "program_single": "",
            "program_double": "",
            "program_long":   "",
            "threshold": TOUCH_DEFAULT_THRESHOLD,
        },
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
            }
        except Exception:
            meta[name] = {}
    return meta

def ensure_config_file(path=DEFAULT_CONFIG_PATH):
    p = Path(path)
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
            "background": {"enabled": "背景图开关", "path": "背景图路径"},
            "widget_config": "每个插件自己的配置（Web 里点 ⚙ 修改）",
            "font": "自定义字体路径",
            "web": {"host": "WebUI 监听地址", "port": "WebUI 端口"},
            "log_level": "DEBUG / INFO / WARNING / ERROR",
            "touch": "全局触控动作。三个都为 passthrough 时，动作透传给插件",
        }
        cfg["_available_widgets"] = meta
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        log.info("已生成默认配置: %s", p)
        log.info("  可用插件: %s", ", ".join(meta.keys()))
        return cfg
    try:
        user_cfg = json.loads(p.read_text("utf-8"))
        if not isinstance(user_cfg, dict):
            raise ValueError("顶层必须是对象")
    except Exception as e:
        log.warning("配置解析失败 (%s)，重写默认: %s", e, p)
        cfg = _base_default_config(); cfg["_available_widgets"] = meta
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        return cfg

    merged = dict(user_cfg)
    added = []
    for k, v in _base_default_config().items():
        if k not in merged:
            merged[k] = v; added.append(k)

    pages = [p for p in merged.get("pages", []) if p in WIDGET_REGISTRY]
    if pages != merged.get("pages"):
        merged["pages"] = pages

    wc = merged.get("widget_config", {}) or {}
    cleaned_wc = {k: v for k, v in wc.items() if k in WIDGET_REGISTRY}
    if cleaned_wc != wc:
        merged["widget_config"] = cleaned_wc
        log.info("已清理 widget_config 中不存在的插件: %s",
                 set(wc.keys()) - set(cleaned_wc.keys()))

    if merged.get("current_page") and merged["current_page"] not in WIDGET_REGISTRY:
        merged["current_page"] = ""

    if merged.get("_available_widgets") != meta:
        merged["_available_widgets"] = meta
    p.write_text(json.dumps(merged, indent=2, ensure_ascii=False), "utf-8")
    if added:
        log.info("配置已补齐缺失项: %s", added)
    return merged

def save_config(cfg, path=DEFAULT_CONFIG_PATH):
    try:
        Path(path).write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    except Exception as e:
        log.warning("保存配置失败: %s", e)

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
.msg.err { color: #c00; }
.plist { border: 1px solid #ddd; background: #fff; border-radius: 3px;
         padding: 4px 6px; }
.pitem { padding: 3px 2px; border-bottom: 1px solid #eee;
         display: flex; align-items: center; flex-wrap: wrap; }
.pitem:last-child { border-bottom: 0; }
.pitem .name { flex: 1; padding-left: 6px; }
.pitem .tags { color: #888; font-size: 11px; margin-left: 6px; }
.pitem.empty { color: #999; font-style: italic; padding: 6px 8px; }
.pitem.current { background: #e8f0ff; }
.pitem.current .name { font-weight: bold; color: #2b5fd9; }
.pitem.current .name::before { content: '▶ '; color: #2b5fd9; }
.pitem .actions { margin-left: auto; display: flex; gap: 4px; }
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
</style>
</head>
<body>

<h1>MSU2_MINI_Console</h1>
<h3>本项目开源，采用 PolyForm Noncommercial License 1.0.0 </h3>

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
    三个都为"未定义"时，手势才会透传给插件<br>
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
  <div style="margin-top:.4em">
    <button onclick="loadPages()">刷新列表</button>
    <button onclick="post('/api/reload')">重载插件</button>
    <span class="msg" id="msg-pages"></span>
  </div>
</div>

<div class="card" id="config-panel" style="display:none">
  <h2 id="config-title">配置</h2>
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
    <button id="btn-bg" onclick="toggleBackground()">启用背景</button>
    <span class="msg" id="msg-bg"></span>
  </div>
</div>

<div class="card">
  <h2>状态</h2>
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

function setMsg(id, text, isErr) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.toggle('err', !!isErr);
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
    if (name === currentWidgetName) row.classList.add('current');

    const cfgBtn = w.has_config
      ? `<button class="mini" onclick="openConfig('${name}')">⚙</button>`
      : '';

    let actionBtns = '';
    if (w.actions && Object.keys(w.actions).length) {
      actionBtns = '<span class="actions">' +
        Object.entries(w.actions).map(([a, label]) =>
          `<button class="mini" onclick="doAction('${name}','${a}')">${label}</button>`
        ).join('') + '</span>';
    }

    row.innerHTML =
      `<button class="mini" onclick="moveUp(${i})" ${i===0?'disabled':''}>↑</button>` +
      `<button class="mini" onclick="moveDown(${i})" ${i===currentPages.length-1?'disabled':''}>↓</button>` +
      `<button class="mini" onclick="removeAt(${i})">×</button>` +
      cfgBtn +
      `<span class="name">${name}</span>` +
      `<span class="tags">[${(w.orientations||[]).join('/')}]</span>` +
      actionBtns;
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
        `<button class="mini" onclick="addWidget('${name}')">+</button>` +
        `<span class="name">${name}</span>` +
        `<span class="tags">[${(w.orientations||[]).join('/')}]</span>`;
      box.appendChild(row);
    });
  }
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

  if (next && !path) {
    setMsg('msg-bg', '请先填写图片路径', true);
    return;
  }
  setMsg('msg-bg', '应用中...', false);
  try {
    const j = await getJSON('/api/background', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: next, path})
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
loadPages();
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""

class WebUI(threading.Thread):
    def __init__(self, scheduler, cfg, cfg_path, on_shutdown=None):
        super().__init__(daemon=True, name="WebUI")
        self.scheduler = scheduler
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.on_shutdown = on_shutdown
        self.host = cfg["web"]["host"]
        self.port = cfg["web"]["port"]
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        sched = self.scheduler
        cfg = self.cfg
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
                n = int(self.headers.get("Content-Length", "0") or 0)
                if n <= 0: return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return {}

            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path

                if path in ("/", "/index.html"):
                    self._html(INDEX_HTML); return

                if path == "/api/status":
                    widgets = sched._widgets
                    cur = sched._current
                    cur_name = widgets[cur].name if 0 <= cur < len(widgets) else None
                    self._json({
                        "connected": sched.device.connected,
                        "port": sched.device.port,
                        "current_widget": cur_name,
                        "widgets": [w.name for w in widgets],
                        "orientation": sched.orientation,
                        "reverse": sched.reverse,
                        "locked": sched._locked,
                        "background_enabled": sched._bg_enabled,
                        "background_path": sched._bg_path,
                    }); return

                if path == "/api/widgets":
                    self._json({
                        "widgets": [
                            {"name": n,
                             "orientations": list(c.orientations),
                             "interval": c.interval,
                             "priority": c.priority,
                             "has_config": bool(getattr(c, "config_schema", [])),
                             "actions": dict(getattr(c, "actions", {}) or {})}
                            for n, c in sorted(WIDGET_REGISTRY.items())
                        ],
                        "active": cfg.get("pages", []),
                    }); return

                if path == "/api/widget_config":
                    q = parse_qs(parsed.query)
                    name = (q.get("name") or [""])[0]
                    cls = WIDGET_REGISTRY.get(name)
                    if not cls:
                        self._json({"ok": False, "err": "未知插件"}); return
                    schema = getattr(cls, "config_schema", [])
                    user = cfg.get("widget_config", {}).get(name, {})
                    values = merge_config(schema, user)
                    self._json({
                        "ok": True, "name": name,
                        "schema": [f.to_dict() for f in schema],
                        "values": values,
                    }); return

                if path == "/api/touch_config":
                    tk = cfg.get("touch", {}) or {}
                    self._json({
                        "ok": True,
                        "values": {
                            "single": tk.get("single", "next_page"),
                            "double": tk.get("double", "toggle_orientation"),
                            "long":   tk.get("long",   "toggle_reverse"),
                            "macro_single": tk.get("macro_single", ""),
                            "macro_double": tk.get("macro_double", ""),
                            "macro_long":   tk.get("macro_long", ""),
                            "program_single": tk.get("program_single", ""),
                            "program_double": tk.get("program_double", ""),
                            "program_long":   tk.get("program_long", ""),
                            "threshold":    tk.get("threshold", TOUCH_DEFAULT_THRESHOLD),
                        },
                    }); return

                self.send_error(404)

            def do_POST(self):
                try:
                    if self.path == "/api/next":
                        ok = sched.next_page()
                        self._json({"ok": ok, "err": "" if ok else "冷却中"})
                    elif self.path == "/api/prev":
                        ok = sched.prev_page()
                        self._json({"ok": ok, "err": "" if ok else "冷却中"})
                    elif self.path == "/api/reload":
                        sched.reload(); self._json({"ok": True})
                    elif self.path == "/api/reverse":
                        ok = sched.toggle_reverse()
                        if ok:
                            cfg["reverse"] = sched.reverse
                            save_config(cfg, cfg_path)
                        self._json({"ok": ok, "reverse": sched.reverse,
                                    "err": "" if ok else "冷却中"})
                    elif self.path == "/api/orientation":
                        ok = sched.toggle_orientation()
                        if ok:
                            cfg["orientation"] = sched.orientation
                            save_config(cfg, cfg_path)
                        self._json({"ok": ok, "orientation": sched.orientation,
                                    "err": "" if ok else "冷却中"})
                    elif self.path == "/api/pages":
                        body = self._read_json()
                        pages = [p for p in body.get("pages", [])
                                 if p in WIDGET_REGISTRY]
                        ok = sched.set_pages(pages)
                        if ok:
                            cfg["pages"] = pages
                            save_config(cfg, cfg_path)
                        self._json({"ok": ok, "pages": pages,
                                    "err": "" if ok else "拒绝"})
                    elif self.path == "/api/background":
                        body = self._read_json()
                        enabled = bool(body.get("enabled", False))
                        path = str(body.get("path", "") or "")
                        if not enabled and not path:
                            path = cfg.get("background", {}).get("path", "")
                        sched.set_background(enabled, path)
                        cfg.setdefault("background", {})
                        cfg["background"]["enabled"] = enabled
                        cfg["background"]["path"] = path
                        save_config(cfg, cfg_path)
                        self._json({"ok": True, "enabled": enabled, "path": path})
                    elif self.path == "/api/widget_config":
                        body = self._read_json()
                        name = str(body.get("name", ""))
                        values = body.get("values", {}) or {}
                        cls = WIDGET_REGISTRY.get(name)
                        if not cls:
                            self._json({"ok": False, "err": "未知插件"}); return
                        schema = getattr(cls, "config_schema", [])
                        merged = merge_config(schema, values)
                        cfg.setdefault("widget_config", {})
                        cfg["widget_config"][name] = merged
                        save_config(cfg, cfg_path)
                        sched.restart_widget(name)
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
                            result = w.on_action(action, payload, sched.ctx)
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
                            save_config(cfg, cfg_path)
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
    print(f"{'NAME':<18} {'ORI':<22} {'PRI':<5} {'DESC'}")
    print("-" * 80)
    rows = sorted(WIDGET_REGISTRY.items(),
                  key=lambda kv: (kv[1].priority, kv[0]))
    for name, cls in rows:
        ori = "/".join(cls.orientations)
        doc = (cls.__doc__ or "").strip().splitlines()
        desc = doc[0].strip() if doc else ""
        print(f"{name:<18} {ori:<22} {cls.priority:<5} {desc}")

def main(argv=None):
    ap = argparse.ArgumentParser(description="MSU2_MINI_Console")
    ap.add_argument("--config")
    ap.add_argument("--init-config", action="store_true")
    ap.add_argument("--port")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--plugins", action="store_true",
                    help="列出所有已发现的插件后退出")
    ap.add_argument("--log", default=None)
    args = ap.parse_args(argv)

    if args.list:
        cmd_list(); return 0
    if args.plugins:
        cmd_plugins(); return 0

    logging.basicConfig(
        level=getattr(logging, (args.log or "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg_path = args.config or DEFAULT_CONFIG_PATH
    if args.init_config:
        p = Path(cfg_path)
        if p.exists():
            try: p.unlink()
            except Exception: pass

    cfg = ensure_config_file(cfg_path)
    level_name = (args.log or cfg.get("log_level", "INFO")).upper()
    logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))

    fi = probe_fonts()
    log.info("字体目录: %s", fi["font_dirs"])
    log.info("CJK 字体: %s", fi["cjk"])
    log.info("Latin 字体: %s", fi["latin"])
    if cfg.get("font"):
        set_user_font(cfg["font"])

    log.info("已注册插件: %s", list(WIDGET_REGISTRY.keys()))

    port = args.port or cfg.get("port")
    if not port or port == "auto":
        log.info("自动扫描设备…")
        device = discover_and_open()
        if not device: return 2
    else:
        device = MiniDevice(port)
        if not device.open():
            log.error("无法打开 %s: %s",
                      port, device.transport.last_error or "未知错误")
            return 3

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

    sched = Scheduler(device, cfg,
                      on_config_changed=_save,
                      on_shutdown=_request_shutdown)
    sched.start()

    web = None
    if not args.headless:
        web = WebUI(sched, cfg, cfg_path, on_shutdown=_request_shutdown)
        web.start()
    else:
        log.info("无头模式: Ctrl+C 退出")

    def _sig(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig)

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        log.info("正在退出…")
        try: _save()
        except Exception: pass
        sched.stop()
        if web: web.stop()
        device.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
