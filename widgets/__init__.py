import importlib
import logging
import pkgutil
from pathlib import Path

log = logging.getLogger("msu2.widgets")

_pkg_path = Path(__file__).parent

_loaded = []
_failed = []

for finder, name, ispkg in pkgutil.iter_modules([str(_pkg_path)]):
    if name.startswith("_") or ispkg:
        continue
    try:
        importlib.import_module(f".{name}", __package__)
        _loaded.append(name)
    except Exception as e:
        _failed.append((name, str(e)))
        log.error("加载插件模块 %s 失败: %s", name, e, exc_info=True)

log.info("插件模块加载完成: 成功 %d 个 (%s)",
         len(_loaded), ", ".join(_loaded))
if _failed:
    log.warning("插件模块加载失败 %d 个: %s", len(_failed), _failed)
