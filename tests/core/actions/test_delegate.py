import pytest

from pico.core.actions import (
    Delegate,
    InvalidActionError,
    ResultShape,
)


def test_delegate_from_arguments() -> None:
    action = Delegate.from_arguments({"question": "what is x?"})
    assert action == Delegate(question="what is x?")


def test_delegate_from_arguments_missing_question() -> None:
    with pytest.raises(InvalidActionError):
        Delegate.from_arguments({})


def test_delegate_from_arguments_with_fields() -> None:
    action = Delegate.from_arguments(
        {"question": "how many?", "fields": {"count": "number", "name": "string"}}
    )

    assert action == Delegate(
        question="how many?", shape=ResultShape({"count": "number", "name": "string"})
    )


def test_delegate_from_arguments_with_null_fields_is_untyped() -> None:
    assert Delegate.from_arguments({"question": "q", "fields": None}).shape is None


def test_delegate_from_arguments_rejects_unknown_field_type() -> None:
    with pytest.raises(InvalidActionError, match="unknown type 'date'"):
        Delegate.from_arguments({"question": "q", "fields": {"when": "date"}})


def test_delegate_from_arguments_rejects_non_object_fields() -> None:
    with pytest.raises(InvalidActionError, match="must be an object"):
        Delegate.from_arguments({"question": "q", "fields": ["count"]})


def test_delegate_from_arguments_rejects_empty_fields() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        Delegate.from_arguments({"question": "q", "fields": {}})


def test_result_shape_prompt_names_every_field() -> None:
    shape = ResultShape({"count": "number", "ok": "boolean"})

    assert '"count": <number>' in shape.prompt()
    assert '"ok": <boolean>' in shape.prompt()


def test_result_shape_check_accepts_conforming_record() -> None:
    shape = ResultShape({"count": "number", "ok": "boolean", "n": "string"})

    assert shape.check('{"count": 3, "ok": true, "n": "a"}') is None


def test_result_shape_check_rejects_boolean_where_number_expected() -> None:
    shape = ResultShape({"count": "number"})

    problem = shape.check('{"count": true}')

    assert problem is not None
    assert "must be a number" in problem


def test_result_shape_check_rejects_non_object_json() -> None:
    shape = ResultShape({"count": "number"})

    problem = shape.check("[1, 2]")

    assert problem is not None
    assert "must be a JSON object" in problem
