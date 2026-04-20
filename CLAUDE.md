# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
Cascade is a 14-agent pipeline that processes podcast recordings to publish-ready shorts + longform video. It ingests media from cameras and external audio recorders (DJI + Zoom H6E), stitches clips, analyzes/syncs audio, segments speakers (supports N-speaker multi-track), transcribes via Deepgram, mines clips via Claude, renders longform (16:9) and shorts (9:16 with subtitles), generates platform metadata, generates caricature thumbnails via OpenAI, runs QA validation, generates a podcast RSS feed, publishes to platforms, and backs up to external storage.

## Commands

### Setup & Server
```bash
./start.sh                    # Creates .venv (Python 3.12 via uv), installs deps, starts uvicorn on :8420
```

### Pipeline (CLI)
```bash
.venv/bin/python -m agents --source-path "/path/to/media/"
.venv/bin/python -m agents --source-path "/path/to/media/" --episode-id ep_2026-02-13_test
.venv/bin/python -m agents --source-path "/path/to/media/" --agents ingest stitch audio_analysis
```

### Tests
```bash
.venv/bin/pytest              # All Python tests
.venv/bin/pytest -v           # Verbose
.venv/bin/pytest tests/test_agent_ingest.py  # Single test file
cd frontend && npm test       # Frontend Jest tests (jsdom)
```

### API
```bash
curl -X POST http://localhost:8420/api/episodes/ep_001/run-pipeline \
  -H "Content-Type: application/json" \
  -d '{"source_path": "/path/to/media/"}'
curl http://localhost:8420/api/episodes/ep_001/pipeline-status
curl -X POST http://localhost:8420/api/episodes/ep_001/auto-approve
```

## Architecture

### Agent System (`agents/`)
- **`base.py`** — `BaseAgent` ABC: agents implement `execute() -> dict`. The `run()` wrapper handles timing, logging, writing `<agent_name>.json`, and `progress.json` for polling. Helpers: `load_json()`, `load_json_safe()` (returns `{}` on missing/invalid), `save_json()`, `get_config(*keys, default=)` for nested config access, `report_progress()`.
- **`pipeline.py`** — DAG-based parallel orchestrator using `ThreadPoolExecutor(max_workers=3)`. `AGENT_DEPS` defines the dependency graph (e.g., `longform_render` depends on both `speaker_cut` and `transcribe`). Agents run concurrently when deps are satisfied.
- **`__init__.py`** — `AGENT_REGISTRY` (name → class) and `PIPELINE_ORDER` (ordered list of 14 names).
- **`__main__.py`** — CLI entry point for `python -m agents`.

### Pipeline Behaviors
- After `stitch`, the pipeline **pauses** with `status: "awaiting_crop_setup"` if crop config isn't set — the user must configure speaker crop points via the API before resuming.
- After `clip_miner`, the episode directory is **renamed** to include the guest name slug.
- `NON_CRITICAL_AGENTS = {"podcast_feed", "publish", "backup"}` — failures here don't abort the pipeline.
- `episode.json` is the master state file, updated continuously.

### Audio Mix System
- Supports external multi-track audio from Zoom H6E (4 XLR tracks + stereo mix + built-in mic)
- Audio sync via FFT cross-correlation between camera scratch audio and H6E stereo mix
- Per-track volume control via `POST /{episode_id}/audio-mix` endpoint
- Pre-mixed audio stored as `work/audio_mix.wav`, used by both render agents
- Speaker cut agent supports N-speaker mode using dedicated mic tracks for detection

### Pipeline Order
1. `ingest` → 2. `stitch` → 3. `audio_analysis` → 4. `speaker_cut` → 5. `transcribe` → 6. `clip_miner` → 7. `longform_render` → 8. `shorts_render` → 9. `metadata_gen` → 10. `thumbnail_gen` → 11. `qa` → 12. `podcast_feed` → 13. `publish` → 14. `backup`

