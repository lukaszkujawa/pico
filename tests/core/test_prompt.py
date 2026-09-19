import random
from itertools import pairwise

from pico.core.ledger import facts
from pico.core.loop.prompt import (
    IMAGE_TOKEN_ESTIMATE,
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    compile_context,
    message_text,
    message_tokens,
    recency_window,
    reconcile,
    render_tool_result,
)
from pico.core.loop.state import Degradation
from pico.llm.budget import estimate_tokens, prompt_budget
from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session import (
    AssistantMessageRecorded,
    PlanSet,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
    connect,
)


def _session() -> Session:
    conn = connect(":memory:")
    return Session(conn, "s1")


def test_render_tool_result_shortens_long_content() -> None:
    content = "y" * 2000
    rendered = render_tool_result(content, fact_id=3)

    assert len(rendered) < len(content)
    assert "fact 3" in rendered
    assert "2000 chars" in rendered
    assert str(estimate_tokens(content)) in rendered


def test_render_tool_result_short_content_not_padded_beyond_overhead() -> None:
    content = "short"
    rendered = render_tool_result(content, fact_id=0)

    assert rendered.endswith(content)
    assert len(rendered) < len(content) + 150


def test_render_tool_result_names_the_same_id_in_summary_and_hint() -> None:
    rendered = render_tool_result("y" * 2000, fact_id=12)

    assert "fact 12 truncated" in rendered
    assert "call read_fact(12) for the full content" in rendered


def test_tool_call_arguments_no_longer_cost_zero() -> None:
    call = ToolCall(id="1", name="write_file", arguments={"content": "z" * 10_000})
    message = Message(role=Role.ASSISTANT, tool_calls=(call,))

    assert estimate_tokens(message_text(message)) > 2_000


def test_message_text_includes_tool_call_names_and_arguments() -> None:
    call = ToolCall(id="1", name="write_file", arguments={"path": "a.txt"})
    message = Message(role=Role.ASSISTANT, content="here", tool_calls=(call,))

    text = message_text(message)

    assert "here" in text
    assert "write_file" in text
    assert "a.txt" in text


def test_message_text_of_tool_message_is_its_result_content() -> None:
    session = _session()
    session.append(ToolCallRecorded(name="echo", arguments={}, result="out", is_error=False))
    tool_message = next(m for m in session.messages() if m.role is Role.TOOL)

    assert message_text(tool_message) == "out"


def _tokens(messages: list[Message]) -> int:
    return sum(estimate_tokens(message_text(message)) for message in messages)


def _tool_pair(fact_id: int, result: str) -> list[Message]:
    return [
        Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id=str(fact_id), name="read_file", arguments={}),),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id=str(fact_id), content=result, is_error=False),
        ),
    ]


def test_recency_window_returns_a_short_conversation_unchanged() -> None:
    messages = [
        Message(role=Role.USER, content="question"),
        *_tool_pair(1, "evidence"),
        Message(role=Role.ASSISTANT, content="answer"),
    ]

    assert recency_window(messages, budget=10_000) == messages


def test_recency_window_keeps_the_whole_transcript_when_the_budget_allows() -> None:
    messages: list[Message] = []
    for index in range(20):
        messages.append(Message(role=Role.USER, content=f"question {index}"))
        messages.append(Message(role=Role.ASSISTANT, content=f"answer {index}"))

    assert recency_window(messages, budget=1_000_000) == messages


