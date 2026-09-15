# Action Vocabulary

Roadmap milestone 3 of 7 (see `0008-session-store.md` for the full arc). Not yet broken into tasks.

`0009` gives Pico a declarative loop that can run arbitrary configured steps; this milestone gives it a first real, typed set of actions the model can take beyond free-text tool calls — the equivalent of the old architecture's `action.py` vocabulary, scoped down to what a small local model actually needs early on. Candidates to start with (confirm/trim when writing tasks, don't port all nine of the old vocabulary at once): `answer` (terminate with a final response), `read_file`, `write_file`, `shell`. Each action is a typed, validated dataclass (required fields, type-checked arguments) dispatched through the loop from `0009`, with every dispatch recorded as a `SessionEvent` (`0008`) — not just executed and forgotten.

Validation failures should be recoverable: an invalid action's error feeds back to the model as context for a retry, bounded by a max-attempts limit, rather than crashing the run (matching the old architecture's `parse_action_with_retry`, sized down).

This is also where `pico.core.tools.ToolRegistry` and this new action vocabulary need their relationship decided: are actions a superset of tools, a replacement for tools, or a layer on top of the existing tool-calling mechanism? Resolve this explicitly rather than running two parallel, overlapping systems.
