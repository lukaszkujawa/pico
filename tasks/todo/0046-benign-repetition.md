# Benign Repetition

Live run 2026-09-17_16-41-51: the agent finished the review — tests run, lint clean, plan checked off — and was killed one generation before writing its answer with "run stopped as stuck: repeated read_fact(27) 6 times this turn". Three mechanisms interacted. The windowed repetition signal (0044) counts "since the last user message", and a headless run has exactly one, so the window silently spans the entire run: six recalls spread over five productive minutes read as a tight loop. The windowed nudge itself steered the model into the killing pattern — 26 prompts said "use read_fact(27) or do something new", the model obeyed, and each obedience incremented the counter that executed it. And a failed `git clone` (no git installed) plus its successful retry counted as a repeat, so a stale "you already ran <clone>" nudge rode along for minutes and pushed the model into a pointless second clone. Meanwhile ten re-reads of `loop.py` minted ten identical facts, pushing real findings into the "+N earlier facts" dark zone, and `render_call`'s 40-char middle elision made `uv run vulture` and `pyright` commands indistinguishable (`…ure`, `…ight`) even to the search sub-task.

The principle this milestone restores: supervision punishes only pathology, and termination belongs to the generation budget (0045). Re-recalling a fact after compaction is the memory design working, not failing.

## Design decisions

* **The windowed signal never stops a run.** `assess` drops the windowed-count reason entirely; `stuck` can only come from the consecutive-identical streak and the consecutive-failure streak, which were never the problem. Endless-but-varied runs are already terminated by `MAX_RUN_STEPS` (0045) — that is the single owner of "this has gone on too long".
* **The window is bounded by generations, not by user messages.** `_trailing_tool_calls` additionally stops after `WINDOW_GENERATIONS = 10` `AssistantMessageRecorded` events, so "this turn" means "recently" even in a headless run with one user message. The user-message break stays; nudges remain render-time only (0014's log-truthfulness principle is untouched).
* **Recall is exempt from the windowed count.** Calls whose name is in `BOOKKEEPING_TOOLS` never enter the windowed counter: repeating `read_fact` or `search_facts` is the recovery path the system prompt advertises, and the nudge for repeated work-tool calls explicitly recommends it. The consecutive-identical streak still covers a genuine `read_fact` busy-loop.
* **Errored calls do not count as repeats of successful ones.** The windowed counter only counts non-error calls, so a failed attempt plus its fixed retry is one occurrence, not a streak of two. Consecutive failures already have their own signal.
* **The fact index dedupes by producing call.** `fact_index` shows only the newest fact for each `(source, arguments)` pair, so re-reading a file replaces its index line instead of stacking ten copies and burying everything else. Fact ids, the ledger, `facts()`, citations, and `read_fact` are untouched — only the index view dedupes.
* **Call signatures keep their distinguishing middle.** `_SIGNATURE_CHARS` rises from 40 to 60 so typical shell commands survive whole; elision still cuts the middle of what remains over-long. Index lines keep their existing one-line bound; the preview yields the extra room.

## [ ] T001 Bound the window, exempt recall, drop the windowed stop

### Description

In `core/stuckness.py`: stop `_trailing_tool_calls` after `WINDOW_GENERATIONS` assistant generations; make `windowed_repeat` skip bookkeeping and errored calls; remove the windowed-count branch from `assess`'s stuck reasons, leaving the nudge tier in place.

### Acceptance criteria

* Six identical `read_fact` calls interleaved with other calls across a run neither nudge nor stop it.
* Six identical non-error `read_file` calls interleaved across a run nudge (naming the existing fact id) but never stop it; the same call repeated six times *consecutively* still stops the run via the streak signal.
* A call repeated twice more than `WINDOW_GENERATIONS` generations ago draws no nudge; the same pair repeated twice within the window does.
* An errored call followed by an identical successful call draws no repeat nudge.
* Streak-based stops, failure streaks, plan-stall, and the no-action nudge behave exactly as before.
* `make check` passes.

## [ ] T002 Dedupe the fact index by producing call

### Description

In `core/context.py`, make `fact_index` keep only the newest fact per `(source, arguments)` before applying the `_INDEX_FACTS` cut; the overflow line counts the remaining hidden facts.

### Acceptance criteria

* Three reads of the same path yield one index line carrying the newest fact's id; the older two remain citable and recoverable via `read_fact`.
* Facts from distinct calls are never merged, including same tool with different arguments.
* The overflow count reflects facts hidden by the cut, and the index never exceeds `_INDEX_FACTS` lines plus the overflow and recovery lines.
* `make check` passes.

## [ ] T003 Widen call signatures

### Description

In `core/ledger.py`, raise `_SIGNATURE_CHARS` to 60. Adjust any renderer or test that assumed the old width; index lines must stay within `_INDEX_LINE_CHARS`.

### Acceptance criteria

* `render_call("shell", {"command": "cd /tmp/pico2 && uv run vulture 2>&1 | tail -10"})` contains `vulture`; the equivalent `pyright` command's signature contains `pyright`.
* Signatures longer than 60 characters still elide from the middle and keep the path or pipe tail.
* Index lines remain one line within the existing length bound.
* `make check` passes.
