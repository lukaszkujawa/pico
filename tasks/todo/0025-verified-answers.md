# Verified Answers

`VISION.md`: "Results should be checked by deterministic software wherever possible rather than trusted because a model produced them." The runtime currently verifies one thing — that `answer` citations name facts that exist (`src/pico/core/loop.py`). Whether the answer's *claim* holds — the tests pass, the file was produced, the command now succeeds — is taken entirely on the model's word, and small models are at their weakest exactly here: declaring victory one step early. The repository's own development rule ("a change is not complete because an agent believes it works") is not yet a rule the runtime imposes on its agent.

This milestone lets an answer carry its own mechanical check: an optional shell command the runtime executes before accepting the answer. Exit zero, the answer stands; anything else, the answer is rejected with the command's output and the loop continues — the model must fix the work or fix the claim. The model chooses *whether* and *what* to verify (its judgment); the runtime does the checking and the gating (its job). Together with the `0021` suite this closes the loop end to end: evals verify Pico's answers from outside, verification lets Pico refuse its own unverified answers from inside — and the suite's task 7 family measures whether this actually raises pass rates on build-and-check tasks.

## Design decisions

* **One optional field.** `Answer` (`src/pico/core/actions.py`) gains `verify: str | None` — a shell command; absent or empty means unverified, accepted as today. The `answer` `ToolSpec` documents it plainly: provide `verify` whenever the task has a checkable outcome; the command must exit 0 exactly when the answer's claim is true.
* **Reuse `Shell`, same rules.** Verification runs through the existing `Shell` action machinery (same timeout, same `ToolError` on timeout per `0020`), in the same working directory the run's tools use. No new execution path to secure or test separately.
* **Rejection is an ordinary error result.** In `tool_call_step`'s `answer` branch, after citation validation passes and `verify` is present: run the command. Non-zero exit → the tool result becomes `answer rejected — verification failed (exit N):\n<output>` with `is_error=True`, `final_answer` stays unset, the run continues. This deliberately rides the existing pressure systems: repeated failing verification feeds `tool_failure_streak`, so a model that cannot make its check pass gets nudged and eventually hard-stopped by `0014`'s thresholds instead of looping forever. Like `ToolError` (`0020`), a failed verification does not touch `invalid_action_attempts` — it is not a protocol mistake.
* **Success is silent, but recorded.** On exit 0, accept the answer exactly as today; the recorded tool result may note `verified` plus the command for the log's benefit — provenance for free.
* **Delegates cannot verify.** Delegates are read-only by design (`register_delegate_actions` grants no shell); an `answer` carrying `verify` inside a delegate is rejected with `InvalidActionError` — running arbitrary commands through the answer channel would quietly break the read-only contract.

## [ ] T001 `Answer.verify` parsing

### Description

Add the optional field to `Answer.from_arguments` (absent → `None`; present → must be a non-empty string, else `InvalidActionError`) and extend the `answer` `ToolSpec` parameters and description per the design decisions.

### Acceptance criteria

* `tests/core/test_actions.py` covers: absent field parses to `None`; a valid command string round-trips; a non-string or empty-string `verify` raises `InvalidActionError`.
* Fully annotated, passes strict Pyright.

## [ ] T002 Verification gating in the loop

### Description

Implement the gate in `tool_call_step` per the design decisions: execution via `Shell`, rejection shape, streak/counter behavior, silent-but-recorded success, and the delegate rejection.

### Acceptance criteria

* `tests/core/test_loop.py` covers, with scripted clients and real (cheap) shell commands like `true`/`false`/`test -f`: a passing `verify` ends the run with `final_answer` set; a failing one records an `is_error` result containing the exit code and output, leaves `final_answer` unset, and the run continues to another model turn; an answer with no `verify` behaves byte-for-byte as before; repeated failing verifications trip the stuckness hard stop rather than `MAX_INVALID_ACTION_ATTEMPTS`; a delegate answering with `verify` set is rejected and the delegate result reports it.
* Fully annotated, passes strict Pyright.

## [ ] T003 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Ensure the `0021` suite's build-until-tests-pass task genuinely rewards verification (its prompt states the check command exists, e.g. the seeded test script — the model should discover it can use that as `verify`). If a live model is configured, run `make evals` and record in the commit message whether verified-answer tasks moved.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
