# Answer Pane State Machine

Reported as: "I'm not even sure what happened to the answer here."

The screenshot shows the final answer rendered as a red `✗ answer` box containing the raw JSON of the tool call's arguments — the full 17-joke text with literal `\n` escapes — followed by `missing required field 'citations'`. The answer the user asked for was then never shown at all; the run continued and ended on an unrelated one-line assistant message.

Three defects compose into that.

**A pending `answer` renders as raw JSON.** `on_tool_call_pane_create` mounts a `ToolCallPane` for *every* call including `answer`, and `format_arguments` `json.dumps` the whole mapping. Because the answer's `content` is by far the largest argument in the system, the failure mode is maximally ugly exactly when it matters most. Reproduced directly:

```
⠹ answer
{"content": "There are 17 dirs", "verify": "echo \"17\""}
```

**A failed answer leaves that JSON box on screen permanently.** `AnswerPaneCreate` is only translated for `is_error=False` (`src/pico/tui/messages.py`), and only `on_answer_pane_create` removes the pending tool pane. A rejected answer — missing `citations`, an unknown citation, or a failed `verify` command — falls through to `ToolCallPaneClose` and the raw-JSON box simply finishes red and stays. That is precisely the screenshot.

**Nothing distinguishes a rejected answer from a fatal one.** A rejected answer is a normal, recoverable event: the model is expected to retry with citations. But it is presented identically to a hard tool error, with no indication the runtime asked for a correction, so the transcript reads as though the answer was lost.

## Design decisions

* **`answer` gets its own pane from the start.** `translate` maps `ToolCallStarted(name="answer")` to `AnswerPaneCreate`, not `ToolCallPaneCreate`. There is never a `ToolCallPane` for an answer, so there is nothing to remove later and no JSON blob can ever be shown. `on_answer_pane_create`'s `self._tool_call_panes.pop(...)` / `pending.remove()` dance is deleted.
* **The pane has three states: streaming, accepted, rejected.** It streams `content` while open (transport from `0029`), then on `ToolCallFinished` either settles as the accepted answer or renders as rejected with the runtime's reason. It is not removed on rejection — the transcript should show that an answer was attempted and why it was sent back.
* **A rejected answer is styled as a correction, not an error.** Distinct from `ErrorPane`'s heavy red border. The model retrying is the system working as designed; `VISION.md`'s verification principle makes rejection a routine outcome.
* **A later accepted answer does not erase the rejected one.** Each attempt is a separate pane, since each has its own pane id. The transcript stays an honest record.
* **Verification outcome is shown explicitly.** `_verified_answer` (`src/pico/core/loop.py`) currently folds the verify command into the result string as `f"{answer.content}\n\nverified: {answer.verify}"`, which the pane then displays as body text. Carry it as structured data on the event instead so the pane can render a verified badge rather than parsing prose.

## [X] T001 Structured answer outcome on the bus

### Description

Add an `AnswerSettled` event to `src/pico/core/events.py` carrying the pane id, the answer content, an accepted flag, the rejection reason (`None` when accepted), and the verify command when one ran. Publish it from `tool_call_step` for `answer` calls in place of the generic `ToolCallFinished`, and stop threading the verify command through the result string in `_verified_answer` — it returns the content and the outcome separately.

### Acceptance criteria

* `tests/core/test_loop.py` covers: an accepted answer with no `verify` publishes `AnswerSettled(accepted=True, reason=None, verify=None)`; an accepted answer with a passing `verify` carries the command and does not embed `"verified:"` in the content; a failing `verify` publishes `accepted=False` with the exit code and output in the reason; a missing-`citations` answer publishes `accepted=False` with the validation message; the recorded `ToolCallRecorded` result the *model* sees is unchanged in all cases (this is a display change, not a prompt change).
* Fully annotated, passes strict Pyright.

## [X] T002 Answer panes are created on start, never as tool panes

### Description

In `src/pico/tui/messages.py`, translate `ToolCallStarted(name="answer")` to `AnswerPaneCreate` and `AnswerSettled` to a new `AnswerPaneSettle` message; remove the `ToolCallFinished`+`name == "answer"` special case. In `src/pico/tui/app.py`, delete the pending-tool-pane removal from `on_answer_pane_create` and add `on_answer_pane_settle`.

### Acceptance criteria

* `tests/tui/test_app.py` covers: an `answer` call mounts exactly one `AnswerPane` and zero `ToolCallPane`s from the moment it starts; a rejected answer leaves the `AnswerPane` mounted showing the reason and still zero `ToolCallPane`s; no raw JSON (`{"content"`) appears in any rendered output for an answer call; a rejected answer followed by an accepted one leaves two `AnswerPane`s in order.
* The existing `test_failed_answer_call_still_renders_as_tool_call_pane` asserts the old, wrong behaviour and is replaced by a rejected-answer-pane test.
* Fully annotated, passes strict Pyright.

## [X] T003 The pane renders all three states

### Description

Give `AnswerPane` (`src/pico/tui/widgets.py`) streaming/accepted/rejected states: a streaming reactive for content, an accepted marker, a rejected style distinct from `ErrorPane`, a rendered reason, and a verified badge when a verify command ran.

### Acceptance criteria

* `tests/tui/test_widgets.py` covers: content deltas accumulate while streaming; an accepted answer renders its content with the success marker; an accepted answer with verification shows the verify command as a badge, not as body text; a rejected answer renders the reason and is visually distinct from both the accepted state and `ErrorPane`; long answer content wraps rather than being clipped (see `0031`).
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until green.

### Acceptance criteria

* `make check` passes, including the coverage floor.
* `grep -rn "_tool_call_panes.pop" src` returns nothing.

### Completion

Commit:
