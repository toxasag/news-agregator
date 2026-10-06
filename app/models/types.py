"""Shared column types for JSONB fields that are mutated in place.

A plain :class:`~sqlalchemy.dialects.postgresql.JSONB` column only registers a
change when the attribute is *reassigned*. Worker code naturally mutates these
structures in place (bumping a retry counter, recording an external post id),
and those edits would never be flushed, so a retry would re-read the stale
state and publish twice. Wrapping the type in SQLAlchemy's mutation-tracking
extensions makes in-place edits dirty the instance, which is what keeps
publishing idempotent per AGENTS.md rule 5.

``MutableDict``/``MutableList`` only track their own top level, so nested
updates such as ``platform_statuses["tg"]["status"] = "published"`` would still
be missed. The containers below carry a link to their owning container and
forward change events up to the attribute-bound root.
"""

from __future__ import annotations

from typing import Any, SupportsIndex

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.types import TypeEngine


class _NestedAware:
    """Forwards ``changed()`` to the attribute-bound root container."""

    _mutable_parent: _NestedAware | None = None

    def _attach(self, parent: _NestedAware | None) -> None:
        self._mutable_parent = parent

    def changed(self) -> None:
        parent = self._mutable_parent
        if parent is not None:
            parent.changed()
        else:
            super().changed()  # type: ignore[misc]


class NestedMutableDict(_NestedAware, MutableDict):  # type: ignore[type-arg]
    """JSONB object whose nested dict/list edits dirty the owning instance."""

    @classmethod
    def coerce(cls, key: str, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            instance = cls()
            for item_key, item_value in value.items():
                dict.__setitem__(instance, item_key, _wrap(item_value, instance))
            return instance
        return super().coerce(key, value)

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(key, _wrap(value, self))

    def setdefault(self, key: Any, default: Any = None) -> Any:
        return super().setdefault(key, _wrap(default, self))

    def update(self, *args: Any, **kwargs: Any) -> None:
        merged = dict(*args, **kwargs)
        super().update({k: _wrap(v, self) for k, v in merged.items()})


class NestedMutableList(_NestedAware, MutableList):  # type: ignore[type-arg]
    """JSONB array whose nested dict/list edits dirty the owning instance."""

    @classmethod
    def coerce(cls, key: str, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if isinstance(value, list):
            instance = cls()
            for item in value:
                list.append(instance, _wrap(item, instance))
            return instance
        return super().coerce(key, value)

    def __setitem__(self, index: Any, value: Any) -> None:
        super().__setitem__(index, _wrap(value, self))

    def append(self, value: Any) -> None:
        super().append(_wrap(value, self))

    def insert(self, index: SupportsIndex, value: Any) -> None:
        super().insert(index, _wrap(value, self))

    def extend(self, values: Any) -> None:
        super().extend(_wrap(value, self) for value in values)


def _wrap(value: Any, parent: _NestedAware) -> Any:
    """Recursively wrap dicts/lists so nested edits propagate to ``parent``."""
    if isinstance(value, NestedMutableDict | NestedMutableList):
        value._attach(parent)
        return value
    if isinstance(value, dict):
        wrapped_dict = NestedMutableDict()
        wrapped_dict._attach(parent)
        for key, item in value.items():
            dict.__setitem__(wrapped_dict, key, _wrap(item, wrapped_dict))
        return wrapped_dict
    if isinstance(value, list):
        wrapped_list = NestedMutableList()
        wrapped_list._attach(parent)
        for item in value:
            list.append(wrapped_list, _wrap(item, wrapped_list))
        return wrapped_list
    return value


def _jsonb() -> TypeEngine[Any]:
    """Return a fresh JSONB instance.

    Each ``as_mutable()`` call attaches listeners to the type instance it is
    given, so the dict and list variants must not share one.
    """
    return JSONB()  # type: ignore[no-untyped-call]


#: JSONB object column with in-place mutation tracking, including nested levels.
MutableJSONB: TypeEngine[Any] = NestedMutableDict.as_mutable(_jsonb())

#: JSONB array column with in-place mutation tracking, including nested levels.
MutableJSONBList: TypeEngine[Any] = NestedMutableList.as_mutable(_jsonb())

__all__ = [
    "MutableJSONB",
    "MutableJSONBList",
    "NestedMutableDict",
    "NestedMutableList",
]
