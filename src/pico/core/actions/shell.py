import codecs
import contextlib
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, Self

from pico.core.actions.arguments import require
from pico.core.actions.context import ActionContext, ActionResult, RunnerAction
from pico.core.errors import ToolError
from pico.core.events import ToolCallResultDelta
from pico.llm.types import ToolSpec


def _read_timeout(stream: IO[bytes], deadline: float) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError
            data = os.read(stream.fileno(), 65536)
            if not data:
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                return
            chunk = decoder.decode(data)
            if chunk:
                yield chunk
    finally:
        selector.close()


SHELL_SPEC = ToolSpec(
    name="shell",
    description="Run a shell command and return its combined stdout and stderr.",
    parameters={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)


@dataclass(frozen=True)
class Shell:
    command: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        command = require(arguments, "command", str)
        return cls(command=command)

    def run(
        self, timeout: float = 30, on_chunk: Callable[[str], None] | None = None
    ) -> tuple[int, str]:
        process = subprocess.Popen(
            self.command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        chunks: list[str] = []
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        with process:
            try:
                for chunk in _read_timeout(process.stdout, deadline):
                    chunks.append(chunk)
                    if on_chunk is not None:
                        on_chunk(chunk)
                code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise ToolError(f"command timed out after {timeout}s: {self.command}") from error
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise
        return code, "".join(chunks)

    def execute(self, timeout: float = 30) -> str:
        code, output = self.run(timeout)
        if code != 0:
            return f"exit code {code}\n{output}"
        return output


def run_shell(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    def on_chunk(chunk: str) -> None:
        context.bus.publish(ToolCallResultDelta(id=context.pane_id, text=chunk))

    code, output = Shell.from_arguments(arguments).run(on_chunk=on_chunk)
    if code != 0:
        return f"exit code {code}\n{output}", True
    return output, False


SHELL_ACTION = RunnerAction(spec=SHELL_SPEC, execute=run_shell)
