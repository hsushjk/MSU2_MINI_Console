from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class Field:

    key: str
    type: str
    label: str = ""
    default: Any = None
    help: str = ""
    options: Optional[list] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None

    def to_dict(self):
        return {
            "key": self.key, "type": self.type,
            "label": self.label or self.key,
            "default": self.default, "help": self.help,
            "options": self.options,
            "min": self.min, "max": self.max, "step": self.step,
        }

def _clamp(field: Field, value: Any) -> Any:
    if field.type not in ("int", "float"):
        return value
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if field.min is not None and value < field.min:
        value = field.min
    if field.max is not None and value > field.max:
        value = field.max
    return int(value) if field.type == "int" else float(value)

def coerce(field: Field, value: Any) -> Any:

    if value is None:
        return field.default
    try:
        if field.type == "int":
            return _clamp(field, int(float(value)))
        if field.type == "float":
            return _clamp(field, float(value))
        if field.type == "bool":
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "on")
            return bool(value)
        if field.type == "list_str":
            if isinstance(value, str):
                out = [v.strip() for v in value.splitlines() if v.strip()]
            elif isinstance(value, list):
                out = [str(v) for v in value]
            else:
                out = []
            return out[:200]
        if field.type == "select" and field.options:
            text = str(value)
            return text if text in field.options else field.default
        return str(value)
    except (ValueError, TypeError, OverflowError):
        return field.default

def defaults_from(schema: list[Field]) -> dict:
    return {f.key: f.default for f in schema}

def merge_config(schema: list[Field], user: dict) -> dict:

    out = defaults_from(schema)
    if not isinstance(user, dict):
        return out
    for f in schema:
        if f.key in user:
            out[f.key] = coerce(f, user[f.key])
    return out
