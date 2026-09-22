import logging

log = logging.getLogger("msu2.core.registry")

WIDGET_REGISTRY: dict = {}

def register(cls):

    name = getattr(cls, "name", None)
    if not name or name == "base":
        raise ValueError(f"插件类 {cls.__name__} 必须定义非空的 name 属性")
    if name in WIDGET_REGISTRY:
        log.warning("插件 %s 被重复注册，后者覆盖前者", name)
    WIDGET_REGISTRY[name] = cls
    log.debug("注册插件: %s -> %s", name, cls.__name__)
    return cls
