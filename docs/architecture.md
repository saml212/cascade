# Architecture

## Agent System (`agents/`)

- **`base.py`** — `BaseAgent` ABC. Agents implement `execute() -> dict`. The `run()` wrapper handles timing, logging, writing `<agent_name>.json`, and `progress.json` for polling.
  - Helpers: `load_json()`, `load_json_safe()` (returns `{}` on missing/invalid), `save_json()`, `get_config(*keys, default=)`, `report_progress()`.
- **`pipeline.py`** — DAG-based parallel orchestrator using `ThreadPoolExecutor(max_workers=3)`. `AGENT_DEPS` defines the dependency graph (e.g., `longform_render` depends on both `speaker_cut` and `transcribe`).
- **`__init__.py`** — `AGENT_REGISTRY` (name → class), `PIPELINE_ORDER`, and names retired from new execution while their history remains readable.
- **`__main__.py`** — CLI entry point for `python -m agents`.

## Pipeline Order

`ingest` → `stitch` → `audio_analysis` → `speaker_cut` → `transcribe` → `clip_miner` → `longform_render` → `shorts_render` → `thumbnail_gen` → `qa` → explicit publication agents → `backup`

Release copy is drafted outside the runtime pipeline, reviewed, and written through the canonical episode and clip PATCH routes before final QA. Legacy `metadata/metadata.json` files remain readable.

## Pipeline Behaviors

- After `stitch`, the pipeline **pauses** with `status: "awaiting_crop_setup"` if crop config isn't set — the user configures speaker crop points via the API before resuming.
- After `clip_miner`, the episode directory is **renamed** to include the guest name slug.
- Video RSS, Upload-Post publishing, and backup remain explicit stages. Audio-only RSS generation is retired; existing feed receipts and MP3 files remain read-only history.
- `NON_CRITICAL_AGENTS` contains the video/short publication, backup, and thumbnail stages, so their failures do not abort other completed work.
- `episode.json` is the master state file, updated continuously.

## API and artifact contract

- FastAPI is the production boundary for people, automation, and coding agents.
  The TypeScript UI is one client of the same JSON, preview, review, and download
  routes; an agent must not need browser DOM access to inspect media state or
  operate the workflow.
- Deterministic code owns timestamps, media transforms, validation, retries, and
  duplicate protection. Agent prompts and skills own editorial judgment and
  operating guidance.
- Every release artifact must be traceable to the exact source, edit, audio,
  crop, caption, and metadata revisions that produced it. Approval applies to
  that revision and becomes stale when an input changes.
- `episode.json` plus per-stage JSON files are the current transitional state
  model. The production target is one artifact manifest exposed through the API
  for the source, audio master, full episode, shorts, metadata, QA, review, and
  distribution receipts.
- In-process background threads currently run some media work. Autonomous
  operation requires a durable queue that survives restarts, records
  checkpoints before side effects, and resumes safely when media is mounted.

## Audio Mix System

- Supports external multi-track audio from Zoom H6E (4 XLR + stereo mix + built-in mic).
- Audio sync via GCC-PHAT anchors between the complete camera timeline and
  concatenated H6E recorder sessions.
- Per-track volume control via `POST /{episode_id}/audio-mix`.
- Pre-mixed audio stored as `work/audio_mix.wav`, used by both render agents.
- Speaker cut agent supports N-speaker mode using dedicated mic tracks for detection.
