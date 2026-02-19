# Cascade

Podcast automation pipeline that processes Canon MP4 recordings into publish-ready shorts and longform video.

## What It Does

Cascade runs a 13-agent pipeline that:
1. **Ingests** raw Canon MP4 clips from an SD card
2. **Stitches** them into a single source file
3. **Analyzes** audio channels (true stereo vs mono)
4. **Segments** speakers by L/R channel energy
5. **Transcribes** via Deepgram Nova-3 with diarization
6. **Mines clips** via Claude (top 10 short-form candidates)
7. **Renders longform** (16:9 with speaker-cropped segments)
8. **Renders shorts** (9:16 with burned-in subtitles)
9. **Generates metadata** (per-platform titles, captions, schedule)
10. **Runs QA** validation on all outputs
11. **Generates podcast RSS feed** and uploads to Cloudflare R2
12. **Publishes** to YouTube, TikTok, Instagram via Upload-Post
13. **Backs up** episode to external HDD via rsync

## Quick Start

### Prerequisites

- Python 3.10+
- ffmpeg (install via `brew install ffmpeg`)
- API keys in `.env`:
  - `ANTHROPIC_API_KEY` (Claude — clip mining + metadata)
  - `DEEPGRAM_API_KEY` (transcription)
  - `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (podcast feed, optional)
  - `UPLOAD_POST_API_KEY` + `UPLOAD_POST_USER` (publishing, optional)

### Setup

```bash
git clone <repo-url> && cd cascade
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Fill in API keys
```

### Run Full Pipeline (CLI)

```bash
python -m agents --source-path "/Volumes/7/DCIM/100CANON/"
```

### Run Specific Agents

```bash
python -m agents --source-path "/path/to/media" --agents ingest stitch audio_analysis
```

### Run Web UI

```bash
./start.sh
# Open http://localhost:8420
```

## Architecture

```
cascade/
├── agents/          # 13 sequential pipeline agents
│   ├── base.py      # BaseAgent ABC (timing, logging, JSON I/O)
│   ├── pipeline.py  # Orchestrator (runs agents, manages episode state)
│   ├── ingest.py    # Copy MP4s from SD card → SSD
│   ├── stitch.py    # Concatenate clips via ffmpeg
│   ├── audio_analysis.py  # Stereo vs mono classification
│   ├── speaker_cut.py     # L/R/BOTH speaker segmentation
│   ├── transcribe.py      # Deepgram Nova-3 transcription
│   ├── clip_miner.py      # Claude-powered clip selection
│   ├── longform_render.py # 16:9 speaker-cropped render
│   ├── shorts_render.py   # 9:16 shorts with subtitles
│   ├── metadata_gen.py    # Per-platform metadata via Claude
│   ├── qa.py              # Output validation
│   ├── podcast_feed.py    # RSS feed + R2 upload
│   ├── publish.py         # Upload-Post distribution
│   └── backup.py          # rsync to external HDD
├── lib/             # Shared utilities (paths, ffprobe, clips, SRT)
├── server/          # FastAPI app (port 8420)
│   ├── app.py       # Entry point + middleware
│   └── routes/      # API endpoints
├── frontend/        # Vanilla JS SPA for clip review
├── config/          # config.toml — all settings
└── tests/           # pytest + Jest test suites
```

## API Costs per Episode

| Service | Cost |
|---------|------|
| Deepgram transcription | ~$0.50 |
| Claude clip mining | ~$0.10–0.30 |
| Claude metadata | ~$0.10–0.20 |

See [CLAUDE.md](CLAUDE.md) for detailed configuration, paths, and error handling.
