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

def coerce(field: Field, value: Any) -> Any:

    if value is None:
        return field.default
    try:
        if field.type == "int":
            return int(value)
        if field.type == "float":
            return float(value)
        if field.type == "bool":
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "on")
            return bool(value)
        if field.type == "list_str":
            if isinstance(value, str):
                return [v.strip() for v in value.splitlines() if v.strip()]
            if isinstance(value, list):
                return [str(v) for v in value]
            return []
        return str(value)
    except (ValueError, TypeError):
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
