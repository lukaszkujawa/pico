# Stats Strip Ordering and Spacing

The strip reads `ctx · req · tokens · spinner · elapsed`, so the spinner sits far right, detached from the text it belongs to. It should trail the most recent output, with tokens and elapsed after it. The strip also hugs the terminal's bottom edge with no breathing room.

## Design decisions

* **Order** becomes: context meter, request count, spinner, completion tokens, elapsed. The spinner moves from fourth to third so it reads as attached to live output rather than stranded at the end.
* **Spacing**: one blank row below the strip. Achieved with `#stats-strip` bottom padding in `app.tcss`, not a spacer widget — the strip stays a single logical row.
* Separator glyphs stay as they are; only the compose order in `StatsStrip.compose` changes.
* No new widgets, no bus protocol change.

## [ ] T001 Reorder the strip and add bottom spacing

### Description

Reorder `StatsStrip.compose` in `src/pico/tui/widgets.py` to ctx, req, spinner, tokens, elapsed, keeping separators consistent. Add bottom padding to `#stats-strip` in `src/pico/tui/app.tcss` so a blank row separates the strip from the screen edge.

### Acceptance criteria

* The strip renders in the order: context meter, request count, spinner, tokens, elapsed.
* One blank row sits below the strip; the footer still docks bottom and the input row is unmoved.
* Accessor properties (`indicator`, `timer`, `counter`, `meter`, `requests`) and start/stop/reset behaviour are unchanged.
* `make check` passes.
