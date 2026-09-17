# Citation Repair

Live session 2026-09-17_21-14-26-d4ac39: asked for LLM news from Reddit, the model tried to answer four times. Attempt one cited fact [39], which does not exist; the rejection said only `unknown fact citation(s): [39]`. Attempt two cited [39] again. Attempt three dropped citations entirely and was rejected for the missing field. Before attempt four the model went hunting with `read_fact` on an unrelated fact, then inverted the contract: it reshaped the answer around the fact ids it knew existed, padding a Reddit-news answer with a two-turns-old Moltbook signup because fact [6] was citable. The groundedness mechanism, under a small model, degraded into content drift.

The rejection taught nothing. It named what was wrong while leaving the model in exactly the informational state that produced the error — no valid ids, no route to find them, no instruction to keep the answer intact while repairing. The codebase already knows the fix: the stuckness nudge teaches by naming the tool and the argument ("you already ran X — its result is fact 12; use read_fact(12) or do something new"), and the decision nudge names the legal moves. Citation rejection gets the same treatment: state the fact plainly, then hand over the move — the citable index inline when it is small, a concrete `search_facts` direction when it is not — and pin the answer content so repair means fixing citations, not rewriting the answer to fit them.

## Design decisions

* **The rejection carries the repair route, chosen by ledger size.** When the citable facts are few, the rejection inlines the fact index itself — the menu costs nothing and the next attempt picks from it with no extra round trip. When the ledger is large, inlining would bloat the transcript, so the rejection directs the model to `search_facts` with a concrete example query phrased from a claim ("describe the claim in plain words, e.g. search_facts(\"reddit request blocked by bot detection\")"). One constant decides which route, by fact count.
* **The index render is reused, not reinvented.** The inline menu is `fact_index` over the session's facts — the same lines the compiled context shows — so ids and previews are consistent everywhere the model sees them.
* **Content is pinned in every citation rejection.** The message ends with the same instruction in both routes: keep the answer's content, fix only the citations, and drop any claim no fact supports. Dropping unsupported claims is the contract working, not an escape from it.
* **A menu is a menu, not an endorsement.** The rejection presents ids that exist; it never implies they support the model's claims. The validator's contract stays mechanical — cited ids must exist — and nothing in the wording suggests the runtime vouches for relevance.
* **Citation misses become rejections, not invalid actions.** Today unknown citations raise `InvalidActionError`, counting toward the five-in-a-row invalid-action death alongside genuine protocol violations. An honest repair attempt is not a protocol violation: return a rejected `AnswerOutcome` instead. The loop stays bounded without that counter — rejected answers are error tool calls, so the tool-failure streak, decision pressure, and the budget all still floor a repair spiral.
* **A missing or malformed `citations` field teaches the same lesson.** The generic `missing required field 'citations'` from argument parsing becomes a citation rejection with the same route and pinning text, so attempt three's failure mode also gets a road back.
* **Out of scope, recorded so it is not forgotten:** rejecting vacuous `verify` commands (`echo`, bare `test` on paths the answer makes no claims about) — the same session gamed verify with `echo "task finished"`; an `[uncited]` degradation floor after repeated repair failures, symmetric with `UNVERIFIED_PREFIX`; and adversarial spot-checking that a cited fact actually supports its claim, which is the real groundedness work and needs its own milestone.

## [X] T001 Teaching citation rejections

### Description

In `core/actions/answer.py`, replace the bare unknown-citation raise with a rejection whose reason teaches the repair. Build the message from three parts: the plain fact ("fact [39] does not exist" — naming every unknown id); the route — the inline `fact_index` of the session's facts when their count is at most a new `INLINE_CITATION_INDEX_MAX` constant, otherwise the `search_facts` direction with a concrete example query; and the pinning instruction ("keep your answer's content, fix only the citations, and drop any claim no fact supports"). Return it as a rejected `AnswerOutcome` rather than raising, so repair attempts stop counting as invalid actions. Give the missing-or-malformed `citations` field the same treatment: catch it in the answer action and reject with the same route and pinning text instead of the generic parsing error.

### Acceptance criteria

* An answer citing a nonexistent fact in a small ledger is rejected with a message naming the bad id(s), containing the full fact index with real ids, and ending with the pinning instruction; the model's next answer citing a listed id is accepted.
* The same rejection against a ledger larger than the threshold contains no index but names `search_facts` with an example query and the pinning instruction.
* An answer with no `citations` field is rejected with the teaching message, not `missing required field 'citations'`.
* Unknown-citation and missing-citation rejections do not increment the invalid-action counter; five in a row no longer stop the run through that counter, while each still records an error tool call.
* Valid answers, result-shape checking, and verify behaviour are unchanged under the existing tests.
* `make check` passes.

## [X] T002 Final soundness sweep

### Description

Run `make check` over the finished milestone and fix everything it surfaces — lint, format, strict typing, dead code, `tach` boundaries, tests, build. Read `core/actions/answer.py` top to bottom once more purely for readability: the rejection builder should read as one small, obvious function beside the action it serves.

### Acceptance criteria

* `make check` passes clean from a fresh run.
* No orphaned symbols remain from the replaced error paths.
