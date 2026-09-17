from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from pico.llm.client import LLMClient
from pico.llm.types import Message, StreamEvent, TextDelta, ThinkingDelta, ToolSpec


def _timestamp() -> str:
    return datetime.now().isoformat()


def _render_messages(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        parts.append(f"[{message.role.value}]")
        if message.content:
            parts.append(message.content)
        for call in message.tool_calls:
            parts.append(f"  tool_call {call.id} {call.name}({dict(call.arguments)!r})")
        if message.tool_result is not None:
            result = message.tool_result
            parts.append(f"  tool_result {result.tool_call_id} is_error={result.is_error}")
            parts.append(result.content)
    return "\n".join(parts)


class RunLog:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._counter = 0

    @staticmethod
    def create(root: Path = Path("logs")) -> "RunLog":
        root.mkdir(parents=True, exist_ok=True)
        base_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        candidate = root / base_name
        suffix = 1
        while candidate.exists():
            suffix += 1
            candidate = root / f"{base_name}-{suffix}"
        candidate.mkdir(parents=True)
        return RunLog(candidate)

    @property
    def directory(self) -> Path:
        return self._directory

    def write_prompt(self, messages: list[Message]) -> None:
        self._counter += 1
        content = f"{_timestamp()}\n{_render_messages(messages)}"
        (self._directory / f"prompt-{self._counter}.txt").write_text(content)

    def write_response(self, text: str) -> None:
        content = f"{_timestamp()}\n{text}"
        (self._directory / f"resp-{self._counter}.txt").write_text(content)

    def log(self, line: str) -> None:
        with (self._directory / "session.log").open("a") as handle:
            handle.write(f"{_timestamp()} {line}\n")


class LoggingLLMClient:
    def __init__(self, client: LLMClient, run_log: RunLog) -> None:
        self._client = client
        self._run_log = run_log

    def models(self) -> list[str]:
        return self._client.models()

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self._run_log.write_prompt(messages)
        text = ""
        try:
            for event in self._client.stream(messages, tools):
                match event:
                    case TextDelta(text=chunk) | ThinkingDelta(text=chunk):
                        text += chunk
                    case _:
                        pass
                yield event
        finally:
            self._run_log.write_response(text)
