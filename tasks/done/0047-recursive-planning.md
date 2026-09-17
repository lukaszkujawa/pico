# Recursive Planning

Live run 2026-09-17_16-41-51: the model considered delegating part of the review to a sub-agent — it is in the thinking at generation 28 — and never did, the same way it never called `note` and run 15-32-14 never called `complete_step`. Voluntary hygiene tools go unused by small models; meanwhile the one long-lived context accumulated sixty generations of reads and reasoning, rolled its own conclusions out of the window, and spent its second half re-deriving its first half. The vision already names the answer as a pillar — "Recursive delegation: complex work can be decomposed into isolated subagents with small, explicit contexts and typed results" — but as an optional tool it does not get used. This milestone promotes it to the execution model.

The shape: every run starts by simply working — a node that never sets a plan behaves exactly as today, acts, and answers; that is the trivial path, and no new decide-gate exists. Setting a plan *is* the act of decomposition: from that moment the runtime, not the model, executes each unfinished step as a child run in a fresh context, settles the step when the child answers, and gives the parent a generation between steps for judgement — revise the plan, dig into something, or answer. Each child faces the same implicit choice, so decomposition recurses exactly as far as the work demands. "Context is a cache" stops being an invariant the harness defends with compaction and becomes how execution works: a node's transcript is born, used, and discarded; what survives is its answer and its facts.

Everything rides on one thing: the state a child starts from. A fresh context that must reconstruct the world from 90-character index lines will flail; a fresh context handed the task, the plan's progress, and its predecessors' full results starts where its predecessor stopped. The handoff is therefore deterministic and explicit, and the write-back side is guaranteed, not voluntary: a child's answer is the distillation of its transcript, and a node that dies — budget spent or stuck — is forced to produce a partial answer before its context is thrown away. The eviction-triggered distiller (`tasks/shelved/distill-on-eviction.md`) stays shelved unless evals show conclusions still being lost inside a node.

## Design decisions

* **Planning is decomposition; the decision is implicit.** No new prompt machinery asks "trivial or plan?". A node without a plan runs today's loop unchanged. Once its plan has unfinished steps (and depth allows), the runner executes the first unfinished step as a child run before each of the node's own generations. Over-planning a trivial task costs a few short child runs; under-planning a hard one ends in a forced partial answer the parent can react to — both degrade gracefully.
* **The handoff is deterministic.** A step child's prompt is composed by the runtime: the root task, the parent's plan with each step's status, the full result text of every completed step, and the step being executed. Completed-step results are answers and therefore bounded; they ride in the prompt verbatim, so a child never has to rediscover where the repository is or what its predecessors concluded. The tree fact index arrives through the child's normal briefing and stays the recall layer for everything else.
* **Steps must stand alone, and the tool says so.** The `set_plan` description states that each step is executed by a fresh agent seeing only the step text, the root task, and the handoff — so every step must be self-contained: name paths, name targets, never point at "the files above". This is prompt guidance, not enforcement; the eval measures whether it holds.
* **The runtime settles steps.** A child's accepted answer marks its step done — `complete_step` is no longer the model's job for orchestrated steps (it remains for inline plans). The parent's between-step generation may accept the world as is, `set_plan` a revision (which can reopen or rewrite anything), make a direct tool call, or `answer`. A child that fails does not settle its step: the parent gets a generation to react, and if the plan is left unchanged the step is retried once; a second failure fails the node with a reason naming the step.
* **Step children reuse the delegate machinery.** A step child is a child session at depth + 1 with its own transcript, plan space, budget, and the same tool vocabulary. `MAX_STEP_STEPS = 30` sits between a delegate's 10 and the root's 100; constants live in `core/loop.py`. The `delegate` tool remains for model-initiated scoped questions.
* **The ledger is tree-scoped; transcripts and plans are node-scoped.** Every node of a run tree reads and writes one fact ledger: `facts()`, the index, `search_facts`, `read_fact`, and `answer` citations span the tree. Fact ids are allocated from the root session's sequence so they are unique and stable tree-wide — this touches the `tool_call_id`-as-fact-id equivalence in `core/context.py` and the recording path, and is the invasive part of the work. All sessions of a tree already share one store table under path-like session ids. `messages()` and `plan()` stay per-node: a child never sees a parent transcript, a parent never re-ingests a child's, and each node plans for itself.
* **A step's outcome is recorded twice: as the step result and as a fact.** The child's answer (or honest failure text) enters the parent transcript like a delegate result and is minted as a fact with source `"step"`, so any node in the tree can recall it in full.
* **No node dies silently.** A node about to fail — generation budget exhausted, or assessed stuck — gets one final generation with the vocabulary restricted to `answer` and a prompt saying so: answer now with what you have, citing facts. Only if that generation produces no valid answer does the node fail as today. This applies at every depth, root included; the wind-down nudge (0045) stays as the early warning ahead of the guarantee.
* **Depth is bounded, degrading to inline.** At `MAX_DELEGATE_DEPTH` a plan's steps execute inline in the node's own context — exactly today's behavior — rather than spawning children.
* **Out of scope, recorded here so it is not forgotten:** step children run on the delegate's quiet `Bus`, so the TUI shows nothing while a step runs; surfacing child progress is follow-up work.