def test_recency_window_keeps_a_pair_straddling_the_cut_atomic() -> None:
    messages: list[Message] = [Message(role=Role.USER, content="the task")]
    for fact_id in range(10):
        messages.extend(_tool_pair(fact_id, f"result {fact_id} " + "x" * 400))

    window = recency_window(messages, budget=_tokens(messages) // 2)

    assert window[0].content == "the task"
    assert window[1].role is Role.ASSISTANT
    assert window[1].tool_calls
    for previous, current in pairwise(window[1:]):
        if current.role is Role.TOOL:
            assert previous.tool_calls or previous.role is Role.TOOL


def test_recency_window_demotes_tool_results_before_dropping_units() -> None:
    messages = [Message(role=Role.USER, content="old question")]
    for fact_id in range(3):
        messages.extend(_tool_pair(fact_id, "x" * 2_000))
    messages.append(Message(role=Role.USER, content="latest question"))
    budget = _tokens(messages) - 300

    window = recency_window(messages, budget=budget)

    assert len(window) == len(messages)
    first_result = window[2]
    assert first_result.tool_result is not None
    assert "fact 0 truncated" in first_result.tool_result.content


def test_recency_window_collapses_to_the_pinned_messages_when_nothing_else_fits() -> None:
    messages: list[Message] = []
    for index in range(3):
        messages.append(Message(role=Role.USER, content=f"old question {index}"))
        messages.append(Message(role=Role.ASSISTANT, content=f"old answer {index}"))
    messages.append(Message(role=Role.USER, content="latest question"))
    for fact_id in range(12):
        messages.extend(_tool_pair(fact_id, "y" * 1_000))

    window = recency_window(messages, budget=10)

    assert window == [messages[0], messages[6]]
    assert _tokens(window) <= 10


def test_recency_window_evicts_the_current_turn_to_fit_a_resumed_session() -> None:
    messages = [
        Message(role=Role.USER, content="the task"),
        Message(role=Role.ASSISTANT, content="earlier answer"),
        Message(role=Role.USER, content="latest question"),
    ]
    for index in range(12):
        messages.append(Message(role=Role.ASSISTANT, content=f"chunk {index} " + "a" * 2_000))
    budget = 2_000

    window = recency_window(messages, budget=budget)

    assert _tokens(window) <= budget
    assert window[0].content == "the task"
    assert any(message.content == "latest question" for message in window)
    assert not any(message.content.startswith("chunk 0 ") for message in window)
    kept = [message.content.split()[1] for message in window if message.content.startswith("chunk")]
    assert kept == ["9", "10", "11"]


def _pinned_messages(messages: list[Message]) -> list[Message]:
    users = [position for position, message in enumerate(messages) if message.role is Role.USER]
    return [messages[position] for position in sorted({users[0], users[-1]})] if users else []


def test_recency_window_estimate_fits_or_collapses_to_the_pinned_messages() -> None:
    rng = random.Random(7)
    for _ in range(200):
        messages: list[Message] = []
        for _ in range(rng.randint(0, 30)):
            size = rng.choice([5, 200, 3_000])
            roll = rng.random()
            if roll < 0.35:
                messages.append(Message(role=Role.USER, content="u" * size))
            elif roll < 0.7:
                messages.append(Message(role=Role.ASSISTANT, content="a" * size))
            else:
                messages.extend(_tool_pair(len(messages), "r" * size))
        budget = rng.choice([50, 500, 5_000])

        window = recency_window(messages, budget=budget)

        estimate = sum(message_tokens(message) for message in window)
        if estimate > budget:
            assert window == _pinned_messages(messages)


def test_recency_window_demoted_handle_names_the_correct_fact_id() -> None:
    messages = [Message(role=Role.USER, content="question"), *_tool_pair(41, "z" * 5_000)]

    window = recency_window(messages, budget=100)

    demoted = window[-1]
    assert demoted.tool_result is not None
    assert "read_fact(41)" in demoted.tool_result.content


def _named_pair(seq: int, name: str, result: str) -> list[Message]:
    return [
        Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id=str(seq), name=name, arguments={}),),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id=str(seq), content=result, name=name),
        ),
    ]


def test_recency_window_demotes_bookkeeping_results_without_a_recovery_hint() -> None:
    messages = [
        Message(role=Role.USER, content="question"),
        *_named_pair(7, "read_fact", "z" * 5_000),
    ]

    window = recency_window(messages, budget=100)

    demoted = window[-1]
    assert demoted.tool_result is not None
    assert len(demoted.tool_result.content) < 5_000
    assert "read_fact(" not in demoted.tool_result.content
    assert "fact 7" not in demoted.tool_result.content


def test_recency_window_demoted_shell_result_keeps_its_fact_handle() -> None:
    messages = [
        Message(role=Role.USER, content="question"),
        *_named_pair(9, "shell", "z" * 5_000),
    ]

    window = recency_window(messages, budget=100)

    demoted = window[-1]
    assert demoted.tool_result is not None
    assert "fact 9 shell() truncated" in demoted.tool_result.content
    assert "read_fact(9)" in demoted.tool_result.content


