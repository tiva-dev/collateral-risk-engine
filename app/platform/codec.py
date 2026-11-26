"""Lossless, type-tagged JSON codec for engine domain objects.

Monitored accounts are nested dataclasses with enums, datetimes, tuples and
dicts keyed by tuples (FX pairs). Plain ``json`` loses those types, so values
are tagged on the way out and rebuilt on the way in. Only classes from the
``app`` package can be rebuilt, so stored JSON cannot instantiate arbitrary
types.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

_ALLOWED_PREFIX = "app."


def _qualname(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _resolve(name: str) -> type:
    module_name, _, qual = name.partition(":")
    if not module_name.startswith(_ALLOWED_PREFIX):
        raise ValueError(f"refusing to decode type outside app package: {name}")
    obj: Any = importlib.import_module(module_name)
    for part in qual.split("."):
        obj = getattr(obj, part)
    if not isinstance(obj, type):
        raise TypeError(f"decoded name is not a type: {name}")
    return obj


def encode(value: Any) -> Any:
    # Enums first: str/int-valued enums are also str/int instances.
    if isinstance(value, Enum):
        return {"__enum__": _qualname(type(value)), "v": encode(value.value)}
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if math.isnan(value) or value in (float("inf"), float("-inf")):
            return {"__float__": repr(value)}
        return value
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__dec__": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            f.name: encode(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.init
        }
        return {"__dc__": _qualname(type(value)), "f": fields}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v) for v in value]}
    if isinstance(value, set | frozenset):
        return {"__set__": [encode(v) for v in sorted(value, key=repr)]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        if all(isinstance(k, str) and not isinstance(k, Enum) for k in value):
            return {"__map__": {k: encode(v) for k, v in value.items()}}
        return {"__pairs__": [[encode(k), encode(v)] for k, v in value.items()]}
    raise TypeError(f"cannot encode value of type {type(value).__name__}")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "__map__" in value:
        return {k: decode(v) for k, v in value["__map__"].items()}
    if "__pairs__" in value:
        return {decode(k): decode(v) for k, v in value["__pairs__"]}
    if "__dc__" in value:
        cls = _resolve(value["__dc__"])
        kwargs = {k: decode(v) for k, v in value["f"].items()}
        return cls(**kwargs)
    if "__enum__" in value:
        return _resolve(value["__enum__"])(decode(value["v"]))
    if "__dt__" in value:
        return datetime.fromisoformat(value["__dt__"])
    if "__date__" in value:
        return date.fromisoformat(value["__date__"])
    if "__dec__" in value:
        return Decimal(value["__dec__"])
    if "__tuple__" in value:
        return tuple(decode(v) for v in value["__tuple__"])
    if "__set__" in value:
        return {decode(v) for v in value["__set__"]}
    if "__float__" in value:
        return float(value["__float__"])
    raise ValueError(f"unrecognised encoded object with keys {sorted(value)}")


def dumps(value: Any) -> str:
    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"))


def loads(text: str | bytes | dict | list) -> Any:
    if isinstance(text, dict | list):
        return decode(text)
    return decode(json.loads(text))