## [X] T001 Tree-scoped fact ledger

### Description

Make the fact ledger span the run tree: `facts()` and everything derived from it — index, search, `read_fact`, citation checking — cover the root session and all descendants. Allocate fact-bearing sequence numbers from the root session so ids are unique and stable tree-wide; adjust the `tool_call_id` linkage in `core/context.py` and the recording path accordingly. Bookkeeping exclusions (0039) and index dedup (0046) apply unchanged.

### Acceptance criteria

* A fact minted in a child session appears in the parent's index and search, and vice versa; `read_fact` recovers it from any node.
* Fact ids never collide anywhere in a tree and are stable across reopening the store; handle demotion in the parent still names the right fact id for a child-minted fact.
* `answer` citations from any node may cite facts minted anywhere in the tree.
* A run that never spawns children keeps working end to end (ids may differ from today's per-session numbering).
* `make check` passes.

## [X] T002 Step orchestration with deterministic handoff

### Description

Add step orchestration to the loop: when the node's plan has unfinished steps and depth permits, run the first unfinished step as a child run whose prompt is the composed handoff (root task, plan with statuses, completed steps' full results, the step text). Record the outcome as a `"step"` fact and as a result in the parent transcript; an accepted answer settles the step by appending `PlanStepCompleted` to the parent session, so `plan()` stays purely event-derived; a failure leaves it open with one retry after a parent generation, then fails the node. Update the `set_plan` description to state that steps run as fresh agents and must be self-contained. At `MAX_DELEGATE_DEPTH`, skip orchestration entirely.

### Acceptance criteria

* A node that sets a two-step plan has each step executed in a child session with its own transcript; the second child's prompt contains the first child's full result and the plan with step one marked done.
* An accepted child answer settles its step without any `complete_step` call; the parent's next generation sees the updated plan and result.
* A parent generation that does nothing does not re-run a settled step; revising the plan changes which step runs next.
* A failed step is retried exactly once if the plan is unchanged; a second failure fails the node with a reason naming the step.
* After a child completes, the parent's next compiled context contains the handoff-visible state but no part of the child transcript.
* A child may itself set a plan and recurse; at `MAX_DELEGATE_DEPTH` a plan proceeds inline with no child spawned, and `complete_step` still works there.
* A node that never sets a plan behaves exactly as today, including the no-action stop and plan-stall nudge.
* The `set_plan` tool description names the fresh-agent execution model and the self-containment requirement.
* `make check` passes.

## [X] T003 Forced partial answer on node death

### Description

When any node is about to fail — generation budget exhausted, or the stuckness assessment says stuck — run one final generation with the tool vocabulary restricted to `answer` and a nudge naming the cause. A valid answer becomes the node's result (marked partial in the step result for children); otherwise the node fails exactly as today.

### Acceptance criteria

* A child that exhausts its budget mid-work returns a partial answer recorded as its step fact instead of a bare failure; the parent's next generation sees it marked partial.
* A node assessed stuck gets the same final generation before the stuck failure.
* The final generation offers only `answer`; other tool calls in it are invalid actions and end the node with the original failure.
* If the forced generation produces no valid answer, the node fails with the original error, unchanged.
* The root run gets the same guarantee; the wind-down nudge still fires ahead of it unchanged.
* `make check` passes.

## [X] T004 State-carrying decomposition eval

### Description

Add an eval task to `evals/tasks.py` that tests the handoff, not just chunking: a discovery made early is required to interpret everything later. Setup writes a manifest in one file set that names which entries in several other large file sets are real versus decoys; the sets together exceed `REFERENCE_CONTEXT_SIZE` several times over, so no single context can hold them, and the final answer requires the manifest's discovery plus evidence from every set. Deterministic setup and check as with existing tasks; running `make evals` remains a manual, human step.

### Acceptance criteria

* The combined file content exceeds the reference window several times; the check requires values from every set and is unsatisfiable without applying the manifest's rule.
* The eval registers alongside existing tasks and `make check` passes; evals are left to a manual run.
