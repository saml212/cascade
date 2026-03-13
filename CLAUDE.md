# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
Cascade is a 14-agent pipeline that processes podcast recordings from SD card to publish-ready shorts + longform video. It ingests Canon MP4 clips, stitches them, analyzes audio channels, segments speakers, transcribes via Deepgram, mines clips via Claude, renders longform (16:9) and shorts (9:16 with subtitles), generates platform metadata, generates caricature thumbnails via OpenAI, runs QA validation, generates a podcast RSS feed, publishes to platforms, and backs up to external storage.

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
- **`base.py`** — `BaseAgent` ABC: agents implement `execute() -> dict`. The `run()` wrapper handles timing, logging, writing `<agent_name>.json`, and `progress.json` for polling.
- **`pipeline.py`** — DAG-based parallel orchestrator using `ThreadPoolExecutor(max_workers=3)`. `AGENT_DEPS` defines the dependency graph (e.g., `longform_render` depends on both `speaker_cut` and `transcribe`). Agents run concurrently when deps are satisfied.
- **`__init__.py`** — `AGENT_REGISTRY` (name → class) and `PIPELINE_ORDER` (ordered list of 13 names).
- **`__main__.py`** — CLI entry point for `python -m agents`.

### Pipeline Behaviors
- After `stitch`, the pipeline **pauses** with `status: "awaiting_crop_setup"` if crop config isn't set — the user must configure speaker crop points via the API before resuming.
- After `clip_miner`, the episode directory is **renamed** to include the guest name slug.
- `NON_CRITICAL_AGENTS = {"podcast_feed", "publish", "backup"}` — failures here don't abort the pipeline.
- `episode.json` is the master state file, updated continuously.

### Pipeline Order
1. `ingest` → 2. `stitch` → 3. `audio_analysis` → 4. `speaker_cut` → 5. `transcribe` → 6. `clip_miner` → 7. `longform_render` → 8. `shorts_render` → 9. `metadata_gen` → 10. `thumbnail_gen` → 11. `qa` → 12. `podcast_feed` → 13. `publish` → 14. `backup`

### Shared Libraries (`lib/`)
| Module | Purpose |
|--------|---------|
| `paths.py` | `resolve_path()` — checks if external volume is mounted, falls back to local; `get_episodes_dir()` checks `CASCADE_OUTPUT_DIR` env var |
| `ffprobe.py` | `probe()`, `get_duration()`, `get_dimensions()` — wrappers over `ffprobe -print_format json` |
| `clips.py` | `normalize_clip()` — ensures both `start`/`end` and `start_seconds`/`end_seconds` exist |
| `srt.py` | `fmt_timecode()`, `escape_srt_path()` — SRT formatting and ffmpeg subtitle filter escaping |
| `encoding.py` | `has_videotoolbox()` — detects macOS GPU encoder; `get_video_encoder_args()` — returns VideoToolbox or libx264 args; `get_lut_filter()` — returns ffmpeg lut3d filter string from config |

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
- Python 3.11+ required; `.venv` is 3.11+, `start.sh` uses 3.12 via `uv`
- Uses `tomllib` (stdlib); `tomli` no longer needed
- macOS resource fork files (`._*.MP4`) appear on SD cards — must filter in globs
- Deepgram SDK v5 has a different API from v3 — use httpx REST API directly instead
- After modifying Python files, clear `__pycache__` or restart uvicorn (stale bytecode with `--reload`)

## Error Handling
- If a pipeline fails mid-run, fix the issue and re-run with `--agents <remaining_agents>`
- Each agent's JSON output includes `_status`, `_elapsed_seconds`, and `_error` (if failed)
- Check `episode.json` → `pipeline.agents_completed` to see what's already done

## API Costs per Episode
- Deepgram transcription: ~$0.50
- Claude clip mining: ~$0.10-0.30
- Claude metadata: ~$0.10-0.20
