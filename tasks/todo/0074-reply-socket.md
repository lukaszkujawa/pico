# Reply Socket

`--sock NAME` lets another process drive Pico by writing a prompt line to `./sock/NAME`, but there is no way back: the answer can only be scraped from debug logs, which are `repr(event)` lines with no turn boundaries. This blocks agent-to-agent use, where the caller needs the reply as a value. This milestone adds the mirror channel — a reply FIFO that emits exactly one record per turn — so one Pico can drive another with two shell lines:

```
echo "What is 17 * 23?" > ./sock/0
reply=$(cat ./sock/0.out)
```

## Design decisions

* **A reply FIFO mirrors the input FIFO.** When `--sock NAME` is set, alongside the input FIFO at `./sock/NAME` a reply FIFO is created at `./sock/NAME.out`, created and unlinked in the same places under the same `sock is not None` guard in `run_pico`. No new flag, no port, no server — the same idiom `read_fifo` already uses.
* **A bus subscriber writes replies, symmetric to `read_fifo`.** A `write_fifo(path, bus, shutdown)` thread subscribes to the bus and writes one line per completed turn. It is the twin of the existing reader thread and lives beside it in `app.py`.
* **Exactly one record per turn, always — even when the agent stops without answering.** The turn-end signal is `RunFinished` (published on every normal, budget, max-steps, and error exit) and `RunCancelled` (published just before an early cancel return). The writer keys on those, not on `AnswerSettled`, so a turn that ends without an answer still produces output. It tracks the most recent accepted `AnswerSettled` of the current turn (resetting on `RunStarted`); at turn end it emits that answer if one settled, otherwise a `stopped` record carrying the reason.
* **One JSON object per line.** Each record is `{"status": ..., "content": ..., "reason": ...}` — content JSON-escaped so it is always a single line regardless of newlines in the answer. `status` is `"answered"` (accepted answer; `content` set, `reason` null) or `"stopped"` (turn ended without an accepted answer; `content` null, `reason` the failure/cancel cause or a fixed "no answer" note). Callers read with `cat`, or `jq -r .content` for the prose.
* **Only accepted, complete answers count as answered.** `AnswerSettled` fires for rejected and intermediate answers too; the writer records an answer only for `accepted and complete`, so the emitted line is the turn's final answer, never a rejection mid-verification.
* **Scope guard.** No change to the input path, the loop, the event types, or the TUI. No request/response correlation ids, no multiplexing, no non-FIFO transport. The reply FIFO exists only when `--sock` is set.

## [ ] T001 Reply FIFO writer

### Description

Add `write_fifo(path, bus, shutdown)` to `app.py`, subscribing to the bus and writing one JSON line per turn as decided above. Create and unlink `./sock/NAME.out` beside the input FIFO in `run_pico`, and start the writer thread beside the reader thread, both under the existing `sock is not None` guard.

### Acceptance criteria

* Writing a prompt to `./sock/NAME` and reading `./sock/NAME.out` yields exactly one JSON line for that turn.
* A turn ending with an accepted answer yields `{"status":"answered","content":<answer>,"reason":null}`; the content round-trips through JSON regardless of embedded newlines.
* A turn ending without an accepted answer — budget exceeded, max steps, error, or cancel — yields `{"status":"stopped","content":null,"reason":<cause>}`; a reader is never left blocking on a turn that produced no answer.
* A rejected or intermediate `AnswerSettled` never produces a record on its own; only the turn-end signal emits.
* When `--sock` is not set, no reply FIFO is created and behaviour is unchanged.
* `make check` passes.

## [ ] T002 Document the reply channel

### Description

Extend the `--sock` section of `README.md` to describe `./sock/NAME.out`, its one-line-per-turn JSON contract, and the two-line request/response example.

### Acceptance criteria

* The README shows writing a prompt and reading the reply, and documents the `answered`/`stopped` statuses.
* No other documentation is changed.
* `make check` passes.