def test_recency_window_demoted_handle_names_the_producing_call_signature() -> None:
    messages = [
        Message(role=Role.USER, content="question"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id="9", name="read_file", arguments={"path": "core/loop.py"}),),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="9", content="z" * 5_000, name="read_file"),
        ),
    ]

    window = recency_window(messages, budget=100)

    demoted = window[-1]
    assert demoted.tool_result is not None
    assert "read_file(core/loop.py)" in demoted.tool_result.content


def _error_pair() -> list[Message]:
    return [
        Message(role=Role.ASSISTANT, tool_calls=(ToolCall(id="2", name="shell", arguments={}),)),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="2", content="e" * 2_000, is_error=True),
        ),
    ]


def test_recency_window_never_demotes_error_results() -> None:
    messages = [Message(role=Role.USER, content="question"), *_error_pair()]

    window = recency_window(messages, budget=10_000)

    assert window[-1].tool_result is not None
    assert window[-1].tool_result.content == "e" * 2_000


def test_recency_window_evicts_an_over_budget_error_unit_whole() -> None:
    messages = [Message(role=Role.USER, content="question"), *_error_pair()]

    window = recency_window(messages, budget=10)

    assert window == [messages[0]]


def test_recency_window_evicts_oldest_units_first_and_keeps_the_task() -> None:
    messages = [Message(role=Role.USER, content="the task")]
    for fact_id in range(4):
        messages.extend(_tool_pair(fact_id, "x" * 2_000))
    demoted_pair = [
        messages[1],
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="0", content=render_tool_result("x" * 2_000, 0)),
        ),
    ]
    budget = _tokens([messages[0], *demoted_pair, *demoted_pair]) + 1

    window = recency_window(messages, budget=budget)

    assert window[0].content == "the task"
    kept_ids = [
        message.tool_result.tool_call_id for message in window if message.tool_result is not None
    ]
    assert kept_ids == ["2", "3"]
    dropped = {"0", "1"}
    assert all(
        message.tool_result is None or message.tool_result.tool_call_id not in dropped
        for message in window
    )


def test_recency_window_never_repromotes_a_demoted_result() -> None:
    degradation = Degradation()
    messages = [Message(role=Role.USER, content="question"), *_tool_pair(1, "z" * 5_000)]
    tight = recency_window(messages, budget=300, degradation=degradation)

    relaxed = recency_window(messages, budget=100_000, degradation=degradation)

    assert relaxed == tight
    handle = relaxed[-1]
    assert handle.tool_result is not None
    assert "read_fact(1)" in handle.tool_result.content


def test_recency_window_never_restores_a_cut_unit() -> None:
    degradation = Degradation()
    messages = [Message(role=Role.USER, content="the task")]
    for fact_id in range(4):
        messages.extend(_tool_pair(fact_id, "x" * 2_000))
    tight = recency_window(messages, budget=100, degradation=degradation)

    relaxed = recency_window(messages, budget=100_000, degradation=degradation)

    assert relaxed == tight
    kept_ids = {
        message.tool_result.tool_call_id for message in tight if message.tool_result is not None
    }
    assert kept_ids < {"0", "1", "2", "3"}


def test_recency_window_degradation_only_grows_as_the_session_appends() -> None:
    degradation = Degradation()
    messages = [Message(role=Role.USER, content="the task")]
    for fact_id in range(3):
        messages.extend(_tool_pair(fact_id, "x" * 2_000))
    tight = recency_window(messages, budget=100, degradation=degradation)
    degraded_ids = {"0", "1", "2"} - {
        message.tool_result.tool_call_id
        for message in tight
        if message.tool_result is not None and "truncated" not in message.tool_result.content
    }

    grown = [*messages, Message(role=Role.ASSISTANT, content="moving on")]
    relaxed = recency_window(grown, budget=100_000, degradation=degradation)

    assert relaxed == [*tight, grown[-1]]
    for message in relaxed:
        if message.tool_result is not None and message.tool_result.tool_call_id in degraded_ids:
            assert "truncated" in message.tool_result.content


