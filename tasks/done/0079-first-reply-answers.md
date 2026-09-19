# First Reply Answers

Say "Hi Pico" and the runtime turns a greeting into a two-generation ordeal. The first generation replies naturally in text with no tool call; `record` in `loop/generate.py` refuses to end the run (with no plan, `undecided` is true), `policy_step` raises `NARRATION_PRESSURE`, and `advance` issues `DECISION_NUDGE` — which literally instructs the model to "say which one and why, then do it". Generation two obeys: it narrates its reasoning to the runtime ("You're right to push me to decide…"), and the TUI renders that runtime-directed commentary as a second reply to the user, followed by the settled answer. Two generations and 39 seconds for hello, with a leaked meta-monologue in the middle. The machinery is right for tasks — mid-run narration drift is real — but it has no concept of a conversational turn. The rule this milestone adds: decision pressure only starts building after the first generation; a model that answers in its first attempt is allowed to just answer.

## Design decisions

* **A text-only first generation is the answer.** At depth 0, when the first generation of a run produces non-empty text, no tool calls, and no plan has been set in the run, the run ends immediately with that text as the answer. No nudge, no second generation. The pressure machinery is untouched from the second generation onward: later text-only generations keep today's press, because a model narrating instead of acting mid-task is exactly what `NARRATION_PRESSURE` and `NO_ACTION_NUDGE` exist to catch.
* **The adopted reply settles like an answer, not a salvage.** It flows through the existing settle publication (the `_publish_degraded_answer` shape in `loop/policy.py`) but marked accepted and complete, so the TUI mounts a normal answer pane and the mailbox outbox records `status: answered`. Citation and verify machinery in the `answer` action is not involved — a conversational reply has no facts to cite.
* **Root runs only.** Children spawned by `delegate` or plan steps always carry an explicit task; a child narrating on its first generation is drift, not conversation. Depth above 0 keeps today's behaviour unchanged, `conclude` included.
* **The nudge stops soliciting narration.** Whenever the press machinery still fires — from generation two on — `DECISION_NUDGE` no longer asks the model to "say which one and why". It instructs the model to act, without explaining the choice, so runtime-directed reasoning stops leaking into the transcript as a reply to the user.
* **A thinking-only or empty first generation is not an answer.** Adoption requires non-empty text; a first generation with only thinking, or nothing, follows today's actionless path.
* **Scope guard.** No change to the answer action, the stuckness or budget rules, `undecided`, child budgets, or the TUI. No attempt to classify conversational versus task intent beyond the first-generation rule.

## [X] T001 Adopt the first text-only generation as the answer

### Description

End a depth-0 run whose first generation is text-only, with no plan set, by settling that text as an accepted, complete answer.

### Acceptance criteria

* A run whose first generation returns only text ends after one generation in `Answered` with that text; the bus carries a settled answer marked accepted and complete, and the mailbox outbox records it as answered.
* A first generation that calls any tool, or a run where a plan exists, or any later text-only generation, behaves exactly as today, covered by tests for each case.
* A child run's first text-only generation still receives the press, not adoption.
* A thinking-only first generation follows the existing actionless path.
* `make check` passes.

## [X] T002 Nudge without narration

### Description

Reword `DECISION_NUDGE` to demand the action without soliciting an explanation.

### Acceptance criteria

* The nudge no longer contains "say which one and why" or any instruction to explain; it still names both `set_plan` and `answer` and the cause.
* Existing decision-machinery tests pass with the new wording.
* `make check` passes.
