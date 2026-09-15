from collections.abc import Iterator

from pico.llm.client import LLMClient
from pico.llm.types import GenerationComplete, Message, StreamEvent, ToolSpec


class FakeClient:
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield GenerationComplete(finish_reason="stop")


def test_fake_client_satisfies_llm_client_protocol() -> None:
    client: LLMClient = FakeClient()
    events = list(client.stream([], []))
    assert events == [GenerationComplete(finish_reason="stop")]
