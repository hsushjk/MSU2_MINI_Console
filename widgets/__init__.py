import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
import types
from pathlib import Path

log = logging.getLogger("msu2.widgets")

_pkg_path = Path(__file__).parent

_EXT_PKG = "msu2_ext_widgets"
_EXT_DIR_ENV = "MSU2_WIDGETS_DIR"

_loaded = []
_failed = []
_ext_loaded = []
_ext_failed = []
_ext_dirs = []

for finder, name, ispkg in pkgutil.iter_modules([str(_pkg_path)]):
    if name.startswith("_") or ispkg:
        continue
    try:
        importlib.import_module(f".{name}", __package__)
        _loaded.append(name)
    except Exception as e:
        _failed.append((name, str(e)))
        log.error("加载插件模块 %s 失败: %s", name, e, exc_info=True)


def _app_dir():
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(os.path.abspath(sys.executable)))
    return _pkg_path.parent


def _candidate_dirs():
    out = []
    raw = os.environ.get(_EXT_DIR_ENV, "")
    for item in raw.replace(";", os.pathsep).split(os.pathsep):
        item = item.strip().strip('"').strip("'")
        if item:
            out.append(Path(item))
    app = _app_dir()
    if getattr(sys, "frozen", False):
        out.append(app / "widgets")
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = os.environ.get("XDG_DATA_HOME")
        if not base:
            try:
                base = str(Path.home() / ".local" / "share")
            except Exception:
                base = ""
        if base:
            out.append(Path(base) / "MSU2_MINI" / "widgets")
    else:
        out.append(app / "widgets_ext")
    return out


def _unique_dirs():
    seen = []
    result = []
    own = None
    try:
        own = _pkg_path.resolve()
    except Exception:
        own = _pkg_path
    for cand in _candidate_dirs():
        try:
            path = cand.expanduser().resolve()
        except Exception:
            continue
        if path == own or path in seen:
            continue
        if not path.is_dir():
            continue
        seen.append(path)
        result.append(path)
    return result


def _load_one(entry):
    if entry.is_dir():
        target = entry / "__init__.py"
        if not target.is_file():
            return
        mod_name = f"{_EXT_PKG}.{entry.name}"
        spec = importlib.util.spec_from_file_location(
            mod_name, target, submodule_search_locations=[str(entry)])
    elif entry.suffix.lower() == ".py":
        mod_name = f"{_EXT_PKG}.{entry.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, entry)
    else:
        return
    if spec is None or spec.loader is None:
        raise ImportError("无法为 %s 创建模块规格" % entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise


def _load_external():
    dirs = _unique_dirs()
    if not dirs:
        return
    pkg = types.ModuleType(_EXT_PKG)
    pkg.__path__ = [str(d) for d in dirs]
    pkg.__package__ = _EXT_PKG
    sys.modules[_EXT_PKG] = pkg
    for folder in dirs:
        _ext_dirs.append(str(folder))
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except Exception as e:
            log.error("读取外部插件目录 %s 失败: %s", folder, e)
            continue
        for entry in entries:
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            try:
                _load_one(entry)
                _ext_loaded.append("%s (%s)" % (entry.name, folder))
            except BaseException as e:
                _ext_failed.append((str(entry), str(e)))
                log.error("加载外部插件 %s 失败: %s", entry, e, exc_info=True)


_load_external()


def external_report():
    return {
        "dirs": list(_ext_dirs),
        "loaded": list(_ext_loaded),
        "failed": list(_ext_failed),
        "builtin": list(_loaded),
        "builtin_failed": list(_failed),
    }


log.info("插件模块加载完成: 内置 %d 个 (%s)",
         len(_loaded), ", ".join(_loaded))
if _ext_dirs:
    log.info("外部插件目录: %s", ", ".join(_ext_dirs))
if _loaded and _ext_loaded:
    log.info("外部插件加载 %d 个: %s", len(_ext_loaded), ", ".join(_ext_loaded))
if _failed:
    log.warning("内置插件模块加载失败 %d 个: %s", len(_failed), _failed)
if _ext_failed:
    log.warning("外部插件加载失败 %d 个: %s", len(_ext_failed), _ext_failed)