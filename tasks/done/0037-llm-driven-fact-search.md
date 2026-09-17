# LLM-Driven Fact Search

Live run 2026-09-17_12-29-27: the model called `search_facts` 14 times and got "no facts match" 12 times, because it writes keyword queries ("review findings architecture core loop") and the tool requires a literal substring. Search over the ledger is the core recall capability, so the matcher becomes the LLM itself — no string-search heuristics, no search infrastructure.

## Design decisions

* **Search is a tool-less LLM sub-task**, three passes: scan, refine, reduce. It makes plain `llm.stream` calls with no tool specs, so it cannot recurse or take actions.
  * **Scan**: page through every fact as `[id] source: preview` lines, pages sized from `prompt_budget(context_size)` and the runner's `chars_per_token`; each page asks which ids look relevant to the query, answered as comma-separated ids or `none`.
  * **Refine**: page the shortlisted facts' full content (each truncated to a bounded length) and ask which actually matter.
  * **Reduce**: one call over the survivors produces the tool result — one line per fact, `[id] <why it is relevant>`, most relevant first — followed by the existing `read_fact(id)` recovery hint.
* **Model output is untrusted**: parse ids as integers, drop unknown ids, treat unparseable replies as selecting nothing. A pass that selects nothing short-circuits the later passes.
* **Never a dead end**: when nothing survives, the result is "no relevant facts found for {query}" plus the tail of the fact index, so the model can steer.
* **Dispatch moves into the loop**: `search_facts` joins `answer`/`delegate`/`shell` as a name-dispatched action in `tool_call_step`, because it needs `runner.llm`, `runner.session`, `runner.context_size`, and `runner.chars_per_token`. The registry keeps only its spec. The substring implementation in `actions.py` is deleted.
* **Same failure and cancellation contract as delegate**: `runner.cancel` is checked between LLM calls (a cancelled search cancels the run without recording a result); an `LLMError` during search records the call as a failed tool result and the run continues.
* **The spec tells the truth**: the tool description says relevance is judged by a sub-task over all recorded facts and results are ids plus reasons, recoverable with `read_fact`.

## [X] T001 Replace substring search with the sub-task

### Description

Implement scan/refine/reduce in `core/loop.py` (or a sibling module it calls), delete the substring matcher from `core/actions.py`, keep the `search_facts` spec registered, and update its description.

### Acceptance criteria

* The queries that failed in the live run, replayed against a seeded ledger with a `ScriptedClient`, return the planted relevant fact ids with reasons; a query with no relevant facts returns the no-results message with the fact-index tail.
* Sub-task calls send no tool specs; a search inside a delegate child works and cannot spawn further work.
* Hallucinated or malformed id replies never raise: unknown ids are dropped, garbage selects nothing.
* Cancelling mid-search cancels the run without recording a search result; an `LLMError` mid-search records a failed `search_facts` call and the run continues to the next generation.
* `make check` passes.
