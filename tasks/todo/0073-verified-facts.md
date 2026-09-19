# Verified Facts

Facts and step answers are the only memory that survives compaction, and nothing today separates a tested finding from a confident guess. The run in `logs/2026-09-19_12-57-31` shows the cost: step 0's answer stated as fact that Playwright 1.63 `headless=True` + `--load-extension` loads extensions — speculation, and false — and step 1 spent a large share of its ~30 generations disproving it. A misspelled flag (`--disable-blink-features=AutomationControlFlags`; the real value is `AutomationControlled`) was then recorded into the "proven" launch recipe of the next handoff. Small models confabulate confidently, but they run inside a container where ground truth is one command away. This milestone lets a note carry a check command; the harness runs it, a passing check stamps the fact verified, and a failing check rejects the note before the falsehood enters memory.

## Design decisions

* **`note` gains an optional `check` parameter** — a shell command that must exit 0 for the note to be recorded. It runs through the same subprocess machinery as the `shell` action (same environment, same 30s timeout), immediately, at note time. No new tool, no schema change: verification is a property of recording, not a separate action.
* **A failing check rejects the note.** The result is a `ToolError` carrying the check command's exit code and its output (truncated to the existing handle preview budget), telling the model to fix either the claim or the check. Rejected notes are `is_error` results, which `facts()` already excludes — a false claim never becomes a fact.
* **A passing check stamps the content, once.** The recorded result is `✓ <content>` — the marker lives in the content string, created at recording and never rewritten (the cache rule 0070 established: shape at creation, never rewrite later). Because the fact index previews `content[:limit]`, the ✓ appears in the digest and in every handle with zero rendering changes.
* **An unchecked `note` is byte-identical to today.** `check` is optional; nothing is enforced, no existing fact is retroactively marked, no re-verification runs at step boundaries or on resume. Absence of ✓ is the signal that a fact is an unverified claim.
* **Descriptions do the steering, as in 0071.** `note`'s description says: when the content claims something about the environment (a command works, a file exists, a server answers), attach a `check` that proves it. The step handoff instruction in `subruns.py` gains one sentence: findings the next step will build on should be noted with a check before answering. No enforcement.
* **Scope guard.** No verification of `answer` content, no critic pass, no fact schema field, no re-running of checks after recording, no changes to `read_fact`/`search_facts`.

## [ ] T001 Checked notes

### Description

Add the optional `check` parameter to the `note` action in `actions/facts.py`, executing it via the shell subprocess machinery from `actions/shell.py`, with pass-stamps and fail-rejects as decided above.

### Acceptance criteria

* `note(content, check)` with a passing check records `✓ <content>` as the result; with a failing check it returns a distinct error naming the exit code and including truncated check output, and no fact is recorded.
* A check that times out or cannot be spawned is a failure, reported with the same instructive shape.
* `note(content)` without `check` produces a result byte-identical to today's.
* The ✓ prefix appears in the fact index line for a verified fact with no changes to `ledger.py` rendering.
* `make check` passes.

## [ ] T002 Steering

### Description

Update the `note` tool description to instruct attaching a `check` for claims about the environment, and extend the step handoff instruction in `loop/subruns.py` with one sentence asking that findings the next step relies on be noted with a check.

### Acceptance criteria

* `note`'s description names the `check` parameter, when to use it, and that ✓ marks verified facts.
* The step instruction addition is a single sentence; the surrounding text is otherwise unchanged.
* No prompt text is rewritten retroactively for existing sessions.
* `make check` passes.
