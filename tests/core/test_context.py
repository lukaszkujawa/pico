from itertools import pairwise

from pico.core.context import (
    COMPLETION_RESERVE_CAP,
    COMPLETION_RESERVE_FRACTION,
    estimate_tokens,
    message_text,
    prompt_budget,
    render_messages,
    render_tool_result,
)
from pico.core.ledger import facts
from pico.llm.types import Message, Role, ToolCall
from pico.session import (
    AssistantMessageRecorded,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
    connect,
)


def _session() -> Session:
    conn = connect(":memory:")
    return Session(conn, "s1")


def test_estimate_tokens_empty_string_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_short_string_matches_formula() -> None:
    text = "abcdefgh"
    assert estimate_tokens(text) == max(1, len(text) // 4)


def test_estimate_tokens_is_monotonic_on_prefixes() -> None:
    text = "a" * 1000
    assert estimate_tokens(text) >= estimate_tokens(text[:500])


def test_render_tool_result_full_returns_content_unchanged() -> None:
    content = "x" * 500
    assert render_tool_result(content, fact_id=0, level="full") == content


def test_render_tool_result_handle_shortens_long_content() -> None:
    content = "y" * 2000
    rendered = render_tool_result(content, fact_id=3, level="handle")

    assert len(rendered) < len(content)
    assert "fact 3" in rendered
    assert "2000 chars" in rendered
    assert str(estimate_tokens(content)) in rendered


def test_render_tool_result_handle_short_content_not_padded_beyond_overhead() -> None:
    content = "short"
    rendered = render_tool_result(content, fact_id=0, level="handle")

    assert rendered.endswith(content)
    assert len(rendered) < len(content) + 150


def test_render_tool_result_handle_names_the_same_id_in_summary_and_hint() -> None:
    rendered = render_tool_result("y" * 2000, fact_id=12, level="handle")

    assert "fact 12 truncated" in rendered
    assert "call read_fact(12) for the full content" in rendered


def test_prompt_budget_reserves_completion_fraction() -> None:
    assert prompt_budget(1000) == int(1000 * (1 - COMPLETION_RESERVE_FRACTION))


def test_render_messages_fits_budget_unchanged() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(ToolCallRecorded(name="read_file", arguments={}, result="small", is_error=False))

    rendered = render_messages(session, context_size=100_000)

    assert rendered == session.messages()


def test_render_messages_evicts_large_tool_result() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="z" * 10_000, is_error=False)
    )

    rendered = render_messages(session, context_size=100)

    user_messages = [m for m in rendered if m.role is Role.USER]
    tool_messages = [m for m in rendered if m.role is Role.TOOL]
    assert user_messages == [Message(role=Role.USER, content="hello")]
    assert tool_messages[0].tool_result is not None
    assert tool_messages[0].tool_result.content != "z" * 10_000
    assert "fact 2" in tool_messages[0].tool_result.content


def test_render_messages_evicts_oldest_first_and_stops_when_fitting() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="a" * 5_000, is_error=False)
    )
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="b" * 100, is_error=False)
    )

    rendered = render_messages(session, context_size=1_500)

    tool_messages = [m for m in rendered if m.role is Role.TOOL]
    assert tool_messages[0].tool_result is not None
    assert tool_messages[1].tool_result is not None
    assert "fact 2" in tool_messages[0].tool_result.content
    assert tool_messages[1].tool_result.content == "b" * 100


def test_render_messages_best_effort_when_full_eviction_still_over_budget() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="a" * 5_000, is_error=False)
    )
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="b" * 5_000, is_error=False)
    )

    rendered = render_messages(session, context_size=1)

    tool_messages = [m for m in rendered if m.role is Role.TOOL]
    for message in tool_messages:
        assert message.tool_result is not None
        assert "fact" in message.tool_result.content


def test_render_messages_never_evicts_error_tool_result() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="e" * 10_000, is_error=True))

    rendered = render_messages(session, context_size=100)

    tool_messages = [m for m in rendered if m.role is Role.TOOL]
    assert tool_messages[0].tool_result is not None
    assert tool_messages[0].tool_result.content == "e" * 10_000


