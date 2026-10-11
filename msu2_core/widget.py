from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
import logging
import os
import sys

from PIL import Image, ImageFont

log = logging.getLogger("msu2.core.widget")

LANDSCAPE_SIZE = (160, 80)
PORTRAIT_SIZE  = (80, 160)
LANDSCAPE = "landscape"
PORTRAIT  = "portrait"
BOTH      = (LANDSCAPE, PORTRAIT)

@dataclass
class RenderContext:
    device: object
    now: float
    orientation: str = LANDSCAPE
    state: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    @property
    def size(self):
        return PORTRAIT_SIZE if self.orientation == PORTRAIT else LANDSCAPE_SIZE

class Widget:
    name = "base"
    interval = 1.0
    orientations = BOTH
    priority = 50
    config_schema: list = []
    actions: dict = {}
    touch_help = ""

    def setup(self, ctx: RenderContext):
        pass

    def teardown(self, ctx: RenderContext):
        pass

    def on_enter(self, ctx: RenderContext):

        pass

    def on_leave(self, ctx: RenderContext):

        pass

    def render(self, ctx: RenderContext) -> Image.Image:

        raise NotImplementedError

    def get_config(self, ctx: RenderContext) -> dict:
        from .config_schema import merge_config
        all_wc = ctx.config.get("widget_config", {}) or {}
        return merge_config(self.config_schema, all_wc.get(self.name, {}))

    def on_action(self, action: str, payload: dict, ctx: RenderContext) -> dict:
        return {"ok": False, "err": f"未实现动作: {action}"}

if getattr(sys, "frozen", False):
    _APP_DIR = Path(os.path.dirname(os.path.abspath(sys.executable)))
else:
    _APP_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_RES_DIR = Path(getattr(sys, "_MEIPASS", str(_APP_DIR)))
FONT_REGULAR = "NotoSansMonoCJKsc-Regular.otf"
FONT_BOLD = "NotoSansMonoCJKsc-Bold.otf"

_FONT_CACHE: dict = {}
_FONT_PATH_CACHE: dict = {}
_USER_FONT_PATH = None
_FONT_WARNED = False

def _resolve_font_path(name):
    if not name:
        return None
    if name in _FONT_PATH_CACHE:
        return _FONT_PATH_CACHE[name]
    result = None
    try:
        p = Path(name)
        if p.is_absolute():
            if p.is_file():
                result = str(p)
        else:
            for root in _font_roots():
                cand = root / name
                if cand.is_file():
                    result = str(cand)
                    break
    except (OSError, ValueError):
        result = None
    _FONT_PATH_CACHE[name] = result
    return result

def set_user_font(path_or_name: str):
    global _USER_FONT_PATH
    p = _resolve_font_path(path_or_name)
    _FONT_CACHE.clear()
    if p:
        _USER_FONT_PATH = p
        log.info("用户指定字体: %s", p)
    else:
        _USER_FONT_PATH = None
        if path_or_name:
            log.warning("用户指定字体未找到: %s", path_or_name)

def _font_roots():
    roots = []
    for base in (_RES_DIR, _APP_DIR):
        p = base / "assets" / "fonts"
        if p not in roots:
            roots.append(p)
    return roots

def _bundled_font(bold: bool):
    names = [FONT_BOLD, FONT_REGULAR] if bold else [FONT_REGULAR]
    for root in _font_roots():
        for name in names:
            p = root / name
            if p.is_file():
                return str(p)
    return None

def get_font(size: int, bold: bool = False, cjk: bool = True):
    try:
        size = max(6, int(size))
    except (TypeError, ValueError):
        size = 12
    bold = bool(bold)
    key = (size, bold)
    f = _FONT_CACHE.get(key)
    if f is not None:
        return f
    path = _USER_FONT_PATH or _bundled_font(bold)
    if path:
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            log.warning("字体加载失败: %s", path)
    global _FONT_WARNED
    if not _FONT_WARNED:
        _FONT_WARNED = True
        log.warning("未找到内置字体 %s，中文将显示为方块", _font_roots())
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f

def probe_fonts():
    info = {"font_dirs": [str(p) for p in _font_roots()], "cjk": None, "latin": None}
    p = _bundled_font(False)
    if p:
        info["cjk"] = {"name": FONT_REGULAR, "path": p}
    p = _bundled_font(True)
    if p:
        info["latin"] = {"name": FONT_BOLD, "path": p}
    return info
