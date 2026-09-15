# Context Budget

Roadmap milestone 5 of 7 (see `0008-session-store.md` for the full arc). Not yet broken into tasks.

This is the milestone that most directly serves "achieve very complex tasks with minimal LLM resources": port the old architecture's active context compression (`context.py`) — render levels (HANDLE/DIGEST/FULL, or a smaller Pico-appropriate subset) for large tool outputs and file contents, a token budget partitioned across fixed slices (system/tools/history/working-memory), and an eviction/promotion algorithm that decides what gets shown in full versus summarized versus just referenced by a handle.

Depends on `0008`'s session log existing (candidates for eviction are read from recorded events/artifacts) and benefits from `0010`'s actions being in place (an `inspect`-equivalent action that promotes an artifact's render level, matching the old architecture's promotion-on-reinspect behavior). `config.context_size` already exists in `pico.config` — this milestone is what finally makes that number do something beyond being read and ignored.

Keep the render-level count and budget-slicing formula as small as actually needed for Pico's scale before matching the old architecture's exact fractions/clamps — those were tuned for a much larger, longer-running system.
