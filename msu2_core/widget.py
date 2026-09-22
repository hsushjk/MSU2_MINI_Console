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

_FONT_CACHE: dict = {}
_FONT_PATH_CACHE: dict = {}
_USER_FONT_PATH = None

def set_user_font(path_or_name: str):
    global _USER_FONT_PATH
    p = _resolve_font_path(path_or_name)
    if p:
        _USER_FONT_PATH = p
        _FONT_CACHE.clear()
        log.info("用户指定字体: %s", p)
    else:
        log.warning("用户指定字体未找到: %s", path_or_name)

def _font_dirs():
    dirs = []
    if sys.platform == "win32":
        win = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(win) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"),
                 Path.home() / "Library/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 Path.home() / ".fonts",
                 Path.home() / ".local/share/fonts"]
    return [d for d in dirs if d.exists()]

_CJK_FONTS = [
    "msyh.ttc", "msyhbd.ttc", "msyhl.ttc",
    "simhei.ttf", "simsun.ttc", "Deng.ttf", "Dengb.ttf",
    "SourceHanSansSC-Regular.otf",
    "PingFang.ttc", "Hiragino Sans GB.ttc",
    "STHeiti Medium.ttc", "Songti.ttc",
    "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf",
    "NotoSansSC-Regular.otf", "SourceHanSansCN-Regular.otf",
    "wqy-zenhei.ttc", "wqy-microhei.ttc",
    "DroidSansFallbackFull.ttf",
]

_LATIN_FONTS = [
    "DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf",
    "consolab.ttf", "consola.ttf",
    "Menlo-Bold.ttf", "Menlo.ttc",
    "arialbd.ttf", "arial.ttf",
]

def _resolve_font_path(name: str):
    if name in _FONT_PATH_CACHE:
        return _FONT_PATH_CACHE[name]
    if os.path.isabs(name) and os.path.exists(name):
        _FONT_PATH_CACHE[name] = name
        return name
    result = None
    for d in _font_dirs():
        p = d / name
        if p.exists():
            result = str(p); break
        for pat in (f"*/{name}", f"*/*/{name}"):
            try:
                hit = next(d.glob(pat), None)
                if hit:
                    result = str(hit); break
            except Exception:
                pass
        if result:
            break
    _FONT_PATH_CACHE[name] = result
    return result

def _try_load(name: str, size: int):
    path = _resolve_font_path(name)
    if not path:
        return None
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None

def get_font(size: int, bold: bool = False, cjk: bool = True):
    key = (size, bold, cjk)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    if _USER_FONT_PATH:
        try:
            f = ImageFont.truetype(_USER_FONT_PATH, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            pass
    candidates = []
    if cjk:
        if bold:
            candidates += ["msyhbd.ttc", "simhei.ttf",
                           "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf"]
        candidates += _CJK_FONTS + _LATIN_FONTS
    else:
        if bold:
            candidates += [n for n in _LATIN_FONTS if "Bold" in n or "bd" in n]
        candidates += _LATIN_FONTS + _CJK_FONTS
    seen = set(); uniq = []
    for n in candidates:
        if n not in seen:
            seen.add(n); uniq.append(n)
    for name in uniq:
        f = _try_load(name, size)
        if f is not None:
            _FONT_CACHE[key] = f
            return f
    log.warning("未找到可用的 TrueType 字体，中文将显示为方块")
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f

def probe_fonts():
    info = {"font_dirs": [str(d) for d in _font_dirs()],
            "cjk": None, "latin": None}
    for name in _CJK_FONTS:
        p = _resolve_font_path(name)
        if p:
            info["cjk"] = {"name": name, "path": p}; break
    for name in _LATIN_FONTS:
        p = _resolve_font_path(name)
        if p:
            info["latin"] = {"name": name, "path": p}; break
    return info
