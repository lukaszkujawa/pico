# Unified Tool Dispatch

Tool dispatch is split across two mechanisms. `ToolRegistry` executes the self-contained tools, while `tool_call_step` in `core/loop.py` name-dispatches `answer`, `delegate`, `search_facts`, and `shell` through ~130 lines of near-identical `try/except` arms — and those four are also registered with decoy `execute=lambda _: ""` bodies whose only purpose is carrying a spec. Two dispatch paths, duplicated error mapping, and registrations that lie about what they do. One dispatch path with one error-mapping site, with no change to the model-visible protocol.

## Design decisions

* **Two honest kinds of tool.** A plain tool executes from its arguments alone (`read_file`, `write_file`, `note`, `read_fact`, `set_plan`, `complete_step`, `load_table`, `sql`). A runner action needs the runner (`shell` for streaming panes, `answer`, `delegate`, `search_facts`). Both are declared with their spec in one place; the decoy registrations are deleted.
* **One dispatch site.** `tool_call_step` resolves each call to its plain tool or runner action and invokes it through a single code path. The exception mapping lives exactly once: `InvalidActionError`/`UnknownToolError` → invalid attempt, `ToolError`/`LLMError` → error result, cancellation (`SearchCancelled`, `runner.cancel`) → cancelled run. The invalid-streak counter and its reset behaviour are unchanged.
* **Vocabulary is assembled, not faked.** The specs offered to the model come from the plain tools plus the runner actions; `delegate` is omitted at `MAX_DELEGATE_DEPTH` exactly as today.
* **`answer` stays special only in its result.** Its handler returns the existing `AnswerOutcome`, and `AnswerSettled` vs `ToolCallFinished` publication is decided from that — not from string-matching the name in multiple places.
* **Recording is uniform.** Every call still appends one `ToolCallRecorded`; fact-id lookup and event publication happen once, after the unified dispatch, for all tools alike.
* **Pure refactor.** No tool gains or loses behaviour; the model sees identical specs, results, and error texts. Existing tests may be restructured but their asserted behaviour stands.

## [X] T001 Collapse dispatch into one path

### Description

Introduce the runner-action shape alongside `Tool`, delete the decoy registrations, and rewrite `tool_call_step` around a single resolve-execute-record sequence with one exception-mapping site.

### Acceptance criteria

* No registered tool has a placeholder executor; grepping `src/` for `lambda _: ""` finds nothing.
* The spec list sent to the model is unchanged at depth 0 and omits `delegate` at `MAX_DELEGATE_DEPTH` (assert equality against the current vocabulary).
* Behaviour parity holds under the existing loop tests: invalid-argument handling, the invalid-streak stop, answer acceptance/rejection/verification, delegate success and failure texts, search cancellation and `LLMError` handling, and shell streaming events are all unchanged.
* `tool_call_step` contains a single `try/except` mapping tool exceptions to outcomes.
* `make check` passes.
