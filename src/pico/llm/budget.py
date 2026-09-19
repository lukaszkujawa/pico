COMPLETION_RESERVE_FRACTION = 0.25
COMPLETION_RESERVE_CAP = 4096


def completion_reserve(context_size: int) -> int:
    return min(int(context_size * COMPLETION_RESERVE_FRACTION), COMPLETION_RESERVE_CAP)


def prompt_budget(context_size: int) -> int:
    return context_size - completion_reserve(context_size)


def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))
