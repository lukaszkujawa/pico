# Fact Ledger Purity

Every non-error `ToolCallRecorded` currently becomes a fact, so the memory machinery records itself: `read_fact` re-records the full recovered content as a brand-new fact (duplicating it in storage, in the search scan set, and in the 20-slot fact index), `search_facts` results become facts about facts, and `set_plan`/`complete_step` fill the index with rendered checklists that the briefing already shows. The fact index is the model's only passive awareness of its memory; bookkeeping noise pushes real findings into the "+N earlier facts" dark zone. A fact must be a result of real work — an observation, a computation, a sub-agent's answer, or an explicit note.

## Design decisions

* **Bookkeeping tools do not mint facts.** `facts()` in `core/ledger.py` excludes `ToolCallRecorded` events whose name is one of `read_fact`, `search_facts`, `set_plan`, `complete_step`. The excluded name set is a single constant in `core/ledger.py`, the only place that defines facthood.
* **The session log is untouched.** Every tool call is still recorded as an event; the transcript, replay, and `messages()` are unchanged. Only the derived `facts()` view filters.
* **Fact ids stay stable.** Ids remain the event `seq`, so existing ids never renumber; excluded calls simply have no fact id.
* **The UI never shows a wrong fact id.** `tool_call_step` in `core/loop.py` currently takes `facts(session)[-1].id` for every non-`answer`/`delegate` call; for an excluded tool that would silently label the pane with some *older* fact's id. The fact-id lookup must yield `None` for excluded tools.
* **A handle never points at a non-fact.** `_demote_to_handle` in `core/context.py` assumes every tool result's `tool_call_id` is a recoverable fact id. For results of excluded tools (identified via `ToolResult.name`), demotion renders a plain truncation notice with the preview but **no** `read_fact` hint — the original fact is still recoverable by its own id, which appears in the adjacent tool-call arguments.
* **Citations are unchanged.** `answer` citations validate against `facts()` as before; they now range only over real facts.

## [X] T001 Filter facthood and the fact-id display

### Description

Add the excluded-name constant and filter in `core/ledger.py`; make `tool_call_step` publish `fact_id=None` for excluded tools instead of the last fact's id.

### Acceptance criteria

* After a run of `note`, `read_fact`, `search_facts`, `set_plan`, `complete_step`, and `shell` calls, `facts()` contains only the `note` and `shell` results, with ids equal to their event seqs.
* Recovering a fact with `read_fact` does not change `len(facts())`, the fact index, or the search candidate set.
* `ToolCallFinished` for an excluded tool carries `fact_id=None`; for a fact-minting tool it carries that call's own seq (regression: never an older fact's id).
* An `answer` citing a `note`'s fact id is accepted; citing the seq of a `read_fact` call is rejected as unknown.
* `make check` passes.

## [X] T002 Demote non-fact results without a recovery hint

### Description

In `core/context.py`, branch demotion on `ToolResult.name`: excluded tools get a truncation notice plus preview with no `read_fact(id)` hint; fact results keep the current handle.

### Acceptance criteria

* A demoted `read_fact` result contains no `read_fact(` hint and does not mention its own seq as a fact id.
* A demoted `shell` result keeps the existing handle text pointing at its own fact id.
* Token accounting for the demoted form is unchanged in kind: the demoted message is smaller than the original for large results.
* `make check` passes.
