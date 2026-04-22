# Frontend → Backend handoff

Date: 2026-04-22
From: the frontend redesign session
To: the backend / producer agent maintaining `server/`, the pipeline agents in `agents/`, and `lib/`

The frontend rebuild in `frontend/` is done and served by `server/app.py`. `frontend-legacy/` is the old vanilla-JS app, still on disk for emergency rollback (`CASCADE_LEGACY_UI=1`). 18 commits on `main` this session, all authored by Sam Larson only — no AI attribution. The scaffolding + design system live in `docs/design-system.md` and `frontend/README.md`; this document is focused on **what the backend needs to know**.

## Summary of what shipped

Every page referenced in `docs/frontend-redesign-handoff.md` is built and rendering against real data from the four episodes on `/Volumes/1TB_SSD/cascade/episodes/`. The editorial surfaces (Clip Review with 9-platform metadata accordion + chat dock, Longform Review with cut timeline and natural-language edit input) and the Crop Setup surface (video scrubber + H6E waveform verifier + Web Audio track mixer) are all wired to the existing backend contract you own.

The typed API client lives at `frontend/src/lib/api.ts`. **Every route my UI calls is enumerated there.** If you rename or reshape any of those routes, that file is the first place to sync.

## Backend issues I hit that need your attention

### 1. `/audio-preview/{track_name}` is unusably slow

- **Symptom:** 2+ minute hang on first call per episode. I timed a single Tr1 preview at 120s+ before giving up. The uvicorn event loop gets wedged and other requests stall behind it until ffmpeg completes.
- **Path:** `server/routes/episodes.py:394` (`get_audio_preview`)
- **Root cause (my read):** The endpoint is `async def` but calls `subprocess.run(ffmpeg, …)` synchronously inside the async handler, so the event loop blocks. The input WAVs are ~1–2 GB 32-bit float files, and ffmpeg is doing a seek + 60s transcode per call. Combined with no pre-generation, the first hit on every page load stalls everything.
- **Fix options:**
  - Run ffmpeg in a thread pool: `await asyncio.to_thread(subprocess.run, cmd, …)`. Lowest-effort fix, stops blocking the loop.
  - Pre-generate previews at `audio_enhance` time so the endpoint becomes a cache lookup (the cache dir + file naming is already in place).
  - Stream ffmpeg output so the response can start before transcoding finishes.

### 2. `/channel-preview/{channel}` shows the same pattern

- Started returning 206s fine earlier in the session, then started timing out after enough audio-preview load had stacked up behind ffmpeg.
- Same async-blocking root cause as `/audio-preview`.

### 3. `/trim` can wedge a pipeline for 10+ minutes

- A POST to `/api/episodes/:id/trim` with trivial args (I sent `{trim_start_seconds: 0, trim_end_seconds: 0}`) kicked off an ffmpeg copy of the 59-minute `source_merged.mp4` to a trimmed sibling. It ran for the duration of the session until I killed it. Client request blocked the whole time, no response body.
- Consider making trim:
  - Fast-return with a job id (fire-and-forget, status via `pipeline-status`).
  - Short-circuit when `trim_start == trim_end == 0`.
  - Pre-flight validate that the trim actually changes anything before copying 1.6 GB.

### 4. `/api/episodes` needs a trailing slash; `/api/episodes/:id/clips` does not

- `GET /api/episodes/` returns the list; `GET /api/episodes` (no slash) falls through to the SPA catchall at `server/app.py:83` and returns `{"error":"not found"}`. This happens because the catchall fires on every non-matching path, pre-empting FastAPI's `redirect_slashes`.
- My client (`frontend/src/lib/api.ts`) works around it by using the trailing slash explicitly on the two routes that need it. But this will bite any other client that doesn't know.
- Suggestion: in `server/app.py:86`, don't return 404 for `api/` paths — let them fall through to FastAPI so the 307 redirect can do its job. (`app.include_router` is registered before the catchall, so FastAPI sees the routes first; the catchall should only match truly unmatched paths.)

