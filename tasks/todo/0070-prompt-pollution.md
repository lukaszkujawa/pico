# Prompt Pollution

Two kinds of dead weight ride in every prompt, confirmed by measuring the 26-generation run in `logs/2026-09-19_10-20-00`. First: a generation that thinks but says nothing still records `AssistantMessageRecorded(content="", thinking=...)` (the `text or thinking` condition in `loop/record.py`), and `session.messages()` replays every such event as an assistant message with empty content — thinking is deliberately never re-sent, so the message carries zero information. That run accumulated 11 empty assistant messages by its last prompt, each re-sent in every subsequent prompt, and some providers reject empty assistant turns outright. Second: `write_file` tool-call arguments embed the entire file content in the transcript forever. The two largest blocks in that run were two versions of the same script at 10.2KB + 9.2KB — 39% of the whole transcript, re-sent in every prompt from mid-run to the end (~200KB cumulative). The degradation machinery can never reclaim them: demotion targets tool results (`Role.TOOL` messages), and bulky arguments live in assistant messages. The content has no forward value in the prompt — the file is on disk, `read_file` recovers it — and both fixes belong at the same seam: `session.messages()`, the one place events become provider messages.

## Design decisions

* **Both fixes are rendering rules, not event changes.** Events keep recording exactly what happened (`thinking` preserved for the debug log, full `write_file` arguments preserved in the session and as the fact signature source). Only the replay into `Message` objects changes. Stuckness comparison, fact provenance, and `completed_step_results` read events, not rendered messages, and are untouched.
* **Empty assistant messages are skipped at replay.** `messages()` renders `AssistantMessageRecorded` only when `content` is non-empty. `record_assistant_message` keeps appending thinking-only events.
* **`write_file` content renders elided from the very first prompt.** When rendering a `write_file` tool call whose `content` exceeds `ELIDE_CONTENT_CHARS = 500`, the rendered arguments replace `content` with a stub naming the size and the recovery path, e.g. `<9863 chars — on disk at /app/gsearch.py; read_file to recover>`. Applied from the first rendering and computed purely from the single event, the rendered message is byte-identical in every prompt — retroactive rewriting would break the prefix cache 0064 established, so the rule is: elide always, from the start, or not at all.
* **Scope is `write_file.content` only.** Bulky `shell` heredocs show the same disease (a 2.5KB block in the same run) but lack the clean recovery story — the script may exist only in the command. Generalising the elision is out of scope until evidence shows the model copes without seeing its recent commands echoed.
* **Behaviour-preserving otherwise.** Tool execution sees full arguments (elision happens at replay, after execution); results, facts, events, and thresholds are unchanged.

## [ ] T001 Skip empty assistant messages

### Description

Render `AssistantMessageRecorded` into a prompt message only when its content is non-empty.

### Acceptance criteria

* A thinking-only generation appends its event (thinking intact in the session) but adds no message to `session.messages()`.
* No message with `role=ASSISTANT`, empty content, and no tool calls is ever produced by `messages()`, covered by a test replaying a mixed session.
* `make check` passes.

## [ ] T002 Elide write_file content at replay

### Description

When `messages()` renders a `write_file` tool call with `content` over `ELIDE_CONTENT_CHARS`, replace the content argument with a stub carrying the original length and the path-based recovery hint.

### Acceptance criteria

* A `write_file` call with content over the threshold renders with the stub; at or under the threshold renders in full.
* The rendered message for a given event is byte-identical regardless of how many later events the session holds, shown by a test rendering the same session at two lengths.
* The stored event and the executed tool call carry the full content (only rendering elides), and the fact signature is unchanged.
* `make check` passes.
