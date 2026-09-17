import threading
from collections.abc import Iterator

import pytest

from pico.core.search import (
    MIN_PAGE_CHARS,
    SCAN_PREVIEW_CHARS,
    SearchCancelled,
    search,
)
from pico.llm.errors import LLMError
from pico.llm.types import GenerationComplete, Message, StreamEvent, TextDelta, ToolSpec
from pico.session import Session, ToolCallRecorded, connect
from tests.llm_fakes import NoModels


class ReplyClient(NoModels):
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.prompts: list[str] = []
        self.seen_tools: list[list[ToolSpec]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.prompts.append(messages[-1].content)
        self.seen_tools.append(tools)
        yield TextDelta(text=self.replies.pop(0))
        yield GenerationComplete(finish_reason="stop")


class ExplodingClient(NoModels):
    def __init__(self, ok_calls: int) -> None:
        self.calls = 0
        self.ok_calls = ok_calls

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.calls += 1
        if self.calls > self.ok_calls:
            raise LLMError("connection lost")
        yield TextDelta(text="1")


def _session(contents: list[str]) -> Session:
    session = Session(connect(":memory:"), "s1")
    for content in contents:
        session.append(
            ToolCallRecorded(
                name="note", arguments={"content": content}, result=content, is_error=False
            )
        )
    return session


def _search(client: object, session: Session) -> str:
    return search(
        client,  # pyright: ignore[reportArgumentType]
        session,
        "review findings architecture core loop",
        128_000,
        4.0,
        threading.Event(),
    )


def test_keyword_query_finds_facts_that_share_no_literal_substring() -> None:
    session = _session(
        ["the Bus drops subscribers on error", "the run loop compiles context each turn"]
    )
    client = ReplyClient(["1, 2", "2", "[2] describes how the core loop builds its prompt"])

    result = _search(client, session)

    assert result.splitlines()[0] == "[2] describes how the core loop builds its prompt"
    assert "Call read_fact(id) to recover any fact in full." in result.splitlines()[1]


def test_search_sends_no_tool_specs() -> None:
    session = _session(["alpha"])
    client = ReplyClient(["1", "1", "[1] alpha matters"])

    _search(client, session)

    assert client.seen_tools == [[], [], []]


def test_query_with_no_relevant_facts_returns_the_fact_index_tail() -> None:
    session = _session(["unrelated one", "unrelated two"])
    client = ReplyClient(["none"])

    result = _search(client, session)

    assert result.startswith("no relevant facts found for 'review findings architecture core loop'")
    assert "[1] note(unrelated one): unrelated one" in result
    assert "[2] note(unrelated two): unrelated two" in result


def test_empty_ledger_returns_the_no_results_message_without_calling_the_model() -> None:
    client = ReplyClient([])

    result = _search(client, _session([]))

    assert result == "no relevant facts found for 'review findings architecture core loop'"
    assert client.prompts == []


def test_hallucinated_ids_are_dropped_from_the_shortlist() -> None:
    session = _session(["alpha", "beta"])
    client = ReplyClient(["1, 99", "1", "[1] alpha is the match"])

    result = _search(client, session)

    assert result.splitlines()[0] == "[1] alpha is the match"
    assert "[99]" not in client.prompts[1]


def test_garbage_reply_selects_nothing() -> None:
    session = _session(["alpha", "beta"])
    client = ReplyClient(["what a lovely day"])

    result = _search(client, session)

    assert "no relevant facts found" in result
    assert len(client.prompts) == 1


def test_refine_selecting_nothing_short_circuits_the_reduce_pass() -> None:
    session = _session(["alpha", "beta"])
    client = ReplyClient(["1, 2", "none"])

    result = _search(client, session)

    assert "no relevant facts found" in result
    assert len(client.prompts) == 2


def test_reduce_reply_naming_no_known_id_falls_back_to_no_results() -> None:
    session = _session(["alpha"])
    client = ReplyClient(["1", "1", "nothing here really"])

    assert "no relevant facts found" in _search(client, session)


class PagingClient(NoModels):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        prompt = messages[-1].content
        self.prompts.append(prompt)
        if "one line per relevant fact" in prompt:
            yield TextDelta(text="[1] first\n[6] last")
            return
        ids = [token for token in ("[1]", "[6]") if token in prompt]
        yield TextDelta(text=", ".join(ids).replace("[", "").replace("]", "") or "none")


def test_scan_pages_facts_and_keeps_survivors_from_every_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pico.core.search.MIN_PAGE_CHARS", 40)
    session = _session([f"fact number {index}" for index in range(6)])
    client = PagingClient()

    result = search(
        client,  # pyright: ignore[reportArgumentType]
        session,
        "anything",
        100,
        1.0,
        threading.Event(),
    )

    assert result.splitlines()[:2] == ["[1] first", "[6] last"]
    assert len(client.prompts) == 6 + 2 + 1
    assert all(len(prompt) < 400 for prompt in client.prompts)


def test_scan_previews_are_bounded() -> None:
    session = _session(["x" * 10_000])
    client = ReplyClient(["none"])

    _search(client, session)

    assert len(client.prompts[0]) < MIN_PAGE_CHARS + SCAN_PREVIEW_CHARS + 1_000


def test_cancelling_between_calls_raises_search_cancelled() -> None:
    session = _session(["alpha"])
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(SearchCancelled):
        search(
            ReplyClient(["1"]),  # pyright: ignore[reportArgumentType]
            session,
            "q",
            128_000,
            4.0,
            cancel,
        )


def test_llm_error_mid_search_propagates() -> None:
    session = _session(["alpha"])

    with pytest.raises(LLMError):
        _search(ExplodingClient(ok_calls=1), session)
