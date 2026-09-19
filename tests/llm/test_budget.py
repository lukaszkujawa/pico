from itertools import pairwise

from pico.llm.budget import (
    COMPLETION_RESERVE_CAP,
    COMPLETION_RESERVE_FRACTION,
    estimate_tokens,
    prompt_budget,
)


def test_estimate_tokens_empty_string_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_short_string_matches_formula() -> None:
    text = "abcdefgh"
    assert estimate_tokens(text) == max(1, len(text) // 4)


def test_estimate_tokens_is_monotonic_on_prefixes() -> None:
    text = "a" * 1000
    assert estimate_tokens(text) >= estimate_tokens(text[:500])


def test_estimate_tokens_default_ratio_matches_floor_division() -> None:
    for text in ("", "a", "abcdefgh", "x" * 4_001):
        assert estimate_tokens(text) == (0 if not text else max(1, len(text) // 4))


def test_estimate_tokens_honours_a_denser_ratio() -> None:
    text = "x" * 300

    assert estimate_tokens(text, 3.0) == 100
    assert estimate_tokens(text, 3.0) > estimate_tokens(text, 4.0)


def test_estimate_tokens_keeps_the_minimum_of_one() -> None:
    assert estimate_tokens("ab", 6.0) == 1


def test_prompt_budget_reserves_completion_fraction() -> None:
    assert prompt_budget(1000) == int(1000 * (1 - COMPLETION_RESERVE_FRACTION))


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
