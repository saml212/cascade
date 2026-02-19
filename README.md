# Cascade

Podcast automation pipeline that turns raw Canon MP4 recordings into publish-ready shorts, longform video, and an RSS podcast feed.

## What It Does

Cascade runs a 13-agent pipeline:

1. **Ingest** — Copy MP4s from SD card to SSD, validate with ffprobe
2. **Stitch** — Concatenate clips via ffmpeg stream-copy
3. **Audio Analysis** — Detect true stereo vs identical/mono channels
4. **Speaker Cut** — Segment into L/R/BOTH based on per-channel RMS energy
5. **Transcribe** — Deepgram Nova-3 with diarization + SRT generation
6. **Clip Miner** — Claude identifies top 10 short-form candidates
7. **Longform Render** — 16:9 speaker-cropped video with hardware encoding
8. **Shorts Render** — 9:16 shorts with burned-in subtitles
9. **Metadata Gen** — Per-platform titles, descriptions, hashtags, schedule
10. **QA** — Validate all outputs (durations, file sizes, formats)
11. **Podcast Feed** — Extract audio, generate RSS, upload to Cloudflare R2
12. **Publish** — Distribute to YouTube, TikTok, Instagram
13. **Backup** — rsync episode to external HDD

Agents run in parallel where possible (transcribe runs alongside audio analysis + speaker cut).

## Quick Start

### Prerequisites

- **Python 3.10+**
- **ffmpeg** — `brew install ffmpeg`
- **uv** (recommended) — `brew install uv`

### Setup

```bash
git clone https://github.com/yourusername/cascade.git && cd cascade
cp .env.example .env   # Fill in your API keys (see below)
./start.sh             # Creates venv, installs deps, opens UI
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Fill in API keys
```

### API Keys

| Key | Required | Purpose |
|-----|----------|---------|
| `ANTHROPIC_API_KEY` | Yes | Claude — clip mining, metadata generation, chat |
| `DEEPGRAM_API_KEY` | Yes | Nova-3 transcription + speaker diarization |
| `YOUTUBE_CLIENT_ID` | No | YouTube publishing |
| `YOUTUBE_CLIENT_SECRET` | No | YouTube publishing |
| `TIKTOK_CLIENT_KEY` | No | TikTok publishing |
| `TIKTOK_CLIENT_SECRET` | No | TikTok publishing |
| `INSTAGRAM_ACCESS_TOKEN` | No | Instagram publishing |
| `FACEBOOK_PAGE_ID` | No | Instagram publishing |

Only `ANTHROPIC_API_KEY` and `DEEPGRAM_API_KEY` are required for the core pipeline (ingest through QA). Publishing keys are only needed if you use the publish agent.

### Run the Pipeline

```bash
# Full pipeline from SD card
python -m agents --source-path "/path/to/media/"

# Specific agents only
python -m agents --source-path "/path/to/media/" --agents ingest stitch audio_analysis

# With a custom episode ID
python -m agents --source-path "/path/to/media/" --episode-id ep_2026-02-19_120000
```

### Run the Web UI

```bash
./start.sh
# Opens http://localhost:8420 automatically
```

The web UI lets you review clips, approve/reject them, trim boundaries, chat with the AI about your episode, and trigger pipeline runs.

## MCP Server (AI Agent Integration)

Cascade includes an MCP server that lets Claude Code, Codex, or any MCP-compatible AI agent run the full pipeline autonomously.

### Setup for Claude Code

Add to your Claude Code MCP config or use the included `cascade.mcp.json`:

