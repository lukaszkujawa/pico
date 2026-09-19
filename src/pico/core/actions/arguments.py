from collections.abc import Mapping
from typing import cast

from pico.core.actions.shape import FIELD_TYPES, FieldType


class InvalidActionError(ValueError):
    pass


def require[T](arguments: Mapping[str, object], field: str, expected: type[T]) -> T:
    if field not in arguments:
        raise InvalidActionError(f"missing required field {field!r}")
    value = arguments[field]
    if not isinstance(value, expected):
        raise InvalidActionError(
            f"field {field!r} must be a {expected.__name__}, got {type(value).__name__}"
        )
    return value


_ELEMENT_NAMES: Mapping[type[object], str] = {str: "strings", int: "integers"}


def require_list[T](arguments: Mapping[str, object], field: str, element: type[T]) -> tuple[T, ...]:
    if field not in arguments:
        raise InvalidActionError(f"missing required field {field!r}")
    value = arguments[field]
    if not isinstance(value, list):
        raise InvalidActionError(f"field {field!r} must be a list, got {type(value).__name__}")
    elements: list[T] = []
    for item in cast(list[object], value):
        if not isinstance(item, element):
            raise InvalidActionError(
                f"field {field!r} must be a list of {_ELEMENT_NAMES[element]}, "
                f"got {type(item).__name__} element"
            )
        elements.append(item)
    return tuple(elements)


def require_fields(arguments: Mapping[str, object]) -> Mapping[str, FieldType] | None:
    value = arguments.get("fields")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidActionError(f"field 'fields' must be an object, got {type(value).__name__}")
    raw = cast(dict[object, object], value)
    fields: dict[str, FieldType] = {}
    for name, type_name in raw.items():
        if not isinstance(name, str):
            raise InvalidActionError("field 'fields' must have string keys")
        if type_name not in FIELD_TYPES:
            raise InvalidActionError(
                f"field 'fields' has unknown type {type_name!r} for {name!r}; "
                f"expected one of {list(FIELD_TYPES)}"
            )
        fields[name] = type_name
    if not fields:
        raise InvalidActionError("field 'fields' must not be empty")
    return fields
