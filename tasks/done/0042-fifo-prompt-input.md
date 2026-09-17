# FIFO Prompt Input

Pico can only be driven by typing into the TUI, so scripted testing and real-time benchmarking of the interactive app are impossible: nothing outside the terminal can submit a prompt. A `--sock PATH` switch exposes a named pipe (FIFO) so that `echo "Hello World" >> PATH` submits a message exactly as if the user had typed it — same transcript pane, same queueing, same run lifecycle — letting scripts feed prompts to a live session and measure behaviour as it happens.

## Design decisions

* **A FIFO, not a socket file.** `--sock PATH` creates a POSIX named pipe with `os.mkfifo`. Shell redirection (`echo ... >> PATH`) is the whole client protocol; no daemon, no framing, no dependency.
* **One line, one message.** Each newline-terminated line is submitted as one user message; lines that are empty after stripping are ignored. Multi-line prompts are out of scope.
* **Injected input is indistinguishable from typing.** Lines enter through the same path as the input box — a `UserInputSubmitted` posted to the running app (thread-safe via Textual's `post_message`) — so the user pane, queued-turn behaviour, stats, and session recording are identical to typed input. No parallel submission path into `input_queue`.
* **The reader lives beside the other threads in `app.py`.** `run_pico` starts a daemon thread that opens the FIFO, reads lines, and reopens on EOF (writers come and go); it honours the existing shutdown event. `tui` stays independent of the feature.
* **Lifecycle is explicit.** If `PATH` exists and is not a FIFO created for this run, startup fails with a clear error on stderr and exit code 1, like a `ConfigError`. The FIFO is unlinked on clean shutdown.
* **Flag plumbing matches `--prompt`.** `--sock` is parsed in `pico/__init__.py` and passed through `run_pico`; it composes with `--prompt`, `--resume`, and `--debug`.

## [X] T001 Read the FIFO and submit lines as user input

### Description

Add the `--sock` argument, the FIFO create/read/reopen/unlink lifecycle in `app.py`, and the thread-safe hand-off that posts each line to the app as `UserInputSubmitted`.

### Acceptance criteria

* With `--sock` given, a line written to the FIFO appears in the transcript as a user pane and starts a run, with the same bus events as a typed submission; a second line written mid-run queues exactly like typed input.
* Lines are delivered in write order; blank and whitespace-only lines produce no pane and no run.
* Writer EOF does not stop intake: after a writer closes the FIFO, a later `echo >> PATH` still delivers.
* Startup fails with a clear stderr message and exit code 1 when `PATH` exists and is not a FIFO; the FIFO is removed after a clean exit; without `--sock` nothing is created and behaviour is unchanged.
* Reader thread shutdown is covered: setting the existing shutdown event ends the thread without hanging the exit path.
* `make check` passes.
