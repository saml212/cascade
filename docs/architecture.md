# Architecture

## Agent System (`agents/`)

- **`base.py`** — `BaseAgent` ABC. Agents implement `execute() -> dict`. The `run()` wrapper handles timing, logging, writing `<agent_name>.json`, and `progress.json` for polling.
  - Helpers: `load_json()`, `load_json_safe()` (returns `{}` on missing/invalid), `save_json()`, `get_config(*keys, default=)`, `report_progress()`.
- **`pipeline.py`** — DAG-based parallel orchestrator using `ThreadPoolExecutor(max_workers=3)`. `AGENT_DEPS` defines the dependency graph (e.g., `longform_render` depends on both `speaker_cut` and `transcribe`).
- **`__init__.py`** — `AGENT_REGISTRY` (name → class) and `PIPELINE_ORDER` (ordered list of 14 names).
- **`__main__.py`** — CLI entry point for `python -m agents`.

## Pipeline Order

1. `ingest` → 2. `stitch` → 3. `audio_analysis` → 4. `speaker_cut` → 5. `transcribe` → 6. `clip_miner` → 7. `longform_render` → 8. `shorts_render` → 9. `metadata_gen` → 10. `thumbnail_gen` → 11. `qa` → 12. `podcast_feed` → 13. `publish` → 14. `backup`

## Pipeline Behaviors

- After `stitch`, the pipeline **pauses** with `status: "awaiting_crop_setup"` if crop config isn't set — the user configures speaker crop points via the API before resuming.
- After `clip_miner`, the episode directory is **renamed** to include the guest name slug.
- `NON_CRITICAL_AGENTS = {"podcast_feed", "publish", "backup"}` — failures here don't abort the pipeline.
- `episode.json` is the master state file, updated continuously.

## Audio Mix System

- Supports external multi-track audio from Zoom H6E (4 XLR + stereo mix + built-in mic).
- Audio sync via FFT cross-correlation between camera scratch audio and H6E stereo mix.
- Per-track volume control via `POST /{episode_id}/audio-mix`.
- Pre-mixed audio stored as `work/audio_mix.wav`, used by both render agents.
- Speaker cut agent supports N-speaker mode using dedicated mic tracks for detection.
