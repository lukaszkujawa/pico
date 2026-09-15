# Stuckness and Streaks

Roadmap milestone 7 of 7 (see `0008-session-store.md` for the full arc). Not yet broken into tasks.

The circuit-breaker layer, added last and deliberately — it only makes sense once there's a real loop with real actions producing a real event log to analyze (`0008`-`0010` at minimum). Ports the old architecture's two-tier design: soft nudges (streak-triggered text injected into the next prompt, e.g. "you've investigated twice with no new content, act or delegate now") and a hard stop (`assess()`-style stuckness detection: no progress in N steps, repeated identical actions, consecutive tool failures), both computed by pure functions over the session event log (`0008`) rather than loop-local mutable counters — matching the "no hidden state, everything derived fresh from the log" principle throughout the old architecture.

Depends on `0009` (needs stop-condition hooks in the declarative loop config to actually terminate a run on hard stuckness) and benefits from `0010`/`0013` existing so there are enough distinct action/delegate outcomes for the signals (repeated-action, tool-failure-streak, delegate-rejection-streak) to be meaningful rather than trivial on a two-action vocabulary.

Keep the signal set small initially — pick the 2-3 highest-value signals from the old architecture's five (e.g. steps-without-progress and repeated-action-count) rather than porting all five plus context-churn tracking in one pass.
