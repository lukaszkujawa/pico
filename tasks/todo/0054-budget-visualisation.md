# Budget Visualisation

The context meter shows prompt tokens against `context_size` and nothing else. Three budgets govern a run and only one is visible: context fill, the generation budget (`max_steps`), and the decision crossroads (`RECENT_UNITS` exchanges). A run can be one generation from `dying_of` with no sign of it in the UI.

## Design decisions

* **Reuse the ctx bar; do not add a second bar.** The meter keeps its geometry (`METER_WIDTH`, filled/empty glyphs) and gains colour bands that encode which budget is closest to exhausted.
* **Three thresholds**, mapped onto the existing `used/context_size` fill:
  * normal — `theme.meter`
  * wind-down — the generation budget has passed `BUDGET_WIND_DOWN_FRACTION`, or the transcript has passed `RECENT_UNITS` exchanges; a distinct warning step in the greyscale-plus-accent palette
  * over — context ratio at or above `OVER_BUDGET_RATIO`, or the run is dying; `theme.error`
  The most severe active band wins.
* **Generation budget as text**, not a second bar: the request counter becomes `N/M req` when `max_steps` is known, plain `N req` when it is not. This costs no horizontal space and reuses a widget that already counts generations.
* **Wiring**: `PicoApp` receives `max_steps` alongside `context_size`. Crossroads and dying state reach the TUI as fields on an existing event rather than a new one — extend `GenerationCompleted` with the current iteration count and a pressure flag, defaulting to `None`/`False` so headless and eval paths are unaffected.
* Colours come from `tui/theme.py` as named steps; no hues beyond the existing accent and error.

## [ ] T001 Surface budget state to the TUI

### Description

Pass `max_steps` into `PicoApp` from `app.py`. Extend `GenerationCompleted` with the iteration count and a flag for active decision or budget pressure, both optional. Publish the real values from the loop.

### Acceptance criteria

* `PicoApp` receives `max_steps` and tolerates `None`.
* `GenerationCompleted` carries iteration count and pressure flag with defaults that leave existing constructions valid.
* Headless and eval runs are unaffected.
* `make check` passes.

## [ ] T002 Colour-banded meter and generation counter

### Description

Give `ContextMeter` the three-band colour rule driven by context ratio, budget wind-down, and crossroads pressure. Change `RequestCounter` to render `N/M req` when a generation budget is known.

### Acceptance criteria

* The bar renders the normal colour below all thresholds, the warning colour when wind-down or crossroads pressure is active, and the error colour when context is at or over `OVER_BUDGET_RATIO` or the run is dying; the most severe band wins.
* The request counter shows `N/M req` with a known budget and `N req` without.
* Bar width, glyphs, and the `used/total` numbers are unchanged; reset clears the band back to normal.
* `make check` passes.
