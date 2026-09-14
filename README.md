# Cascade

Podcast automation pipeline that turns raw recordings into publish-ready shorts, longform video, and an RSS podcast feed. Supports single-camera or multi-camera setups with external multi-track audio (Zoom H6E or similar).

## What It Does

Cascade runs a 14-agent pipeline:

1. **Ingest** — Copy media from SD card(s) to SSD, validate with ffprobe, sync external audio
2. **Stitch** — Concatenate clips via ffmpeg stream-copy
3. **Audio Analysis** — Detect true stereo vs identical/mono channels
4. **Speaker Cut** — Segment speakers via per-channel RMS energy (supports N-speaker multi-track)
5. **Transcribe** — Deepgram Nova-3 with diarization + SRT generation
6. **Clip Miner** — Claude identifies top 10 short-form candidates
7. **Longform Render** — 16:9 speaker-cropped video with hardware encoding
8. **Shorts Render** — 9:16 shorts with burned-in subtitles
9. **Metadata Gen** — Per-platform titles, descriptions, hashtags, schedule
10. **Thumbnail Gen** — AI-generated caricature artwork via OpenAI
11. **QA** — Validate all outputs (durations, file sizes, formats)
12. **Podcast Feed** — Extract audio, generate RSS, upload to Cloudflare R2
13. **Publish** — Distribute to YouTube, TikTok, Instagram, and more
14. **Backup** — rsync episode to external HDD

Agents run in parallel where possible (transcribe runs alongside audio analysis + speaker cut).

## Quick Start

### Prerequisites

- **Python 3.11+** (`start.sh` creates a Python 3.12 environment)
- **ffmpeg** with the `ass` subtitle filter. On Homebrew, use `brew install ffmpeg-full` and ensure that build is first on `PATH`.
- **uv** (required by `start.sh`) — `brew install uv`
- **Node.js + npm** — used to compile the TypeScript frontend

### Setup

```bash
git clone https://github.com/saml212/cascade.git && cd cascade
cp config/config.example.toml config/config.toml  # Edit paths & podcast info
cp .env.example .env                               # Fill in your API keys (see below)
./start.sh                                         # Repairs setup, builds UI, opens loopback server
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..
cp config/config.example.toml config/config.toml   # Edit paths & podcast info
cp .env.example .env                               # Fill in API keys
```

### API Keys

| Key | Required | Purpose |
|-----|----------|---------|
| `ANTHROPIC_API_KEY` | For API generation | Clip mining and automatic metadata generation |
| `DEEPGRAM_API_KEY` | For transcription | Nova-3 transcription + speaker diarization |
| `OPENAI_API_KEY` | No | Thumbnail generation (caricature artwork) |
| `YOUTUBE_CLIENT_ID` | No | YouTube publishing |
| `YOUTUBE_CLIENT_SECRET` | No | YouTube publishing |
| `TIKTOK_CLIENT_KEY` | No | TikTok publishing |
| `TIKTOK_CLIENT_SECRET` | No | TikTok publishing |
| `INSTAGRAM_ACCESS_TOKEN` | No | Instagram publishing |
| `FACEBOOK_PAGE_ID` | No | Instagram publishing |
| `CLOUDFLARE_ACCOUNT_ID` | No | Podcast RSS feed (R2 storage) |
| `CLOUDFLARE_API_TOKEN` | No | Podcast RSS feed (R2 storage) |
| `UPLOAD_POST_API_KEY` | No | Upload-Post publishing |
| `UPLOAD_POST_USER` | No | Upload-Post publishing |

Local import, framing, manual metadata, audio mastering, and upload-video
preparation do not require either key. The full automated pipeline needs
`ANTHROPIC_API_KEY` and `DEEPGRAM_API_KEY` for its generation and transcription
stages. Episode chat uses the installed, authenticated `claude` CLI. Publishing
and RSS keys are only needed for those specific agents.

The default audio mastering path uses ffmpeg and the lean dependencies in
`requirements.txt`. DeepFilterNet restoration is optional because its PyTorch
runtime is large. Install it only when the configured denoise model needs it:

```bash
uv pip install --python .venv/bin/python -r requirements-restoration.txt
```

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

