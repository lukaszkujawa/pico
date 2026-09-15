# Delegate Sub-agents

Roadmap milestone 6 of 7 (see `0008-session-store.md` for the full arc). Not yet broken into tasks.

This is the "recursive agentic tasks" requirement: a `delegate` action (extending `0010`'s vocabulary) that spawns an isolated, constrained inner loop — reusing the declarative loop runner from `0009` with a narrower action set (read-only: no `write_file`/`shell`-that-mutates/`delegate` itself, preventing unbounded recursion) and its own bounded step budget — to answer a single, scoped question and return a typed, evidence-cited result.

Depends on `0009` (the loop runner must already be generic enough to run with a different `LoopConfig` for the sub-agent without code changes — this milestone is the first real test that `0009`'s declarative design actually achieved reusability, not just an abstraction that looks reusable) and `0011` (the sub-agent's verified result is what mints a `Fact`, closing the loop the old architecture used to force "the model claims X" into "a fact citing exact evidence").

Each delegated sub-run gets its own `Session` (from `0008`) rather than sharing the parent's event log directly, so recursion depth and delegation history stay inspectable and bounded. Decide explicitly, when writing tasks: whether nested delegation (a sub-agent delegating further) is allowed in this milestone or deliberately disallowed to keep the first version simple — the old architecture's worker loop notably excludes `delegate` from the worker's own action set for exactly this reason.
