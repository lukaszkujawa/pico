# Token Budget Calibration

The context budget (`src/pico/core/context.py`) is the load-bearing wall of "compiled context", and its arithmetic is wrong in three known ways.

First, tool-call arguments cost zero. `_message_tokens` counts `tool_result.content` for TOOL messages and `message.content` for everything else — but an assistant message carrying tool calls has `content=""`, so a `write_file` call with a 10k-char `content` argument is fully present in the prompt and invisible to the budget. Second, fixed overhead is unbudgeted: the system prompt, the tool specs Ollama serializes into every prompt, the transient nudge (`0014`), and the injected plan message (`0022`) all ride outside `render_messages`' accounting, silently absorbed by the completion reserve. Third, `estimate_tokens`' `len // 4` is uncalibrated and never reconciled — code, JSON, and non-English text run closer to ~3 chars per token, so the estimate systematically undercounts exactly the content Pico handles most, while `GenerationComplete.prompt_tokens` (`src/pico/llm/types.py`) reports the *real* prompt size after every single call and is currently discarded for this purpose.

One structural note on proportionality: the budget being a fraction of `context_size` is correct, but the completion *reserve* should not scale without bound. Completion needs are roughly absolute — a model writes a few thousand tokens whether its window is 8k or 128k — so `COMPLETION_RESERVE_FRACTION = 0.25` of a large window wastes prompt room the small-model mission needs. Proportional budget, capped reserve.

This milestone lands before `0024` (compaction) on purpose: elision drops whole turns based on this arithmetic, and it must not inherit these holes. It benefits from `0021` — every eval run produces estimate-vs-actual pairs for free.

## Design decisions

* **Count what the prompt contains.** `_message_tokens` additionally counts each `ToolCall` in `message.tool_calls` — its name plus its JSON-serialized arguments (`json.dumps`, matching roughly what the vendor serializes). No message field the prompt carries may cost zero.
* **Overhead is the caller's to declare.** `render_messages` gains an `overhead_tokens: int = 0` parameter subtracted from the budget. `stream_step` (`src/pico/core/loop.py`) computes it per call: the system prompt, the serialized tool specs (`ToolRegistry.specs()` through `json.dumps`, estimated with the same `estimate_tokens`), the pending nudge if any, and the plan message if any. `render_messages` stays a pure function; the loop, which knows what it appends around the rendered conversation, owns knowing its size.
* **Capped reserve.** `prompt_budget(context_size)` becomes `context_size - min(int(context_size * COMPLETION_RESERVE_FRACTION), COMPLETION_RESERVE_CAP)` with `COMPLETION_RESERVE_CAP = 4096`, both module constants. Small windows keep today's behavior exactly (the cap only binds above ~16k); large windows stop donating a quarter of themselves to a reserve that will never fill.
* **Calibrate from observed truth, transiently.** `estimate_tokens` gains a `chars_per_token: float = 4.0` parameter (default preserving current behavior: `len(text) // 4` stays the floor-division semantics via `int(len(text) / chars_per_token)`, minimum 1). `LoopRunner` holds `chars_per_token: float` as transient runner state (the `pending_nudge` pattern): after each `GenerationComplete` carrying `prompt_tokens`, `stream_step` computes the actual ratio — total characters it sent (messages plus overhead, the same accounting as above) divided by reported `prompt_tokens` — and updates the runner's value, clamped to `[2.0, 6.0]`. Subsequent estimates in that run use it. Nothing is persisted; every run relearns from its first call, which costs one slightly-off estimate and keeps the session log free of derived state.
* **Alarm on the lie that matters.** When reported `prompt_tokens` exceeds `context_size` minus the (capped) reserve — the estimate approved a prompt that actually overflowed the budget — publish a new bus event `BudgetExceeded(estimated: int, actual: int, budget: int)` (`src/pico/core/events.py`). No behavior change on the event yet: it exists so the debug `RunLog` (`0015`) records it and eval runs surface it; reacting to it is compaction's business (`0024`) once the arithmetic beneath it is trustworthy.

## [ ] T001 Honest message and overhead accounting

### Description

Extend `_message_tokens` to count tool-call names and serialized arguments; add the `overhead_tokens` parameter to `render_messages`; compute and pass the per-call overhead from `stream_step` per the design decisions.

### Acceptance criteria

* `tests/core/test_context.py` covers: an assistant message with a large tool-call argument is no longer estimated at zero and can by itself push a conversation over budget (triggering handle truncation that previously would not fire); `overhead_tokens` shrinks the effective budget by exactly its value; zero overhead reproduces prior behavior byte-for-byte.
* `tests/core/test_loop.py` covers, with a recording client: the overhead passed by `stream_step` grows when a nudge or plan message is present and reflects the registered tool specs.
* Fully annotated, passes strict Pyright.

## [ ] T002 Capped completion reserve

### Description

Implement the capped reserve in `prompt_budget` per the design decisions.

### Acceptance criteria

* `tests/core/test_context.py` covers: at a small context size (e.g. 8192) the budget equals today's fractional value exactly; at a large one (e.g. 65536) the reserve is `COMPLETION_RESERVE_CAP`, not the fraction; the crossover point behaves continuously (no off-by-one cliff).
* Fully annotated, passes strict Pyright.

## [ ] T003 Ratio calibration and the `BudgetExceeded` alarm

### Description

Add the `chars_per_token` parameter to `estimate_tokens`, the transient runner ratio with clamping, the per-call reconciliation in `stream_step`, and the `BudgetExceeded` event per the design decisions. Calls whose `GenerationComplete` lacks `prompt_tokens` leave the ratio untouched.

### Acceptance criteria

* `tests/core/test_context.py` covers: `estimate_tokens` at the default ratio matches current outputs; a ratio of 3.0 estimates a known string accordingly; the minimum-1 floor survives.
* `tests/core/test_loop.py` covers, with scripted `GenerationComplete` token counts: the runner's ratio moves toward the observed value and clamps at both bounds; a second LLM call in the same run estimates with the updated ratio (assert via the recording client on a scenario the default ratio would have trimmed differently); absent `prompt_tokens` changes nothing; a scripted overflow publishes `BudgetExceeded` with the right fields, and a fitting prompt publishes none.
* Fully annotated, passes strict Pyright.

## [ ] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` re-exports (`BudgetExceeded`, `COMPLETION_RESERVE_CAP`). If a live model is configured, run `make evals` and note in the commit message whether any `BudgetExceeded` events fired and how estimate-vs-actual ratios settled.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