def test_render_messages_handles_name_the_fact_id_reported_by_ledger() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="e" * 400, is_error=True))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="a" * 5_000, is_error=False)
    )
    session.append(AssistantMessageRecorded(content="thinking", thinking=""))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="f" * 400, is_error=True))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="b" * 5_000, is_error=False)
    )

    rendered = render_messages(session, context_size=1)

    truncated = [
        m.tool_result.content
        for m in rendered
        if m.role is Role.TOOL and m.tool_result is not None and not m.tool_result.is_error
    ]
    assert [fact.id for fact in facts(session)] == [3, 6]
    assert "fact 3" in truncated[0]
    assert "fact 6" in truncated[1]


def test_render_messages_leaves_error_results_intact_among_truncated_facts() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="e" * 4_000, is_error=True))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="a" * 5_000, is_error=False)
    )

    rendered = render_messages(session, context_size=1)

    errors = [
        m.tool_result.content
        for m in rendered
        if m.role is Role.TOOL and m.tool_result is not None and m.tool_result.is_error
    ]
    assert errors == ["e" * 4_000]


def test_tool_call_arguments_no_longer_cost_zero() -> None:
    call = ToolCall(id="1", name="write_file", arguments={"content": "z" * 10_000})
    message = Message(role=Role.ASSISTANT, tool_calls=(call,))

    assert estimate_tokens(message_text(message)) > 2_000


def test_render_messages_truncates_when_tool_call_argument_blows_budget() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(
        ToolCallRecorded(
            name="write_file", arguments={"content": "z" * 40_000}, result="ok", is_error=False
        )
    )

    rendered = render_messages(session, context_size=2_000)

    tool_messages = [m for m in rendered if m.role is Role.TOOL]
    assert tool_messages[0].tool_result is not None
    assert "fact 2" in tool_messages[0].tool_result.content


def test_render_messages_overhead_shrinks_budget_by_exactly_its_value() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="a" * 4_000, is_error=False)
    )
    messages = session.messages()
    total = sum(estimate_tokens(message_text(message)) for message in messages)
    context_size = 8_000
    slack = prompt_budget(context_size) - total

    assert render_messages(session, context_size, overhead_tokens=slack) == messages

    tight = render_messages(session, context_size, overhead_tokens=slack + 1)
    tool_messages = [m for m in tight if m.role is Role.TOOL]
    assert tool_messages[0].tool_result is not None
    assert "fact 2" in tool_messages[0].tool_result.content


def test_render_messages_zero_overhead_reproduces_prior_behaviour() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hello"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="a" * 5_000, is_error=False)
    )

    assert render_messages(session, 1_500, overhead_tokens=0) == render_messages(session, 1_500)


def test_prompt_budget_small_context_uses_fraction() -> None:
    assert prompt_budget(8192) == int(8192 * (1 - COMPLETION_RESERVE_FRACTION))


def test_prompt_budget_large_context_caps_the_reserve() -> None:
    assert prompt_budget(65536) == 65536 - COMPLETION_RESERVE_CAP


def test_prompt_budget_is_continuous_across_the_crossover() -> None:
    crossover = int(COMPLETION_RESERVE_CAP / COMPLETION_RESERVE_FRACTION)
    budgets = [prompt_budget(size) for size in range(crossover - 4, crossover + 5)]

    assert budgets == sorted(budgets)
    assert all(later - earlier <= 1 for earlier, later in pairwise(budgets))


def test_prompt_budget_never_reserves_more_than_the_cap() -> None:
    for size in (1_000, 8_192, 16_384, 32_768, 131_072):
        assert size - prompt_budget(size) <= COMPLETION_RESERVE_CAP


def test_estimate_tokens_default_ratio_matches_floor_division() -> None:
    for text in ("", "a", "abcdefgh", "x" * 4_001):
        assert estimate_tokens(text) == (0 if not text else max(1, len(text) // 4))


def test_estimate_tokens_honours_a_denser_ratio() -> None:
    text = "x" * 300

    assert estimate_tokens(text, 3.0) == 100
    assert estimate_tokens(text, 3.0) > estimate_tokens(text, 4.0)


def test_estimate_tokens_keeps_the_minimum_of_one() -> None:
    assert estimate_tokens("ab", 6.0) == 1


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