### 5. `clips.status` is inconsistent across endpoints

- `GET /api/episodes/:id` returns clips with `status: "pending"` for the PJ episode.
- `GET /api/episodes/:id/clips/:clip_id` returns `status: "approved"` for the same clips.
- This makes the counts on the Clips tab (derived from the episode payload) diverge from the counts on the Clip Review page (derived from `/clips`). I expose the episode-payload count today; both should probably return the same ground truth.

### 6. `sync-offset` POST returns an empty body

- Sam expects a toast on save. My UI parses the response as JSON — when it's empty, I catch and ignore. Worth returning at least `{"status":"saved","offset_seconds":…}` so the client can confirm and optimistically update its display.

### 7. `alternative` clip endpoint is a placeholder

- The UI wires a "Request alternative" button to `POST /api/episodes/:id/clips/:clip_id/alternative`. The backend route is present but (per the route map I read) is a stub. Either implement it (re-dispatch clip_miner looking for a similar clip in a neighborhood), or I'll hide the button.

### 8. `thumbnail_gen` errors on several episodes

- Looking at the pipeline state of `ep_2026-02-17_234937` I saw `thumbnail_gen` wasn't run (it's not in `agents_completed`). For the longform player I'm using `crop_frame.jpg` as a poster fallback because no thumbnail is available on disk.
- If you stabilize thumbnail output (e.g. `<episode>/thumbnails/longform.jpg`), add an endpoint like `GET /api/episodes/:id/thumbnail?type=longform|clip&clip_id=…` and I'll swap the poster URL over in one line.

### 9. `audio_tracks[].filename` carries H6E session timestamps

- Track filenames look like `260311_162356_Tr1.WAV` (session-timestamp-prefixed). The audio-preview endpoint matches by exact filename or stem, so any client that hardcodes `Tr1`, `TrLR`, etc. 404s. My UI now resolves the real stem from `episode.audio_tracks` on the fly.
- Consider either: (a) accepting logical names server-side (`TrLR` matches any `*_TrLR.WAV`), or (b) storing a `role` field on each audio track in `episode.json` (`role: "sync" | "speaker:1" | "ambient"`) so clients don't have to regex-match filenames.

## Gaps the frontend exposes but depends on you to fill

### SSE event stream

- `frontend/src/lib/events.ts` defines `subscribe(episodeId, onEvent)` as the contract the rest of the UI uses. Today it's implemented as a 3-second poll of `/pipeline-status`.
- If you add `GET /api/episodes/:id/events` as an SSE endpoint that emits `{kind: "agent_start" | "agent_done" | "agent_error" | "status" | "progress", ...}` lines, I can swap the implementation in that one file without touching call sites.
- Highest-value events: agent transitions, percentage progress during `longform_render` / `shorts_render`, and terminal status flips.

### Published-URL auto-fill

- `episode.youtube_longform_url`, `spotify_longform_url`, `link_tree_url` are all editable in the Metadata tab. Today nothing populates them automatically. Sam has to paste them in. When `publish` uploads to YouTube, it knows the resulting URL — would be great if it `PATCH`'d episode.json with it so the Longform tab lights up "Live on YouTube" without Sam having to do anything.

### Progress payloads

- `pipeline.progress` is always `null` in what I've seen. The StepProgress component and the `ProgressBar` are ready to render `{percent: number, eta_seconds: number, detail: string}`. If a long-running agent (`longform_render`, `shorts_render`, `backup`) writes these to episode.json, the UI lights up automatically.

## What the frontend will NOT do (by design)

