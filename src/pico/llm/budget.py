COMPLETION_RESERVE_FRACTION = 0.25
COMPLETION_RESERVE_CAP = 4096


def completion_reserve(context_size: int) -> int:
    return min(int(context_size * COMPLETION_RESERVE_FRACTION), COMPLETION_RESERVE_CAP)
