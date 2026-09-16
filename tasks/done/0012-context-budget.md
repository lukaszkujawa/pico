# Context Budget

Roadmap milestone 5 of 7 (see `0008-session-store.md` for the full arc).

Today `stream_step` calls `runner.llm.stream(runner.session.messages(), ...)` with the *entire* replayed message history, unbounded, every turn. `config.context_size` is read into `Config` but nothing ever consults it. This milestone makes it do something: a token budget applied to the rendered message list, with large tool-result content evicted (replaced by a short digest + stable handle) oldest-first when the full history would not fit.

## Design decisions (scoped for this milestone)

* **No tokenizer dependency.** Pico has no tokenizer today and none of the existing milestones added one. Token counts are estimated with a simple stdlib heuristic (`len(text) // 4`, the standard chars-per-token rule of thumb) — good enough for a budget that only needs to decide "roughly too big" vs "fits", not exact counts. `GenerationComplete.prompt_tokens` (actual counts from Ollama) is diagnostic-only and not part of the budgeting decision, since it arrives after the request is already sent.
* **Two render levels, not three.** The old architecture's HANDLE/DIGEST/FULL becomes `"full"` and `"handle"`. Pico has no `inspect`-equivalent action yet (`0010`'s vocabulary is `read_file`/`write_file`/`shell`/`answer` — no promotion action exists to wire a third level into), so a middle DIGEST-only level with no way to promote out of it would be dead machinery. `"handle"` *is* the digest: a truncated preview plus a stable reference, in one level. Promotion-on-reinspect is explicitly deferred to whichever future milestone adds an inspect-style action.
* **What's evictable.** Only `ToolCallRecorded.result` content (file reads, shell output — the only unbounded, potentially-large content in the session log). `UserMessageRecorded` and `AssistantMessageRecorded` content is never evicted; it's assumed small (chat turns, not data dumps) and losing it would corrupt the conversation itself rather than just hiding a large artifact. This is the "fixed slices" simplification: history splits into a non-evictable part (user/assistant turns) and an evictable part (tool results), not a fully general multi-slice system/tools/history partition — Pico has no system prompt module yet (`Role.SYSTEM` is defined but unused anywhere), so budgeting a slice for it would be speculative.
* **Eviction order.** Oldest evictable tool result first, matching the old architecture's recency bias (newest context is most likely to still be relevant to the current step). Stop evicting as soon as the rendered total fits the budget; if every tool result is already at `"handle"` level and it still doesn't fit, stop anyway (best-effort, not a hard guarantee — never truncate user/assistant content to force a fit).
* **Handle format.** A handle names the fact it refers to (`facts()` from `0011` already assigns each successful tool result a stable 0-based index) so a future promotion mechanism has something stable to key on: `f"[fact {index} truncated — {len(content)} chars, {tokens} tokens]"` followed by a short prefix of the original content (first ~200 chars). Deterministic given the same session state — no randomness, no new event type, no mutation of the stored event (eviction is a rendering-time transform, applied fresh each call, exactly like `Session.messages()` itself is derived fresh).
* **Budget input.** `config.context_size` is the model's total context window. Reserve a fixed fraction for the model's own response (completion headroom) before computing the prompt budget — reuse a single constant (e.g. 25%) rather than a configurable knob; this can be tuned later if it matters in practice, per VISION's "smallest complete implementation" guidance.

## [X] T001 Token estimation

### Description

In `src/pico/core/context.py`, add `estimate_tokens(text: str) -> int`: returns `max(1, len(text) // 4)` for non-empty text, `0` for empty text. This is the single heuristic used everywhere a token count is needed in this milestone — no per-call-site reimplementation.

### Acceptance criteria

* `tests/core/test_context.py` (new) covers: empty string returns `0`; a short string returns a small positive count consistent with the `len // 4` formula; the function is monotonic (longer text never yields a smaller estimate than a prefix of it).
* Fully annotated, passes strict Pyright.

## [X] T002 Render levels and fact-keyed handles

### Description

Still in `src/pico/core/context.py`:

* `RenderLevel = Literal["full", "handle"]`.
* `render_tool_result(content: str, fact_index: int, level: RenderLevel) -> str` — returns `content` unchanged at `"full"`. At `"handle"`, returns the handle format described in the milestone's design decisions: a bracketed summary line (`fact index`, original char length, estimated token count) followed by a short prefix of `content` (first 200 characters, or the whole string if shorter — never adds a prefix longer than the original content).
* This function is pure and stateless — no `Session`/store access, so it's trivially testable and reusable by T003.

