import os
import time
import uuid

import pytest

from pico.core.actions import (
    InvalidActionError,
    Shell,
    register_actions,
)
from pico.core.actions.shell import _read_timeout  # pyright: ignore[reportPrivateUsage]
from pico.core.bus import Bus
from pico.core.errors import ToolError
from pico.core.events import (
    RunFinished,
    ToolCallFinished,
    ToolCallResultDelta,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.runner import LoopRunner
from pico.core.tools import ToolRegistry
from pico.llm.types import (
    GenerationComplete,
    ToolCall,
    ToolCallReady,
)
from pico.session import (
    UserMessageRecorded,
)
from tests.conftest import wait_until
from tests.core.loop_fixtures import (
    ScriptedClient,
    make_session,
)


def test_shell_from_arguments() -> None:
    action = Shell.from_arguments({"command": "echo hi"})
    assert action == Shell(command="echo hi")


def test_shell_from_arguments_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        Shell.from_arguments({})


def test_shell_from_arguments_wrong_type() -> None:
    with pytest.raises(InvalidActionError):
        Shell.from_arguments({"command": 1})


def test_shell_execute_returns_stdout() -> None:
    action = Shell(command="echo hello")
    assert action.execute().strip() == "hello"


def test_shell_execute_failing_command_includes_exit_code() -> None:
    action = Shell(command="exit 3")
    result = action.execute()
    assert "exit code 3" in result


def test_shell_execute_timeout_raises_tool_error() -> None:
    action = Shell(command="sleep 5")
    with pytest.raises(ToolError, match="timed out"):
        action.execute(timeout=0.1)


def test_shell_run_returns_exit_code_and_output() -> None:
    assert Shell(command="echo hi").run() == (0, "hi\n")
    code, output = Shell(command="echo boom >&2; exit 3").run()
    assert code == 3
    assert "boom" in output


def test_shell_run_invokes_callback_and_preserves_combined_output() -> None:
    chunks: list[str] = []
    code, output = Shell(command="echo one; echo two").run(on_chunk=chunks.append)

    assert code == 0
    assert "".join(chunks) == output
    assert output == "one\ntwo\n"


def test_read_timeout_yields_bytes_without_waiting_for_a_newline() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with open(read_fd, "rb", buffering=0) as reader:
            chunks = _read_timeout(reader, time.monotonic() + 5)
            os.write(write_fd, b"partial")
            assert next(chunks) == "partial"
            os.write(write_fd, b" caf\xc3")
            assert next(chunks) == " caf"
            os.write(write_fd, b"\xa9\n")
            assert next(chunks) == "é\n"
    finally:
        os.close(write_fd)


def test_read_timeout_times_out_on_a_stalled_partial_line() -> None:
    read_fd, write_fd = os.pipe()
    try:
        with open(read_fd, "rb", buffering=0) as reader:
            chunks = _read_timeout(reader, time.monotonic() + 0.05)
            os.write(write_fd, b"no newline")
            assert next(chunks) == "no newline"
            with pytest.raises(TimeoutError):
                next(chunks)
    finally:
        os.close(write_fd)


def test_shell_run_reports_non_zero_exit_code_with_callback() -> None:
    chunks: list[str] = []
    code, output = Shell(command="echo boom >&2; exit 3").run(on_chunk=chunks.append)

    assert code == 3
    assert "boom" in output
    assert "".join(chunks) == output


def test_shell_run_timeout_raises_tool_error_and_kills_process() -> None:
    marker = f"pico-timeout-{uuid.uuid4().hex}"

    with pytest.raises(ToolError, match="timed out"):
        Shell(command=f"sleep 5 # {marker}").run(timeout=0.1)

    def killed() -> bool:
        _, output = Shell(command=f"pgrep -f {marker} >/dev/null; echo $?").run()
        return output.strip() == "1"

    wait_until(killed, "the timed-out process is gone")


def test_shell_tool_call_publishes_result_deltas_before_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    call = ToolCall(id="1", name="shell", arguments={"command": "echo one; echo two"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break

    finished_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolCallFinished)
    )
    delta_indices = [
        index for index, event in enumerate(events) if isinstance(event, ToolCallResultDelta)
    ]
    assert delta_indices
    assert all(index < finished_index for index in delta_indices)
    finished = events[finished_index]
    assert isinstance(finished, ToolCallFinished)
    assert finished.result == "one\ntwo\n"