def test_recency_window_still_degrades_further_when_the_budget_demands_it() -> None:
    degradation = Degradation()
    messages = [Message(role=Role.USER, content="the task"), *_tool_pair(0, "small result")]
    recency_window(messages, budget=100_000, degradation=degradation)

    grown = [*messages, *_tool_pair(1, "y" * 8_000)]
    window = recency_window(grown, budget=300, degradation=degradation)

    assert _tokens(window) <= 300
    newest = window[-1]
    assert newest.tool_result is not None
    assert "read_fact(1)" in newest.tool_result.content


def test_compile_context_windows_then_briefs_with_plan_and_facts() -> None:
    session = _session()
    session.append(PlanSet(steps=("find the port",)))
    session.append(UserMessageRecorded(content="what port?"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="port = 8421", is_error=False)
    )

    compiled = compile_context(session, context_size=100_000)

    briefing = compiled[-1]
    assert briefing.role is Role.USER
    assert "Your current plan:" in briefing.content
    assert "[ ] 0. find the port" in briefing.content
    assert "Facts gathered so far:" in briefing.content
    assert "read_fact(" in briefing.content
    assert compiled[:-1] == session.messages()


def test_compile_context_pre_briefing_messages_prefix_the_next_generation() -> None:
    session = _session()
    session.append(PlanSet(steps=("investigate",)))
    session.append(UserMessageRecorded(content="start"))
    session.append(
        ToolCallRecorded(name="shell", arguments={"command": "ls"}, result="a.py", is_error=False)
    )
    earlier = compile_context(session, context_size=100_000)

    session.append(
        ToolCallRecorded(
            name="read_file", arguments={"path": "a.py"}, result="body", is_error=False
        )
    )
    session.append(AssistantMessageRecorded(content="done", thinking=""))
    later = compile_context(session, context_size=100_000)

    assert earlier[-1].content != later[-1].content
    assert earlier[:-1] == later[: len(earlier) - 1]


def test_compile_context_without_plan_or_facts_is_the_bare_window() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(AssistantMessageRecorded(content="hi", thinking=""))

    assert compile_context(session, context_size=100_000) == session.messages()


def test_compile_context_fits_the_budget_after_overhead() -> None:
    session = _session()
    session.append(PlanSet(steps=("tally",)))
    session.append(UserMessageRecorded(content="tally the parts"))
    for index in range(10):
        session.append(
            ToolCallRecorded(
                name="read_file", arguments={"path": str(index)}, result="v" * 3_000, is_error=False
            )
        )
    overhead = 500

    compiled = compile_context(session, 8_192, overhead_tokens=overhead)

    assert _tokens(compiled) <= prompt_budget(8_192) - overhead


def test_compile_context_keeps_dropped_facts_addressable_in_the_index() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="start"))
    for index in range(12):
        session.append(
            ToolCallRecorded(
                name="shell",
                arguments={"command": str(index)},
                result=f"result {index} " + "x" * 4_000,
                is_error=False,
            )
        )
        session.append(UserMessageRecorded(content=f"next {index}"))

    compiled = compile_context(session, context_size=8_192)

    briefing = compiled[-1]
    window = compiled[:-1]
    dropped_fact = facts(session)[0]
    assert f"[{dropped_fact.id}]" in briefing.content
    assert all(dropped_fact.content not in message_text(message) for message in window)


def test_message_tokens_counts_an_attached_image_at_the_flat_estimate() -> None:
    result = ToolResult(tool_call_id="1", content="viewing /tmp/x.png", name="view_image")
    plain = Message(role=Role.TOOL, tool_result=result)
    with_image = Message(role=Role.TOOL, tool_result=result, images=("/tmp/x.png",))

    assert message_tokens(with_image) == message_tokens(plain) + IMAGE_TOKEN_ESTIMATE


def test_message_text_ignores_attached_images() -> None:
    result = ToolResult(tool_call_id="1", content="viewing /tmp/x.png", name="view_image")
    plain = Message(role=Role.TOOL, tool_result=result)
    with_image = Message(role=Role.TOOL, tool_result=result, images=("/tmp/x.png",))

    assert message_text(with_image) == message_text(plain)


def test_reconcile_clamps_the_observed_ratio() -> None:
    assert reconcile(400, 100) == 4.0
    assert reconcile(100, 1_000) == MIN_CHARS_PER_TOKEN
    assert reconcile(10_000, 100) == MAX_CHARS_PER_TOKEN