### Acceptance criteria

* `tests/core/test_context.py` covers: `"full"` returns content unchanged; `"handle"` on content longer than 200 characters returns a string strictly shorter than the original, containing the fact index and both a char-length and token-count figure; `"handle"` on content shorter than 200 characters never pads or lengthens it beyond a small fixed overhead for the summary line.
* Fully annotated, passes strict Pyright.

## [X] T003 Budgeted rendering over a session

### Description

Still in `src/pico/core/context.py`:

* `COMPLETION_RESERVE_FRACTION = 0.25` — module constant.
* `prompt_budget(context_size: int) -> int` — returns `int(context_size * (1 - COMPLETION_RESERVE_FRACTION))`.
* `render_messages(session: Session, context_size: int) -> list[Message]` — the eviction algorithm:
  1. Build the full `Message` list exactly as `session.messages()` does today (in fact, reuse `session.messages()` for the non-tool-result shape — do not duplicate the user/assistant/tool-call reconstruction logic already in `pico.session.Session`).
  2. Compute each message's estimated token count via `estimate_tokens` over its rendered text content (`content` for user/assistant messages, `tool_result.content` for tool-result messages).
  3. If the total fits `prompt_budget(context_size)`, return the list unchanged.
  4. Otherwise, walk tool-result messages oldest-first (by position in the list) and replace each one's content with `render_tool_result(..., level="handle")`, using `facts(session)` (from `0011`'s `pico.core.ledger`) to look up each tool result's fact index by matching source order — recomputing the running total after each replacement, stopping as soon as the total fits or every tool result has been converted to `"handle"` level (whichever comes first).
  5. Return the resulting list; the original messages returned by `session.messages()` (and the stored session log) are never mutated — this is a rendering-time view, matching `Session`'s existing "derive fresh, never cache" contract.

Only successful tool results become `Fact`s (per `0011`'s gate); an errored tool-result message has no fact index to key a handle off and is left at `"full"` unconditionally — errors are small and diagnostic, not the large-payload case this milestone targets.

### Acceptance criteria

* `tests/core/test_context.py` covers: a session small enough to fit the budget is rendered unchanged; a session with one huge tool result and a small `context_size` gets that result rendered at `"handle"` level while user/assistant messages stay untouched; a session with multiple large tool results evicts oldest-first and stops as soon as the budget is met (assert a newer large result stays `"full"` while an older one is evicted); a session where even full eviction doesn't fit still returns a list (best-effort, no exception) with every tool result at `"handle"` level; an errored tool-result message is never converted to `"handle"` level regardless of size.
* Fully annotated, passes strict Pyright.

## [X] T004 Wire budgeted rendering into the loop

### Description

`src/pico/core/loop.py`'s `stream_step` currently calls `runner.llm.stream(runner.session.messages(), runner.tools.specs())`. Change it to `runner.llm.stream(render_messages(runner.session, runner.context_size), runner.tools.specs())`.

`LoopRunner` needs a `context_size: int` — add it as a required constructor parameter (after `session`, before `config`, keeping related state grouped) so the runner has the number to pass through; do not reach into a global `Config` from inside `loop.py` (the module has no `pico.config` import today and shouldn't gain one — keep `context_size` an explicit dependency, same pattern as `llm`/`tools`/`bus`/`session`).

`src/pico/app.py`'s `_turn_loop` (and the `LoopRunner(...)` construction inside it) passes `config.context_size` through.

### Acceptance criteria

* `tests/core/test_loop.py` updated: existing `LoopRunner` construction call sites pass a `context_size` (a generous constant like `128_000` for scenarios where eviction isn't the thing under test); at least one new/updated scenario constructs a runner with a small `context_size` and a session containing a large prior tool result, and asserts the `Message` list actually sent to the (recording) LLM client had that content replaced with a handle rather than the raw content.
* `tests/test_app.py` updated for the new `LoopRunner` call signature.
* Fully annotated, passes strict Pyright.

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Add the new public surface (`RenderLevel`, `estimate_tokens`, `render_tool_result`, `render_messages`, `prompt_budget`, `COMPLETION_RESERVE_FRACTION`) to `src/pico/core/__init__.py`.

### Acceptance criteria

* `make check` passes with no errors.
* `grep -rn "context_size" src/pico/core/loop.py src/pico/app.py` shows it flowing from `Config` through `LoopRunner` into `render_messages`.

### Completion

Commit:
