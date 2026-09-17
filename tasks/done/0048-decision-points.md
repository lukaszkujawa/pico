# Decision Points

Live run 2026-09-17_19-32-06: the first run under recursive planning, and the machinery never engaged — `set_plan` was called zero times in 62 generations, so no step ever ran in a fresh context. The model worked reasonably (it found the pgrep self-match test bug, used `delegate` twice, even `note` once) and then ended the run by narrating its review as plain text: the no-tool-calls-with-no-plan path accepted the narration as completion, the `answer` tool was never called, and the run finished with no citations, no verification, and `answer=None` for the caller. The design's safety net — "under-planning hits the budget and forces decomposition" — never fired because the run finished at 62 of 100 generations. Two lessons, both already known from `note` and `complete_step`: voluntary hygiene goes unused, and advice-shaped nudges compete with momentum while a demanded choice does not. The evidence that demands work is in the same logs — the model visibly attends to every nudge; what it was never given is a question it had to answer.

This milestone adds decision points: at deterministic pressure moments the runtime demands a justified choice — decompose or finish — and escalates to enforcement only if the demand is ignored. Try-first survives: while a node is making progress with no pressure signal, nothing interrupts it. The decision stays the model's; the runtime only insists that it be made, said aloud, and acted on. No run ends silently again.

## Design decisions

* **Pressure is deterministic and per-node.** Two signals raise a decision point in a node with no plan (or a fully completed one) and no accepted answer: the node's transcript exceeding the structural recency bound (`RECENT_UNITS` units — the moment its context begins to roll and conclusions start dying), and the budget wind-down threshold where it already exists (the root, 0045). Each signal demands at most once — no nudge storms (0046's lesson).
* **The demand is a question, not advice.** The nudge reads: decision required — `set_plan` to hand the remaining work to fresh agents, or finish with `answer`; say which and why, then do it. The justification requirement is deliberate: the choice lands in the transcript where it can be read, logged, and evaluated, and committing a reason improves the choice itself.
* **Ignoring the demand escalates to a crossroads generation.** If `DECISION_GRACE = 3` generations pass after a demand with still no plan set and no accepted answer, the next generation offers a restricted vocabulary — `set_plan`, `answer`, and `note` only — with the same demand as its nudge. This reuses the restricted-generation mechanism the last-words path introduced (0047); the difference is that a crossroads continues the run afterwards. Both doors stay open: a model that believes it is nearly done answers properly instead of being forced into a grudging plan.
* **Narration is an ignored demand, not an ending.** The text-only-with-no-plan path no longer completes the run; it raises the decision point. After `MAX_CROSSROADS = 2` crossroads generations that still produce neither plan nor answer, the run finishes with the node's last narration recorded as its answer, explicitly marked unverified and uncited — an honest degraded ending in place of a silent `None`.
* **`set_plan` leads with the benefit.** The description opens with why to plan — break a task too big for one context into steps, each run with a fresh context — before the mechanics of hand-off, so planning reads as gaining leverage, not losing control.
* **The wind-down nudge defers to the demand.** When a decision point is active it owns the nudge slot; the plain wind-down wording applies only to nodes with a plan already in motion.

## [X] T001 Pressure signals demand a decision

### Description

Detect the two pressure signals per node — transcript past `RECENT_UNITS` units, and the existing wind-down threshold — and when the node has no plan with unfinished steps and no accepted answer, set the decision-demand nudge, once per signal. Constants and wording in `core/loop.py`.

### Acceptance criteria

* A node whose transcript first exceeds the structural bound gets the demand nudge on its next generation; the same signal never demands twice in that node.
* The root gets the demand at wind-down when it has no active plan; with unfinished plan steps the existing wind-down wording is used instead.
* A node with an active plan or an accepted answer never sees the demand.
* The demand names both options and requires a stated reason.
* `make check` passes.

## [X] T002 Crossroads generation

### Description

If `DECISION_GRACE` generations pass after a demand with no plan set and no accepted answer, run the next generation with the vocabulary restricted to `set_plan`, `answer`, and `note`, carrying the demand as its nudge; other tool calls in it are invalid actions. The run continues normally afterwards. Generalize the last-words restricted-generation mechanism rather than duplicating it.

### Acceptance criteria

* Three post-demand generations without a plan or answer trigger a generation offering only `set_plan`, `answer`, and `note`; a `shell` call in it is rejected as an invalid action.
* Setting a plan at the crossroads flows into step orchestration on the next iteration; answering ends the run through the normal verified path.
* A node that heeds the original demand within the grace window never sees a crossroads.
* The last-words path (budget, stuck, twice-failed step) behaves exactly as before.
* `make check` passes.

## [X] T003 No silent endings

### Description

Remove the run-completing branch for a text-only generation with no plan: route it into the decision point instead. After `MAX_CROSSROADS` crossroads generations that produce neither plan nor accepted answer, finish the run with the node's most recent narration recorded as its answer, marked unverified and uncited, so callers never receive `answer=None` from a run that produced content. Reword `set_plan`'s description to lead with the benefit.

### Acceptance criteria

* A text-only generation with no plan no longer ends the run; it raises the decision demand.
* After `MAX_CROSSROADS` failed crossroads, the run ends with the last narration as its answer, marked unverified; `AnswerSettled` and the headless result carry it.
* A run whose model answers properly at any point is unaffected.
* The `set_plan` description opens with the reason to plan before the hand-off mechanics.
* `make check` passes.
