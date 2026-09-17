# Compiled Context Always Fits

`recency_window` in `core/context.py` has two branches with different guarantees. The single-turn branch caps the body at `RECENT_UNITS` and may evict every unit, so it always converges toward the budget. The multi-turn branch protects everything after the last user message from eviction and applies no unit cap to that tail, so a long agentic turn inside a resumed session can only demote tool results; assistant text is undemotable, and the compiled prompt can exceed the budget with no recourse. An over-budget prompt is then silently front-truncated by Ollama — the system prompt is lost first. The budget must be an invariant of compilation, not a hope, and `BudgetExceeded` today fires only after the request was already sent.

## Design decisions

* **One code path, one invariant.** `recency_window` pins the first user message (the task) and, when different, the last user message (the current instruction). Everything else — including units after the last user message — is ordinary body: capped at `RECENT_UNITS`, demotable where it is a non-error tool result, and evictable oldest-first. The two branches collapse into this single shape.
* **The invariant:** the compiled window's estimated tokens are within budget whenever the pinned messages alone fit; otherwise the window is exactly the pinned messages and `BudgetExceeded` reports the irreducible overflow. Eviction of the current turn's own units is safe by design — tool results survive as facts and the plan is re-rendered into every briefing; that is the vision's "context is a cache".
* **Order of relief is unchanged:** demote tool results oldest-first, then evict whole units oldest-first, stopping as soon as the estimate fits.
* **`BudgetExceeded` becomes truthful.** It is published from compilation when even the pinned minimum exceeds the budget — before the request is sent — instead of only from post-hoc reconciliation. Reconciliation keeps updating `chars_per_token`.
* **No behaviour change for sessions that already fit.** Under-budget compilations return the same messages as today.

## [X] T001 Unify the window and enforce the budget invariant

### Description

Rewrite `recency_window` as the single pinned-plus-body shape; move the over-budget `BudgetExceeded` publication into the compile path in `core/loop.py`.

### Acceptance criteria

* Regression: a multi-turn session (two-plus user messages) whose current turn holds many units of long assistant text compiles to an estimate within budget, evicting the oldest of those units; the last user message and the task message are always present.
* Property-style test over generated sessions (varying message counts, roles, sizes): the compiled estimate is within budget, or the result is exactly the pinned messages and `BudgetExceeded` was published.
* Single-turn compilations that fit today produce identical output (existing tests unchanged in expectation).
* Demotion still precedes eviction: a session that fits after demotion alone evicts nothing.
* `make check` passes.