The web UI uses a three-step release workflow: confirm picture and sound, edit
episode details, then prepare and download verified local upload files. Publishing
is a separate explicit action. FastAPI serves the canonical TypeScript/Vite app
from `frontend/dist`; `./start.sh` rebuilds it before every launch.

## Architecture

See [the system architecture](docs/architecture.md) and the
[recovery and production workflow](docs/recovery-workflow.md) for the API and
artifact contracts used by both the UI and autonomous agents.

```
cascade/
├── agents/          # 14 pipeline agents (DAG-parallel execution)
│   ├── base.py      # BaseAgent ABC (timing, logging, JSON I/O, config helpers)
│   ├── pipeline.py  # DAG orchestrator with dependency-aware parallelism
│   ├── ingest.py → stitch.py → audio_analysis.py → speaker_cut.py
│   ├── transcribe.py (runs parallel to audio_analysis + speaker_cut)
│   ├── clip_miner.py → shorts_render.py + metadata_gen.py (parallel)
│   ├── longform_render.py (starts when speaker_cut + transcribe finish)
│   ├── thumbnail_gen.py → qa.py → podcast_feed.py → publish.py → backup.py
│   └── ...
├── lib/             # Shared utilities
│   ├── encoding.py  # VideoToolbox / libx264 encoder selection + LUT support
│   ├── ffprobe.py   # ffprobe wrapper
│   ├── audio_mix.py # Multi-track audio mixing with per-track volume control
│   ├── paths.py     # Path resolution (external drive fallback)
│   ├── clips.py     # Clip normalization
│   └── srt.py       # SRT generation, parsing, and ffmpeg escaping
├── server/          # FastAPI app (port 8420)
│   ├── app.py       # Entry point + static files
│   └── routes/      # API endpoints (episodes, clips, pipeline, chat, trim, etc.)
├── frontend/        # TypeScript + Vite SPA; FastAPI serves frontend/dist
├── config/          # config.toml — all settings
├── tests/           # Python pytest suite
├── frontend/tests/  # TypeScript helper/state tests run with Node
└── start.sh         # One-command setup + launch
```

## Storage

By default, Cascade stores everything locally in `./episodes/` and `./work/`. This works out of the box with no external drives.

For large episodes (multi-GB source files), you can point to an external SSD by editing `config/config.toml`:

```toml
[paths]
output_dir = "~/cascade/episodes"
work_dir = "~/cascade/work"
backup_dir = "~/cascade/backup"
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
- **`[podcast.links]`** — Link-in-bio page URLs (see below)

## Links Page (Link-in-Bio)

Cascade includes a built-in link-in-bio page generator. Fill in the `[podcast.links]` section of your config with your platform URLs, then generate the static HTML:

```bash
python -m links.generate
```

This produces `links/index.html` — a single-file, dark-themed page with your podcast artwork, platform links, and an embedded Spotify player. Deploy it to Cloudflare Pages, GitHub Pages, Netlify, or any static host.

Supported platforms: Spotify, Apple Podcasts, YouTube, Instagram, X, TikTok, iHeartRadio, GitHub. Empty URLs are automatically excluded.

To prepare revision-bound landing pages for each episode, write a reviewable site to
a separate directory. An optional Apple catalog supplies exact Apple episode URLs;
otherwise the page labels the configured Apple show URL as a show-level fallback.

```bash
.venv/bin/python -m links.episode_hub prepare \
  --episodes-root /path/to/cascade/episodes \
  --output-dir /tmp/cascade-watch-site \
  --apple-catalog /path/to/apple-current-video-catalog.json
```

The prepared manifest binds every HTML file by size and SHA-256. After review, upload
those exact bytes to the configured R2 bucket; episode pages are uploaded before the
branded `links/index.html` entry point.

```bash
.venv/bin/python -m links.episode_hub upload \
  --site-dir /tmp/cascade-watch-site
```

The API exposes the same current link resolution at
`GET /api/episodes/{episode_id}/watch-links` and an HTML preview at
`GET /api/episodes/{episode_id}/watch-page`.

## API Costs per Episode

| Service | Cost |
|---------|------|
| Deepgram transcription | ~$0.50 |
| Claude clip mining | ~$0.10-0.30 |
| Claude metadata | ~$0.05-0.10 |

## License

MIT — see [LICENSE](LICENSE).