### Shared Libraries (`lib/`)
| Module | Purpose |
|--------|---------|
| `paths.py` | `resolve_path()` — checks if external volume is mounted, falls back to local; `get_episodes_dir()` checks `CASCADE_OUTPUT_DIR` env var |
| `ffprobe.py` | `probe()`, `get_duration()`, `get_dimensions()` — wrappers over `ffprobe -print_format json`. All ffprobe calls go through this module. |
| `clips.py` | `normalize_clip()` — ensures both `start`/`end` and `start_seconds`/`end_seconds` exist |
| `srt.py` | `fmt_timecode()`, `escape_srt_path()`, `generate_srt_from_diarized()`, `parse_srt()`, `parse_srt_time()` — shared SRT generation, parsing, and ffmpeg escaping |
| `encoding.py` | `has_videotoolbox()` — detects macOS GPU encoder; `get_video_encoder_args()` — returns VideoToolbox or libx264 args; `get_lut_filter()` — returns ffmpeg lut3d filter string from config |
| `audio_mix.py` | `generate_audio_mix()` — generates pre-mixed stereo WAV from multi-track H6E recordings with per-track volume control and sync offset |

### Server (`server/`)
- FastAPI app on port 8420 (`server/app.py`)
- Routes in `server/routes/`: `episodes.py`, `clips.py`, `pipeline.py`, `chat.py`, `publish.py`, `analytics.py`, `trim.py`
- `chat.py` is an AI-powered multi-turn chat route: maintains `chat_history.json`, loads full episode context into a system prompt, parses `action` JSON blocks from Claude's response to execute operations (approve/reject clips, update metadata, re-render shorts, etc.)
- Serves `frontend/` as static files with SPA catch-all

### Frontend (`frontend/`)
- Vanilla JS SPA — no framework, no build step
- Served directly as static files by FastAPI

## Configuration
- **`config/config.toml`** — All paths, thresholds, API settings (copy from `config.example.toml`)
- **`.env`** — API keys: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` (copy from `.env.example`, gitignored)
- Dependencies in `requirements.txt`, installed via `uv pip install`

## Key Constraints
- macOS resource fork files (`._*.MP4`) appear on SD cards — always filter: `if not f.name.startswith("._"):`
- Deepgram SDK v5 has a completely different API from v3 — use httpx REST API directly
- After modifying Python files, clear `__pycache__` or restart uvicorn (stale bytecode with `--reload`)
- `tomllib` (stdlib 3.11+) for TOML config — `tomli` has been removed
- ffmpeg 8.x: use `-shortest` not `-fflags +shortest`; use `-use_editlist 0` for platform compliance
- Never hardcode `/Volumes/1TB_SSD/` paths — use `lib/paths.resolve_path()`

## Error Handling
- If a pipeline fails mid-run, fix the issue and re-run with `--agents <remaining_agents>`
- Each agent's JSON output includes `_status`, `_elapsed_seconds`, and `_error` (if failed)
- Check `episode.json` → `pipeline.agents_completed` to see what's already done

## API Costs per Episode
- Deepgram transcription: ~$0.50 (stays on API — best-in-class STT)
- Claude clip mining: ~$0.10-0.30 (pending migration to `claude` CLI / Max subscription)
- Claude metadata: ~$0.10-0.20 (pending migration)

## Dev Harness
Task backlog: `wishlist.md`. Subagents in `.claude/agents/`:
- **`dev`** — feature work, bug fixes (`agents/`, `lib/`, `server/`, `frontend/`)
- **`clean`** — ruff F401/F811/F841 + dead code, no logic changes
- **`api-migrator`** — replace Anthropic SDK with `claude` CLI subprocess
- **`test-runner`** — run pytest, fix failures, no new tests

Hooks in `.claude/hooks/`: episode-data guard, force-push block, ruff pre-commit gate, Python cache reminder, stop summary.
