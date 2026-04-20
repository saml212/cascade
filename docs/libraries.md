# Shared Libraries (`lib/`)

USE these modules — do not DIY what they already provide.

| Module | Purpose |
|--------|---------|
| `paths.py` | `resolve_path()` — checks if external volume is mounted, falls back to local. `get_episodes_dir()` checks `CASCADE_OUTPUT_DIR` env var. |
| `ffprobe.py` | `probe()`, `get_duration()`, `get_dimensions()` — wrappers over `ffprobe -print_format json`. **All ffprobe calls go through this module.** |
| `clips.py` | `normalize_clip()` — ensures both `start`/`end` and `start_seconds`/`end_seconds` exist. |
| `srt.py` | `fmt_timecode()`, `escape_srt_path()`, `generate_srt_from_diarized()`, `parse_srt()`, `parse_srt_time()` — shared SRT generation, parsing, and ffmpeg escaping. |
| `encoding.py` | `has_videotoolbox()` (macOS GPU encoder detect), `get_video_encoder_args()` (VideoToolbox or libx264), `get_lut_filter()` (ffmpeg lut3d filter from config). |
| `audio_mix.py` | `generate_audio_mix()` — pre-mixed stereo WAV from multi-track H6E with per-track volume control and sync offset. |
| `audio_enhance.py` | highpass → lowpass → compressor → loudnorm chain; optional ML denoise (ClearerVoice-Studio MossFormer2_SE_48K). |

## Hard rules
- Never call `subprocess.run(["ffprobe", ...])` directly — use `lib/ffprobe`.
- Never hardcode `/Volumes/1TB_SSD/` paths — use `lib/paths.resolve_path()`.
- Never DIY SRT timecode formatting — use `lib/srt`.
- Filter chain order is fixed: LUT (10-bit) → crop → scale → `format=yuv420p` → subtitles.
