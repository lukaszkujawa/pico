from pico.tui.commands import (
    COMMANDS,
    Completion,
    Options,
    Row,
    awaits_argument,
    complete,
    find,
)


class FakeSwitch:
    def __init__(self, options: Options) -> None:
        self._options = options
        self.current = "qwen3:8b"

    def available(self) -> Options:
        return self._options

    def switch_to(self, model: str) -> None:
        self.current = model


def test_registry_reads_as_data() -> None:
    assert [command.name for command in COMMANDS] == ["model", "quit"]
    assert find("quit") is not None
    assert find("nope") is None


def test_plain_text_and_multiline_input_never_complete() -> None:
    assert complete("hello", None) is None
    assert complete("read a/b.txt", None) is None
    assert complete("/model\nqwen3", None) is None


def test_command_stage_narrows_by_prefix() -> None:
    assert complete("/", None) == Completion(
        rows=(
            Row(label="model", hint="switch the model for the next run"),
            Row(label="quit", hint="exit pico"),
        )
    )
    assert [row.label for row in complete("/q", None).rows] == ["quit"]  # type: ignore[union-attr]
    assert complete("/zz", None) == Completion()


def test_argument_stage_needs_a_command_that_takes_one() -> None:
    switch = FakeSwitch(Options(names=("qwen3:8b",)))

    assert complete("/quit ", switch) is None
    assert complete("/nope ", switch) is None
    assert complete("/model ", None) is None


def test_argument_stage_marks_the_current_model_and_completes_the_full_text() -> None:
    switch = FakeSwitch(Options(names=("qwen3:8b", "gemma3:27b")))
    completion = complete("/model gem", switch)

    assert completion is not None
    assert completion.rows == (Row(label="gemma3:27b"),)
    assert completion.accepted(completion.rows[0]) == "/model gemma3:27b"


def test_a_listing_failure_becomes_an_error_row() -> None:
    completion = complete("/model ", FakeSwitch(Options(error="connection refused")))

    assert completion is not None
    assert completion.rows == ()
    assert completion.error == "connection refused"


def test_command_stage_accepts_into_the_bare_command() -> None:
    completion = complete("/q", None)

    assert completion is not None
    assert completion.accepted(completion.rows[0]) == "/quit"


def test_awaits_argument_only_for_a_bare_command_that_takes_one() -> None:
    assert awaits_argument("/model")
    assert not awaits_argument("/model gemma3:27b")
    assert not awaits_argument("/quit")
    assert not awaits_argument("/nope")
