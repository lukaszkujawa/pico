# Ledger

Roadmap milestone 4 of 7 (see `0008-session-store.md` for the full arc). Not yet broken into tasks.

Adds the epistemic-discipline layer from the old architecture's `ledger.py`, scoped to what's useful once real actions (`0010`) exist: typed `Goal` and `Fact` entities (start with these two; `Constraint`/`Question`/`Hypothesis` from the old architecture are candidates to add later only if a concrete need appears — do not port all five speculatively) derived from the session event log (`0008`), queryable via SQL against the same SQLite store rather than held as separate mutable state.

The core discipline to port: `answer` (from `0010`'s action vocabulary) should only be able to cite known facts, and facts should only be created through a verifiable path (initially: perhaps any successful action result can become a fact; the old architecture's stronger rule — facts can only be minted by a delegate sub-agent — depends on `0013` and may need to wait until then). Decide and document explicitly how strict this gate is for this milestone's scope, since going straight to the old architecture's full strictness may not make sense before delegation exists.
