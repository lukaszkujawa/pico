import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

FieldType = Literal["string", "number", "boolean"]

FIELD_TYPES: tuple[FieldType, ...] = ("string", "number", "boolean")


def _matches(value: object, type_name: FieldType) -> bool:
    match type_name:
        case "string":
            return isinstance(value, str)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)


@dataclass(frozen=True)
class ResultShape:
    fields: Mapping[str, FieldType]

    def prompt(self) -> str:
        shape = ", ".join(f'"{name}": <{type_name}>' for name, type_name in self.fields.items())
        return (
            "\n\nAnswer with content that is exactly one JSON object of this shape, "
            f"and nothing else: {{{shape}}}"
        )

    def check(self, content: str) -> str | None:
        try:
            parsed: object = json.loads(content)
        except ValueError:
            return "answer content must be a JSON object, but it did not parse as JSON"
        if not isinstance(parsed, dict):
            return f"answer content must be a JSON object, got {type(parsed).__name__}"
        record = cast(dict[str, object], parsed)
        missing = sorted(set(self.fields) - set(record))
        if missing:
            return f"answer is missing required field(s): {missing}"
        extra = sorted(set(record) - set(self.fields))
        if extra:
            return f"answer has unexpected field(s): {extra}"
        for name, type_name in self.fields.items():
            if not _matches(record[name], type_name):
                return f"field {name!r} must be a {type_name}, got {type(record[name]).__name__}"
        return None
