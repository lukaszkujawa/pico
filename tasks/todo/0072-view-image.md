# View Image

Many of the models Pico targets are multimodal — Qwen VL variants run happily under llama.cpp and Ollama — but Pico is text-blind: an agent that takes a screenshot through `shell` has no way to look at it, so a whole class of verification (did the page render, what does the chart show, what error is in the dialog) is closed off. The capability fits the vision document exactly as it stands: an image is the ultimate bulky payload, so the file on disk is the handle, the pixels enter the prompt only while the model is looking, and what the model learned is recorded as text facts in the ledger. Not every model can see, and no OpenAI-compatible `/models` listing reliably reports modality, so the capability is an explicit opt-in. One tool does all of it: `view_image(path)`. There is no screenshot tool — capture already belongs to `shell` (`screencapture`, headless browsers); Pico only needs the looking half.

## Design decisions

* **`LLM_VISION` opts in, and off means absent.** `Config` gains `vision: bool`, parsed from `LLM_VISION` ("1"/"true", default off) in the `LLM_TEMPERATURE` style. The flag gates registration in `register_actions` — reaching `app.py`, `headless.py`, and the child registries in `subruns.py` — so a text-only model never sees the tool in its vocabulary. With the flag off, behaviour is byte-identical to today.
* **`view_image` validates and returns a handle, never pixels.** The tool sniffs magic bytes (png, jpeg, gif, webp), rejects anything else with an instructive error, and rejects files over `VIEW_IMAGE_CAP_BYTES = 4_000_000` with the instruction to downscale via shell first. Success returns one short line — path, format, byte size. The result content is the handle; the image itself never enters the transcript as text. The description tells the model to record what it observes with `note`: the image is cache, the ledger is memory.
* **Images ride on the tool-result message, as paths.** `Message` gains `images: tuple[str, ...] = ()`. `session.messages()` attaches the path to the rendered `Role.TOOL` message for a `view_image` event; the stored event is unchanged. Paths, not bytes: encoding to base64 happens at send time in the vendor clients via one shared helper (path → media type + base64, `None` if unreadable). A file missing at send time degrades to a text stub in the payload, never an error — a replayed session must not brick because a screenshot was cleaned up.
* **Each vendor puts the image where its wire format wants it.** Anthropic: an image source block inside the `tool_result` content. Ollama: the `images` field on the message. OpenAI: tool messages cannot carry image parts on most compatible servers, so the client emits the tool message followed by a user message holding the data-URL image part. The `Message` type stays honest about where the image belongs; only serialization differs.
* **Only the latest image is live.** At replay, the image attaches only to the most recent `view_image` event; older ones render their result as a fixed line naming the path and saying to `view_image` again if needed. This knowingly bends 0070's shape-at-creation rule: when a new image arrives, one earlier message changes and the prefix cache re-primes from there. The alternative — every viewed image riding in every prompt forever at four figures of tokens each — is strictly worse, and re-viewing is one cheap call.
* **Budget counts images at a flat `IMAGE_TOKEN_ESTIMATE = 1500`.** `message_tokens` adds the constant per attached image so the recency window budgets honestly. `sent_chars` and `reconcile` are untouched: with at most one live image the calibration skew is bounded by the existing `MIN_CHARS_PER_TOKEN` clamp, and per-model image tokenisation is not worth modelling until evidence demands it.
* **Scope guard.** No screenshot tool, no image generation, no OCR, no autodetection of model modality, no image content in facts or the scratch database, no retroactive changes to sessions recorded before the flag existed.

## [ ] T001 Vision flag and view_image tool

### Description

Add `vision` to `Config` from `LLM_VISION`; add `view_image` in the house closure shape with magic-byte and size validation; register it behind the flag at every depth, threading the flag through `register_actions` to `app.py`, `headless.py`, and `subruns.py`.

### Acceptance criteria

* `LLM_VISION` unset or falsy leaves the tool unregistered everywhere, including child registries, and every existing test passes unchanged.
* A valid image returns the one-line handle result; a non-image file, a missing path, and an oversized file each produce a distinct, instructive error naming the path.
* Format detection uses magic bytes, not the file extension.
* `make check` passes.

## [ ] T002 Latest-image replay and budgeting

### Description

Add `images` to `Message`; in `session.messages()` attach the path to the rendered tool-result message of the most recent `view_image` event and render older ones as the fixed re-view line; count attached images in `message_tokens` at `IMAGE_TOKEN_ESTIMATE`.

### Acceptance criteria

* With two `view_image` events in a session, only the later rendered message carries `images`; the earlier renders the re-view line naming its path.
* A session with no `view_image` events renders byte-identically to today.
* `message_tokens` on an image-bearing message exceeds its text-only twin by exactly `IMAGE_TOKEN_ESTIMATE`; `sent_chars` is unchanged.
* The stored `ToolCallRecorded` event carries no image bytes.
* `make check` passes.

## [ ] T003 Vendor wire formats

### Description

Add the shared path-to-base64 helper and serialize image-bearing messages in all three clients: Anthropic as an image block inside `tool_result`, Ollama via the `images` field, OpenAI as a trailing user message with a data-URL image part.

### Acceptance criteria

* Each client's payload for an image-bearing tool-result message matches its wire format, covered by tests asserting payload shape against a small real image fixture.
* An unreadable image path yields a text stub in the payload and no exception, in all three clients.
* Messages without images serialize byte-identically to today in all three clients.
* `make check` passes.
