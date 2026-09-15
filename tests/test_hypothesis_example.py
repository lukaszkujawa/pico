from hypothesis import given
from hypothesis import strategies as st


@given(st.text())
def test_reverse_twice_is_identity(s: str) -> None:
    assert s[::-1][::-1] == s
