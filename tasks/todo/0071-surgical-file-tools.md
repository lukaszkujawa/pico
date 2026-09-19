# Surgical File Tools

File content still floods the transcript from the read side. The 85-generation run in `logs/2026-09-19_11-20-42` carries an 8.7KB `read_file` result — the agent reading back its own script — in every prompt to the end of the run, and the reason it read the whole file was to change a small part of it: with only whole-file `read_file` and whole-file `write_file`, every modification is read-everything then rewrite-everything. The agent already knows this is wrong — in `logs/2026-09-19_10-20-00` it improvised its own patch tool with shell heredocs (`src.replace(old, new)` in inline Python) rather than rewrite the file. 0070 elided the write side; this milestone fixes the read-and-modify cycle at the source, so bulk file content stops entering the transcript from either direction. Two changes: a mechanical `edit_file` that replaces a known snippet, and a `read_file` that can return a slice and caps what an uncapped call ingests.

## Design decisions

* **`edit_file(path, old_text, new_text)` is mechanical, not recursive.** It replaces exact text; it never spawns a child. Semantic edits to unread files are already served by `delegate` and plan steps, which provide fresh context by construction — a spawning tool would rebuild `delegate` inside a primitive and, worse, be unusable at `MAX_DELEGATE_DEPTH`, exactly where the hands-on work happens. `edit_file` registers like `read_file`/`write_file` and is available at every depth.
* **`edit_file` fails loudly and precisely.** `old_text` absent from the file → error naming the path; present more than once → error with the occurrence count and the instruction to add surrounding context; `old_text == new_text` → rejected. Success returns one short line (path plus replaced/new sizes) — the result must never echo file content beyond what the caller already supplied.
* **`read_file` gains optional `offset` and `limit`** (1-based start line, line count). A slice result is prefixed with one header line — `lines {start}-{end} of {total}:` — followed by the raw, unnumbered lines. No per-line numbering: a small model will paste what it sees into `old_text`, and numbered lines would poison exact-match editing.
* **An uncapped read of a big file returns a capped result.** Over `READ_FILE_CAP_CHARS = 4000`, `read_file(path)` returns the first lines up to the cap plus a final line: `… {total_chars} chars / {total_lines} lines total — pass offset and limit to read more`. At or under the cap, behaviour is byte-identical to today. The cap is applied when the result is created, so the rendered message is stable forever — the same cache rule 0070 established: shape at creation, never rewrite later.
* **Tool descriptions steer the choice.** `edit_file`'s description says to use it when the exact current text is known (a file just written or just read) and to delegate the edit when it is not. `read_file`'s description names the slice parameters. The descriptions are how the routing happens; there is no enforcement.
* **Scope guard.** No change to `write_file` (0070's elision stands), no recursive behaviour in any tool, no retroactive trimming of past results, no line-numbered output.

## [ ] T001 Mechanical edit_file

### Description

Add `edit_file` to `actions/files.py` in the house closure shape, register it in the catalog, and cover the failure modes.

### Acceptance criteria

* `edit_file` replaces a uniquely-occurring `old_text` and reports success in one line without echoing file content.
* Absent text, ambiguous text (with count), identical `old_text`/`new_text`, and unreadable path each produce a distinct, instructive error; the file is unmodified in every failure case.
* The tool is registered at every depth alongside `read_file` and `write_file`.
* `make check` passes.

## [ ] T002 Sliced and capped read_file

### Description

Add optional `offset`/`limit` to `read_file`; cap uncapped reads at `READ_FILE_CAP_CHARS` with the totals-and-hint trailer.

### Acceptance criteria

* A slice request returns the header line and exactly the requested lines, unnumbered; out-of-range offsets produce an instructive error naming the file's line count.
* An uncapped read over the cap ends with the totals-and-hint line; at or under the cap the result is byte-identical to today's.
* A file read in full via successive slices reconstructs the exact content.
* `make check` passes.
