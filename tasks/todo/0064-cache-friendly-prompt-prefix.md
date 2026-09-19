# Cache-Friendly Prompt Prefix

The prompt is assembled cache-hostile. `compile_context` (context.py) returns `[briefing, *recency_window(...)]`, and `assemble` (loop/prompt.py) places that right after the system prompt — so the message order is `[system] + [briefing] + [transcript] + [trailer]`. The briefing holds the fact index (a sliding, deduped window of the newest 20 facts) and the rendered plan, which means its content changes on virtually every generation that accomplished anything. Under provider prefix caching (Anthropic cache breakpoints, OpenAI automatic prefix cache), everything after the first divergent token is a cache miss: the cacheable prefix collapses to the system prompt while the entire transcript behind the briefing is re-processed at full price, every generation, in a loop whose defining behaviour is re-sending the whole conversation each iteration. A second, smaller churn source compounds it under context pressure: `recency_window` recomputes demotion and cutting from scratch each call, so the set of demoted-to-handle results and cut units can differ between adjacent generations, mutating the transcript's middle even when nothing new happened to those messages.

## Design decisions

* **The briefing moves to the end: `[system] + [transcript] + [briefing] + [trailer]`.** The transcript is append-mostly, so the stable prefix grows with the conversation instead of being capped at the system prompt. The briefing stays a synthetic user message with unchanged content; recency-weighted attention favours the prompt's end for orientation content, so this is at worst quality-neutral. Budget accounting is unchanged — the briefing's tokens are still subtracted before the window is sized.
* **Degradation becomes monotone within a run.** The runner remembers how far demotion and cutting have progressed (a watermark over message positions, which are stable because the log is append-only) and never un-degrades: once a tool result is a handle it stays a handle, once a unit is cut it stays cut. `recency_window` may still degrade *further* when the budget demands it; it may never degrade less. The watermark lives on runner state alongside `chars_per_token` — in-memory is enough, since a process restart loses cache warmth anyway.
* **Fitting still wins over stability.** The watermark is a floor, not the algorithm: after applying it, the existing oldest-first demote-then-cut logic runs as today until the window fits. No prompt may exceed the budget to preserve a cache prefix.
* **Scope guard.** No llm-layer changes (no cache_control breakpoints, no provider-specific API use), no change to briefing content, fact index size, or demotion/cut rules themselves. Restriction-driven tool-spec churn at crossroads/last-words is accepted — it is rare and end-of-run.

## [ ] T001 Briefing at the end of the prompt

### Description

Reorder assembly so the briefing follows the recency window instead of preceding it; `assemble` sends `[system] + [window] + [briefing] + [trailer]`.

### Acceptance criteria

* `compile_context` output ends with the briefing message; `assemble` keeps the trailer as the final message when present.
* Briefing content, token budgeting, and the pinned-message rules are byte-identical to before — only position changes.
* Context and prompt tests assert the new order; a test states the intent: the messages before the briefing are a prefix of the previous generation's messages for an append-only session.
* `make check` passes.

## [ ] T002 Monotone degradation watermark

### Description

Make demotion and cutting sticky within a run: carry a watermark in runner state, feed it to `recency_window`, and advance it whenever degradation goes further.

### Acceptance criteria

* Given the same session grown by appending only, consecutive `recency_window` results never re-promote a handle back to a full result and never restore a cut unit.
* A test drives two adjacent generations where naive recomputation would degrade less the second time, and asserts the degraded set only grows.
* The window still fits the budget in every case the existing tests cover.
* `make check` passes.
