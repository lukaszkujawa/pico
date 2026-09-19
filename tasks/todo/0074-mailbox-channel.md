# Mailbox Channel

`--sock NAME` lets another process push a prompt into a running Pico (write a line to `./sock/NAME`), but there is no way back: the answer can only be scraped from debug logs. And the transport is a FIFO on a container tmpfs relayed by a `docker exec` forwarder, which cannot be bind-mounted or shared — so containers can neither return answers nor talk to each other.

This milestone turns the socket into a **mailbox**: an addressable inbox/outbox pair backed by plain append-log files in one shared directory. A caller writes a prompt to an agent's inbox and reads its reply from the matching outbox; because plain files bind-mount across the Docker boundary (FIFOs do not), one directory shared into every container makes a full agent-to-agent mesh with no forwarder:

```
echo "What is 17 * 23?" >> ./sock/worker      # prompt agent "worker"
tail -f ./sock/worker.out                      # read its replies, one JSON line per turn
```

The flag is renamed `--sock` → `--mailbox` because it now names *this agent's* mailbox (its inbox `NAME` and outbox `NAME.out`), not a one-way pipe.

## Design decisions

* **Transport is a plain append-log file, not a FIFO.** The inbox is a regular file; the reader tail-follows it — tracks a byte offset, reads newly-appended complete lines, polls for growth (reuse the existing `select`-based cadence). `echo "x" >> ./sock/NAME` keeps working; the reader consumes by advancing its offset, so nothing is lost and a backlog survives a restart. This removes `os.mkfifo`, and with it the entire `docker exec` forwarder in `bin/run_in_docker.sh`.
* **On startup the reader seeks to end.** A fresh turn loop ignores lines already in the inbox (they belong to a prior process); it processes only lines appended after it starts. This keeps behaviour intuitive and avoids replaying a stale backlog.
* **The outbox gets exactly one JSON line per turn — always, even when the agent stops without answering.** A bus subscriber writes it, keyed on turn-end signals (`RunFinished` on every normal/budget/max-steps/error exit; `RunCancelled` on early cancel), not on `AnswerSettled`. It tracks the latest accepted, complete `AnswerSettled` of the current turn (reset on `RunStarted`); at turn end it appends that answer if one settled, otherwise a `stopped` record. A reader is never left blocking on a turn that produced no answer.
* **Outbox line format.** `{"status": ..., "content": ..., "reason": ...}`, content JSON-escaped so it is always one line. `status` is `"answered"` (accepted+complete answer; `content` set, `reason` null) or `"stopped"` (turn ended with no accepted answer; `content` null, `reason` the failure/cancel cause). `jq -r .content ./sock/NAME.out` reads prose.
* **`--mailbox NAME` names this agent's own inbox/outbox in the shared dir.** `NAME` matches `[A-Za-z0-9._-]+` (unchanged validation). The old `--sock` spelling is removed, not aliased — there are no external users to preserve. Local (`make run`) and Docker paths now use identical code.
* **Docker shares one host directory read-write into every container.** `bin/run_in_docker.sh` bind-mounts `./sock` (host) at the container mailbox dir and points `--mailbox` at a file inside it. Every container mounting the same host `./sock` sees every mailbox, so agents address each other by name. The tmpfs, the `--name`/`docker exec` forwarder, and the `forward_sock`/`cleanup`-of-forwarder machinery are deleted.
* **Scope guard.** No request/response correlation ids, no multiplexing beyond one line per turn, no change to the loop, event types, or TUI. No network transport. Files grow unbounded within a run; truncation/rotation is out of scope (a run starts by seeking to end, so growth is harmless).

## [ ] T001 Append-log inbox reader

### Description

Replace the FIFO input path in `app.py` with a tail-following reader over a plain file: drop `_create_fifo`/`os.mkfifo`, read the inbox as a regular file seeking to end on start, and submit each newly-appended non-empty line via the existing `submit` callback.

### Acceptance criteria

* Appending a line to the inbox file (`echo "x" >> file`) submits it as a prompt; multiple appends submit in order.
* Lines present before the reader starts are not submitted; only lines appended afterward are.
* No FIFO is created anywhere; the inbox is an ordinary file.
* `make check` passes.

## [ ] T002 Outbox writer

### Description

Add a bus subscriber in `app.py` that appends one JSON line per turn to the outbox file (`NAME.out`), keyed on turn-end signals as decided above, tracking the latest accepted+complete `AnswerSettled` since the last `RunStarted`.

### Acceptance criteria

* A turn ending with an accepted answer appends `{"status":"answered","content":<answer>,"reason":null}`; content round-trips through JSON regardless of embedded newlines.
* A turn ending without an accepted answer (budget, max steps, error, cancel) appends `{"status":"stopped","content":null,"reason":<cause>}`.
* Exactly one line is appended per turn; a rejected or intermediate `AnswerSettled` never emits on its own.
* `make check` passes.

## [ ] T003 Rename `--sock` to `--mailbox`

### Description

Rename the CLI argument in `pico/__init__.py` and the `sock` parameter thread through `run_pico` (and `_turn_loop` wiring) to `mailbox`. Wire the inbox reader (T001) and outbox writer (T002) under the single `mailbox is not None` guard, creating and removing the outbox file beside the inbox.

### Acceptance criteria

* `pico --mailbox NAME` enables both channels; `--sock` is no longer accepted.
* With no `--mailbox`, no mailbox files are touched and behaviour is unchanged.
* The inbox and outbox files are created under the mailbox path and cleaned up on exit.
* `make check` passes.

## [ ] T004 Docker shares one mailbox directory

### Description

Rewrite the mailbox handling in `bin/run_in_docker.sh` to bind-mount the host `./sock` directory read-write into the container and pass `--mailbox` pointing inside it. Delete the tmpfs, the container `--name`, the `forward_sock` forwarder, and its `docker exec` relay and cleanup. Rename the `--sock` script flag to `--mailbox` and update the usage/comment block and the `Makefile` examples.

### Acceptance criteria

* `make run_in_docker ARGS="--mailbox worker"` bind-mounts `./sock` into the container so `echo "task" >> ./sock/worker` reaches the agent and `./sock/worker.out` receives its replies on the host.
* Two containers started with `--mailbox a` and `--mailbox b` sharing the same host `./sock` can address each other's inboxes and read each other's outboxes.
* No FIFO is created on the host and no `docker exec` forwarder process is started.
* The `run_in_docker.sh` usage text and the `Makefile` `--sock` examples are updated to `--mailbox` with the `>>` append form.
* `make check` passes.

## [ ] T005 Document the mailbox

### Description

Update the `--sock` section of `README.md` to describe the mailbox: `--mailbox NAME`, the inbox/outbox files, the one-line-per-turn JSON contract with `answered`/`stopped` statuses, the `>>` append and `tail -f` read forms, and the shared-directory mesh across containers.

### Acceptance criteria

* The README documents inbox and outbox, the JSON line format, and a two-agent mesh example over a shared `./sock`.
* All `--sock` references in the README are replaced with `--mailbox`.
* `make check` passes.
