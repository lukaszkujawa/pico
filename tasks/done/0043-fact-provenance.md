# Fact Provenance

Live run 2026-09-17_15-32-14: the agent re-read the same source files seven times each while `read_fact` sat unused, because the fact index renders only a content preview — `[92] read_file: import itertools import json…` — and every Python file looks identical. A fact's identity is the call that produced it, and no surface shows it, so the ledger is unaddressable and the model's only rational recovery is redoing the work. VISION.md lists provenance as first-class state; this makes it so.

## Design decisions

* **A fact knows its call.** `Fact` gains the recorded call's arguments alongside its source name; both come straight from `ToolCallRecorded`, no new events and no migration.
* **One renderer for the call signature.** A single function in `core/ledger.py` renders `name(arguments)` compactly — argument values only, joined, truncated to a bounded length, long values elided from the middle so paths keep their tail (`read_file(/tmp/pico/…/core/loop.py)`).
* **Every recall surface uses it.** The fact index line becomes `[id] signature: preview`; `search`'s scan and refine lines carry the signature so relevance judging sees what was done, not just what came back; the demoted-handle summary names the signature so the model knows what a handle holds before recovering it.
* **The content preview shrinks to make room.** Index lines stay one line within the current length bound; signature first, preview fills the remainder.
* **No behavioural machinery.** No dedup, no caching, no new tools: once facts are addressable, `read_fact` becomes the cheapest path to old results and the existing tools suffice.

## [X] T001 Carry and render the producing call

### Description

Extend `Fact` and `facts()` with the call arguments, add the signature renderer, and thread it through `fact_index`, `search`'s line renderers, and the handle summary in `core/context.py`.

### Acceptance criteria

* Index lines for two `read_file` facts with different paths are distinguishable by path within the first 40 characters; a `shell` fact shows the leading part of its command.
* Search scan and refine lines contain the signature; a query naming a file path finds the fact for that file's read in a seeded ledger even when the preview alone is ambiguous.
* A demoted handle names the signature and still points at the correct fact id.
* Index lines respect the existing one-line length bound.
* `make check` passes.