```json
{
  "mcpServers": {
    "cascade": {
      "command": ".venv/bin/python",
      "args": ["mcp_server.py"],
      "env": {}
    }
  }
}
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `check_prerequisites` | Verify Python, ffmpeg, API keys, disk space |
| `setup_environment` | Create venv, install deps, copy .env.example |
| `start_server` | Start the web UI (FastAPI on port 8420) |
| `stop_server` | Stop the web server |
| `get_server_status` | Check if server is running |
| `list_source_media` | List MP4 files on SD card or given path |
| `run_pipeline` | Run full or partial pipeline |
| `get_pipeline_status` | Check pipeline progress for an episode |
| `list_episodes` | List all episodes with status |
| `get_episode` | Get full episode details (clips, files, metadata) |
| `set_crop_config` | Set speaker crop positions for rendering |
| `extract_frame` | Extract a frame for visual crop reference |
| `list_clips` | List clips with status and scores |
| `approve_clips` | Approve specific clips or by score threshold |
| `auto_approve_clips` | Auto-approve all pending clips |
| `chat_with_episode` | Chat with the AI about an episode |
| `run_single_agent` | Re-run a specific pipeline agent |
| `get_config` | View current configuration |
| `get_transcript` | Read the diarized transcript |
| `backup_episode` | Backup to external HDD |

### Example AI Workflow

An AI agent can run a complete end-to-end episode with:

```
1. check_prerequisites()
2. setup_environment()
3. list_source_media("/Volumes/7/DCIM/100CANON/")
4. run_pipeline(source_path="/Volumes/7/DCIM/100CANON/")
5. extract_frame(episode_id="ep_...")     # Determine crop positions
6. set_crop_config(episode_id="ep_...", ...)
7. run_pipeline(source_path="...", agents="longform_render,shorts_render,metadata_gen,qa")
8. auto_approve_clips(episode_id="ep_...")
9. start_server()                          # For human review at localhost:8420
```

## Architecture

```
cascade/
├── agents/          # 13 pipeline agents (DAG-parallel execution)
│   ├── base.py      # BaseAgent ABC (timing, logging, JSON I/O)
│   ├── pipeline.py  # DAG orchestrator with dependency-aware parallelism
│   ├── ingest.py → stitch.py → audio_analysis.py → speaker_cut.py
│   ├── transcribe.py (runs parallel to audio_analysis + speaker_cut)
│   ├── clip_miner.py → shorts_render.py + metadata_gen.py (parallel)
│   ├── longform_render.py (starts when speaker_cut + transcribe finish)
│   ├── qa.py → podcast_feed.py → publish.py → backup.py
│   └── ...
├── lib/             # Shared utilities
│   ├── encoding.py  # VideoToolbox / libx264 encoder selection
│   ├── ffprobe.py   # ffprobe wrapper
│   ├── paths.py     # Path resolution
│   ├── clips.py     # Clip normalization
│   └── srt.py       # SRT formatting
├── server/          # FastAPI app (port 8420)
│   ├── app.py       # Entry point + static files
│   └── routes/      # API endpoints (episodes, clips, pipeline, chat, etc.)
├── frontend/        # Vanilla JS SPA for clip review + chat
├── config/          # config.toml — all settings
├── mcp_server.py    # MCP server for AI agent integration
├── tests/           # pytest + Jest test suites
└── start.sh         # One-command setup + launch
```

## Storage

By default, Cascade stores everything locally in `./episodes/` and `./work/`. This works out of the box with no external drives.

For large episodes (multi-GB source files), you can point to an external SSD by editing `config/config.toml`:

```toml
[paths]
output_dir = "/Volumes/1TB_SSD/cascade/episodes"
work_dir = "/Volumes/1TB_SSD/cascade/work"
backup_dir = "/Volumes/Seagate Portable Drive/podcast"
```

If an external drive path is configured but the volume isn't mounted, Cascade automatically falls back to local storage.

## Configuration

All settings live in `config/config.toml`. Key sections:

- **`[paths]`** — Output directory, work directory, backup drive (local fallback if drive missing)
- **`[processing]`** — CRF, resolution, clip duration limits, hardware acceleration
- **`[transcription]`** — Deepgram model, language, diarization settings
- **`[clip_mining]`** — LLM model, temperature, clip count
- **`[schedule]`** — Shorts posting cadence, peak days, timezone
- **`[platforms.*]`** — Per-platform publishing settings
- **`[podcast]`** — RSS feed metadata (title, author, artwork)

## API Costs per Episode

| Service | Cost |
|---------|------|
| Deepgram transcription | ~$0.50 |
| Claude clip mining | ~$0.10-0.30 |
| Claude metadata | ~$0.05-0.10 |

## License

MIT — see [LICENSE](LICENSE).
