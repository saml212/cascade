# Cascade — Podcast Automation Pipeline

## Overview
Cascade is a 10-agent pipeline that processes podcast recordings from SD card to publish-ready shorts + longform video. It ingests Canon MP4 clips, stitches them, analyzes audio channels, segments speakers, transcribes via Deepgram, mines clips via Claude, renders longform (16:9) and shorts (9:16 with subtitles), generates platform metadata, and runs QA validation.

## Architecture
- **Config:** `config/config.toml` — all paths, thresholds, API settings
- **Agents:** `agents/` — 10 sequential agents, each producing JSON + media artifacts
- **Server:** `server/` — FastAPI app on port 8420 with episode/clip/pipeline routes
- **Frontend:** `frontend/` — SPA for clip review and approval
- **Working storage:** `/Volumes/1TB_SSD/cascade/` — all episode data lives on external SSD

## Pipeline Order
1. `ingest` — Copy MP4s from SD card to SSD, validate with ffprobe
2. `stitch` — Concatenate clips via ffmpeg stream-copy
3. `audio_analysis` — Detect true stereo vs identical channels
4. `speaker_cut` — Segment into L/R/BOTH based on per-channel RMS energy
5. `transcribe` — Deepgram Nova-3 with diarization + SRT generation
6. `clip_miner` — Claude identifies top 10 clips from transcript
7. `longform_render` — Render speaker-cropped 16:9 longform
8. `shorts_render` — Render 9:16 shorts with burned subtitles
9. `metadata_gen` — Claude generates per-platform metadata + schedule
10. `qa` — Validate all outputs

## How to Run

### Prerequisites
```bash
# Ensure .env has API keys
cat .env  # Check ANTHROPIC_API_KEY and DEEPGRAM_API_KEY are set

# Ensure SSD is mounted
ls /Volumes/1TB_SSD/cascade/

# Ensure ffmpeg is installed
ffmpeg -version
```

### Full Pipeline (CLI)
```bash
cd /Users/samuellarson/Local/Github/cascade
python -m agents --source-path "/Volumes/7/DCIM/100CANON/"
```

### With specific episode ID
```bash
python -m agents --source-path "/Volumes/7/DCIM/100CANON/" --episode-id ep_2026-02-13_test
```

### Run specific agents only
```bash
python -m agents --source-path "/Volumes/7/DCIM/100CANON/" --agents ingest stitch audio_analysis
```

### Via API
```bash
# Start server
./start.sh

# Trigger pipeline
curl -X POST http://localhost:8420/api/episodes/ep_001/run-pipeline \
  -H "Content-Type: application/json" \
  -d '{"source_path": "/Volumes/7/DCIM/100CANON/"}'

# Check status
curl http://localhost:8420/api/episodes/ep_001/pipeline-status

# Auto-approve clips
curl -X POST http://localhost:8420/api/episodes/ep_001/auto-approve
```

### Backup to Seagate HDD
```bash
rsync -av --progress "/Volumes/1TB_SSD/cascade/episodes/<episode_id>/" "/Volumes/Seagate Portable Drive/podcast/<episode_id>/"
```

## Key Paths
| Path | Purpose |
|------|---------|
| `/Volumes/1TB_SSD/cascade/episodes/` | Episode output (SSD working storage) |
| `/Volumes/1TB_SSD/cascade/work/` | Temp processing files |
| `/Volumes/7/DCIM/100CANON/` | SD card source (Canon) |
| `config/config.toml` | All configuration |
| `.env` | API keys (gitignored) |

## Episode Directory Structure
```
episodes/<episode_id>/
├── episode.json          # Master state
├── ingest.json           # Ingest results
├── stitch.json           # Stitch results
├── source_merged.mp4     # Stitched source
├── audio_analysis.json   # Channel analysis
├── segments.json         # Speaker segments
├── transcript.json       # Raw Deepgram response
├── diarized_transcript.json  # Speaker-labeled utterances
├── clips.json            # Mined clips
├── longform.mp4          # Final longform render
├── source/               # Copied source files
├── shorts/               # Rendered 9:16 clips
├── subtitles/            # SRT files
├── metadata/             # Platform metadata
├── qa/                   # QA results
└── work/                 # Temp files (WAVs, concat lists, RMS data)
```

## Error Handling
- If a pipeline fails mid-run, fix the issue and re-run with `--agents <remaining_agents>`
- Each agent's JSON output includes `_status`, `_elapsed_seconds`, and `_error` (if failed)
- Check `episode.json` → `pipeline.agents_completed` to see what's already done

## API Costs per Episode
- Deepgram transcription: ~$0.50
- Claude clip mining: ~$0.10-0.30
- Claude metadata: ~$0.10-0.20