- **Agent chat panel** — explicitly Phase C per the brief. The right rail reserves 380px; today it shows a live event feed. When the launcher work puts a persistent `claude /produce` subprocess behind a `POST /agent/chat` endpoint, the rail can grow a chat surface.
- **`anthropic` SDK calls from frontend** — never. All LLM interaction routes through the backend's `/chat` action parser.
- **Destructive operations without explicit confirmation** — backup requires a typed phrase; episode delete is not wired (flag to me if you want it, it's a one-line addition).

## Flows I fully dogfooded

- Episode list loads, all 4 episodes render with correct status pills and metadata
- Episode detail sections (Overview with canonical 14-stage pipeline timeline + error summaries, Longform tab with player + cut details, Clips tab with 9:16 thumb grid + stats, Audio tab with H6E sync readouts, Metadata tab with save round-trip)
- Crop Setup for `ep_2026-04-22_001253` (Canon 2-speaker) — seeds defaults, drawing overlays, save payload shape verified
- Crop Setup for `ep_2026-03-18_204203` (H6E 3-speaker) — loads saved crops + speakers + tracks, waveform renders with 4× visibility gain, offset nudge controls
- Clip Review metadata save: `PATCH /api/episodes/:id/clips/:clip_id/metadata` round-trips correctly (tested with a noop update + revert)
- Schedule calendar: 7-day layout, today highlighted, 3 longforms queued render properly
- `complete-metadata` endpoint: returns the expected `{complete, iterations, actions_taken, summary}` shape

## Flows I wired but couldn't dogfood

- Actually approving a clip (`POST /clips/:id/approve`) — POST shape matches `clips.py`, not tested live
- `approve-longform`, `approve-publish`, `approve-backup` — UI calls them; pipeline advancement not driven end-to-end
- `resume-pipeline` — called after crop save; not driven
- Web Audio playback — graph constructs correctly and stem resolution works, but the audio-preview endpoint (see issue #1) was too slow in this session for the Play button to ever hear audio come back. Unblocking that endpoint should make the whole sync-by-ear flow light up
- Video scrubber on Crop Setup — UI toggles and RAF loop are correct; stall on initial video-preview load depended on backend responsiveness

## Commits shipped this session (most recent last)

```
7e79895  Scaffold Vite + TS + compiled Tailwind frontend rebuild
b354a34  Build Crop Setup editing surface (crops + track assignment)
03aca81  Build full Clip Review editorial surface
696c13a  Build Longform Review, Publish, Backup, and Schedule screens
a7d4b53  Shorten nav labels so they fit the 72px rail
2d1ac35  Add frontend README with dev/build/rollback instructions
2cca963  Crop Setup pass 2: H6E sync verifier + track mixer + episode metadata editor
c56f5c6  Polish pass 1: clips strip, longform-tab cut details, error summaries
6d5d17f  Polish pass 2: canonical pipeline, refined chrome, summarized errors
5b287bf  Polish pass 3: backup checklist, analytics preview grid, dashboard stagger
a71a0b4  Remove dashboard row stagger — was glitchy in screenshots
91263d2  Polish pass 4: one-click hero, hide Track for Canon, retire dead Settings
4cb20d3  Crop Setup pass 3: video scrubber + Web Audio playback for sync and mixer
0ac854e  Polish tail: longform timeline click-to-seek, poster, schedule empty days
97bbae0  Resolve real H6E stem names from episode.audio_tracks
```

## Where to look first

- `frontend/src/lib/api.ts` — the typed client. Every route I call is here.
- `frontend/src/lib/events.ts` — the SSE swap point.
- `frontend/src/state/episodes.ts` — polling cadence (episodes list every 8s, per-episode detail every 4s). Adjust if it's too chatty.
- `frontend/src/components/audio/` — SyncVerifier + TrackMixer, where the H6E stem resolution logic lives. If you change the `audio_tracks` shape, start here.
- `docs/design-system.md` — visual language, typography, color tokens, components. If you touch the UI, consult it first.
- `frontend/README.md` — dev server, prod build, legacy rollback.

Build:

```bash
cd frontend && npm install && npm run build
```

Dev server (with proxy to uvicorn on 8420):

```bash
cd frontend && npm run dev   # http://localhost:8421
```

Rollback to legacy:

```bash
CASCADE_LEGACY_UI=1 ./start.sh
```

That's it. The frontend is yours to iterate on; the biggest single win for Sam right now is unblocking issue #1 (audio preview perf) so the Play-to-verify-sync flow stops being theoretical.

— Sam
