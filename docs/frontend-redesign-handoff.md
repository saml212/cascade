# Frontend Redesign — Handoff to Design Agent

Copy everything below the `---` into a fresh Claude Code session with the frontend-design skill installed. This is the brief. Start by reading the current code before you design — then propose a design system, then build.

---

## Mission

Redesign the cascade frontend from scratch. The current UI is functional but visually flat and interactionally confusing. Your job is to ship a professional, quality-feeling, opinionated UI that matches the seriousness of the product (automated podcast production pipeline) and the polish of the user's reference brands (Huberman Lab, Acquired, Dwarkesh Patel — those are the quality bar for both output and tooling).

**The backend is frozen for this work.** FastAPI routes, Pydantic schemas, `episode.json` on disk, all cascade agents — none of these change. You're rebuilding `frontend/` as a drop-in replacement that reads/writes the existing API. If you need a new endpoint, stop and flag it to the developer — don't just add one.

## Who the user is

Sam is a podcast creator, not an engineer. He films episodes, hands off everything else to this software. He's on a Claude Max subscription and expects Claude agents to do the thinking. He does NOT read code, does NOT want to see state codes or file paths, and does NOT tolerate UI that makes him guess what just happened.

**Implications for design:**
- Every screen should report outcomes in plain language ("Longform is rendering, about 20 minutes left") — never raw state codes.
- Destructive actions need big, obvious confirmations.
- Progress, errors, and status changes should be visually loud and unambiguous.
- Minimize clicks. Every extra click between him and "publish" is wasted.
- The app is used on a desktop browser (not mobile). Assume a wide viewport.

## What cascade does (context)

Cascade ingests raw footage from Sam's camera and multi-track audio recorder, runs a 14-stage pipeline (video stitch, audio sync + enhancement, transcription, clip mining, longform render, shorts render, metadata generation, thumbnails, QA, RSS feed, multi-platform publish, backup), and at the end Sam's episode is live on YouTube, Spotify, TikTok, Instagram, X, LinkedIn, etc.

The pipeline has four human-review checkpoints:
1. **Crop setup** — Sam draws speaker crop boxes on a video frame and maps audio tracks to speakers. 2-speaker (DJI camera alone) or 3+-speaker (external Zoom H6essential multi-track recorder) setups.
2. **Longform review** — Sam watches the rendered longform, flags edits (trim front/back, cut mid-episode moments), approves.
3. **Clip + metadata review** — Sam reviews the 10 auto-picked short clips and their per-platform metadata.
4. **Publish + backup approval** — Sam clicks go, pipeline uploads to all platforms and backs up to external drive.

An agent (Claude Code `/produce` skill) orchestrates everything between these checkpoints.

## High-level design goals

1. **Feel premium.** This is a tool for a creator. It should feel like a tool the creator is proud to use, not a dashboard someone threw together. Think: Linear, Raycast, Superhuman, Cursor. Dense but elegant. Every surface considered.

2. **Strong visual hierarchy.** At a glance, Sam should know: which episode, what stage, what's blocking. Status is the most important piece of info on any screen — not buried, not in a state code.

3. **Status is a first-class element.** Live status for the pipeline (agent running, ETA, progress bar), publish (per-platform submission state), backup (copy progress). Where there's a watcher process, its events should stream into the UI.

4. **Editorial surfaces prioritize.** The clip review is the biggest UX win in this app — right now it's buried in tabs. Make that flow (10 clips, ~10 lines of hook per clip, per-platform metadata, keep/reject) feel more like reviewing a draft than configuring settings.

5. **Audio is central.** Cascade is an audio/video tool. Audio visualization (waveforms, sync indicators, mixer) is core UX, not an afterthought. Make it beautiful and accurate.

6. **Honest defaults.** If something is wrong (audio sync drifted, a platform failed to publish, a clip is too short), surface it loudly. If everything is green, signal that too — don't leave Sam wondering.

7. **No surprises.** Every button's actual effect should match its label exactly. If a button is gated on some state, tell Sam why it's disabled.

## The workflow flow map

Here's what Sam actually does, in order:

### 1. Session start
- Physically: plug in SD cards (DJI camera + Zoom H6E).
- Today: terminal + `./start.sh` + browser + Claude Code. In the redesign, assume a launcher will handle this; the UI loads to a populated dashboard.

### 2. Dashboard
- See all episodes on disk, each with status + next-action.
- Click one to enter detail. Create a new episode from an SD card dump.

### 3. New episode ingest
- Pick source path (SD card folder or archive path). Optionally pick separate audio path (H6E recorder folder). Specify speaker count.
- Press go — pipeline starts. Return to dashboard or stay on the new episode's detail page.

### 4. Crop setup (pause 1)
- Watch video scrub to find a frame where speakers are seated.
- Draw a crop box per speaker. Each speaker has TWO crops: 9:16 (shorts portrait) and 16:9 (longform landscape) — independently positioned and zoomed.
- For H6E episodes: map audio tracks (Tr1-Tr4, stereo Mix, built-in Mic) to speakers.
- Verify H6E audio sync by playing both camera audio and H6E audio simultaneously and adjusting offset.
- Optionally adjust per-track volume (override; defaults are usually right).
- Save. Pipeline resumes automatically.

### 5. Automated pipeline runs (speaker_cut → transcribe)
- Status panel shows live progress. Sam waits.

### 6. Clip mining (dispatched to a Claude Code subagent — not a UI concern)
- Clips.json lands. UI shows "10 clips picked" and a preview.

### 7. Longform render
- Takes 15-40 min. Big, visible progress bar. ETA. Breakdown of what it's doing (rendering segment 22/89).

### 8. Longform review (pause 2)
- Watch the rendered longform inline.
- Request edits in natural language: "trim the first 2 minutes, cut the strip-club story around 42 minutes."
- UI shows cuts as colored overlays on a timeline. Sam confirms, fires re-render.
- Approve → pipeline publishes longform to YouTube + updates RSS (triggering Spotify auto-ingest).

### 9. Waiting for YouTube URL (async, 15 min to several hours)
- Status shows "waiting for YouTube to process." When URL arrives, auto-advance OR Sam pastes URL.

### 10. Shorts + metadata (pause 3 — THE BIG UX PAYOFF)
- 10 clips rendered (9:16 with burned-in ASS captions).
- Inline conversation per clip: "Clip 3, 52s, ranked #2. Opens with 'so let me get this straight...'. Title: X. Keep?"
- Sam can: keep, reject, retitle, combine-with-adjacent, trim, request metadata rewrite.
- For each clip, per-platform metadata preview (collapsed by default; expanded on click). Per-platform captions/descriptions/hashtags.
- One-button "approve all, publish with the YouTube funnel."

### 11. Publish (pause 4)
- Scheduled posts per platform per clip. Show the schedule visually (calendar-ish).
- Per-platform live status: submitted / live / failed.
- When failures happen, show the error in plain language + retry / skip buttons.

### 12. Backup (pause 5)
- Copy to Seagate drive. Clear SD card (with explicit confirmation).
- Done screen: celebration moment with links to all the live posts.

## Screens to design (explicit list)

### Core screens
- **Dashboard** — list all episodes, status at a glance, one-click into detail. Status needs a dominant visual.
- **New Episode Wizard** — source path, audio path, speaker count. 1-2 steps max.
- **Episode Detail** — today has tabs (Overview, Clips, Metadata, Audio, etc.). Consider whether tabs are even right. Could be a single scrolling page with sticky section headers, OR a side-nav + content panel.
- **Crop Setup** — THE COMPLEX ONE. See "Crop setup detail" below. This page is where most of the UX complaints live.
- **Longform Review** — video player + cut timeline + natural-language edit input.
- **Clip Review** (currently part of Shorts/Metadata tabs) — the editorial conversation page. Possibly the most important screen.
- **Publish** — schedule preview + per-platform submission status.
- **Backup** — simple, mostly a confirmation screen.
- **Schedule** (existing route) — calendar view of scheduled posts across all episodes.
- **Analytics** (existing route, currently empty) — future: post performance data.

### Crop setup detail (the complex one)
This page today has six distinct concerns crammed together; redesign is a chance to make them flow cleanly:

1. **Header** — episode metadata (title TBD, duration, speaker count, audio setup type).
2. **Video scrubber with dual-audio playback** — a video player. Native mute controls stripped. Custom Camera/H6E toggle buttons. Play button starts both audio streams in sync. A live status indicator (`cam: playing / h6e: playing`) for debugging. Stripped video controls should show native play/pause + seek ONLY.
3. **Audio Track Mixer** (H6E episodes only) — 6 rows: Tr1, Tr2, Tr3, Tr4, Mix, Mic. Each row has solo/mute buttons, a volume slider, and a READ-ONLY assignment label (editing happens in Speakers panel). Load Audio + Play preview at the top. The preview is currently Web Audio client-side; ideally becomes "play the real audio_mix.wav" once backend exposes it.
4. **Audio Sync verification panel** (H6E episodes only) — video + H6E audio playback in sync, a waveform canvas showing both streams, an offset slider (± seconds), a save button. Scroll on offset input to nudge. Currently on the same page as the scrubber, duplicates a lot of UI.
5. **Speaker Crop editor** — interactive canvas where Sam clicks to place speaker center points. Each speaker has TWO crops (9:16 shorts + 16:9 longform), independently positioned. Toggle "Placing for: 9:16 Shorts / 16:9 Longform" determines which one gets placed on click. Each speaker's two rects render simultaneously in the same color, distinguished by line pattern (dashed vs dotted).
6. **Speakers panel (right side)** — per-speaker block: label, coordinates, TWO zoom sliders (Shorts + Longform), Track assignment dropdown.

There's also the "Wide Shot" (all speakers in frame) crop — its own center + zoom. And ambient track assignment.

Current pain: these 6 concerns fight for space on one page. Redesign: consider splitting into steps (Setup → Sync → Crop), OR a master-detail layout where the video + sync are sticky at the top and the crop editor expands below.

### Clip review detail (the editorial win)
Today split across "Clips" tab + "Metadata" tab. In the redesign, fuse them. Per clip:
- Small video thumbnail (autoplay on hover would be nice; 9:16 vertical preview).
- Duration, virality score, speaker attribution.
- Title + hook line + compelling reason (generated by clip-miner subagent).
- Per-platform metadata collapsed by default: YouTube, TikTok, Instagram, X, LinkedIn, Facebook, Threads, Pinterest, Bluesky.
- Actions: keep / reject / retitle / combine (if adjacent clip present) / trim.
- A conversation/input field where Sam types freeform ("make the titles more about the nuclear danger angle") and agent rewrites metadata.

This is where the non-technical user saves hours per episode. Make it feel fast.

## Design system you should propose

The redesign agent should PROPOSE and then BUILD:

- **Color system** — primary/brand, accent, success, warning, destructive, surface layers, text tiers. Dark mode is probably correct for this user.
- **Typography** — headings, body, mono for code/coordinates. One sans-serif family, one mono.
- **Spacing scale** — consistent 4 or 8 px unit.
- **Component library** — buttons (primary, secondary, destructive, icon), inputs (text, number, range slider with visible thumb, toggle, select), cards, status chips, progress bars (step-based + continuous), toast notifications, modals, tabs (if kept), navigation.
- **Motion** — subtle transitions on state change, loud celebrations on completion, loading states that don't feel bad.
- **Audio-specific components** — waveform visualization, mixer track row, volume slider with large thumb, sync offset control, audio playback state indicator.

Tailwind CSS (currently used via CDN — NOT production) should be swapped for either (a) Tailwind compiled via the CLI or PostCSS plugin, or (b) something else the design agent prefers. The CDN is a known problem.

Consider: is vanilla JS right, or should this be React/Svelte/Solid? The existing `frontend/app.js` is 4000+ lines of vanilla JS — functional but hard to modify. If the design agent chooses a framework, that's a significant scope addition — flag it to the developer before committing.

## Backend contract you must NOT break

The Pydantic models in `server/routes/*.py` are canonical. Read them first:

- `server/routes/episodes.py`:
  - `NewEpisodeRequest` (source_path, audio_path, speaker_count)
  - `EpisodeUpdateRequest`
  - `SpeakerCropConfig` (label, center_x, center_y, zoom, longform_center_x, longform_center_y, longform_zoom, track, volume)
  - `AmbientTrackConfig`
  - `CropConfigRequest`
  - `SyncOffsetRequest`
- `server/routes/pipeline.py` — run-pipeline, run-agent, pipeline-status, cancel-pipeline, resume-pipeline, auto-approve, approve-backup, approve-longform, approve-publish
- `server/routes/clips.py` — per-clip CRUD
- `server/routes/chat.py` — 14 editorial actions; this is a big one for the Clip Review screen, use it
- `server/routes/trim.py` — trim operations
- `server/routes/edits.py` — longform_edits
- `server/routes/schedule.py` — schedule views

All routes return JSON. No API change needed for most of the redesign. If you think an endpoint is missing, stop and flag it — don't add routes yourself.

`/Volumes/1TB_SSD/cascade/episodes/<id>/episode.json` is the source of truth. Read the Pydantic models + read a real episode's `episode.json` to understand the full shape.

## Deliverables

1. **A design system document** (`docs/design-system.md`) — colors, typography, spacing, components, voice. Written before building.
2. **A rebuilt frontend** (`frontend/` — replacing existing files). Ships alongside the current UI during development if feasible (e.g. at a `/new/` route) so the developer can A/B. Final swap cuts over cleanly.
3. **Updated README/docs** for the frontend: how to build/serve, component usage patterns, how to extend.
4. **Verification** against real episodes on disk (`ls /Volumes/1TB_SSD/cascade/episodes/`). There are several episodes in varying states — use them as end-to-end test cases.

## Verification pattern

The backend has 5 real episodes on disk:
- `ep_2026-02-17_234937` — PJ Greenbaum (2-speaker Canon, ready_for_review with clips + longform)
- `ep_2026-03-02_073400` — Laura + Todd (2-speaker Canon, ready_for_review)
- `ep_2026-03-18_204203` — Tug Life (3-speaker H6E, ready_for_review)
- `ep_2026-04-16_235129` — April 16 (3-speaker H6E, awaiting crop) — **live test case in progress; don't mutate state**
- `ep_2026-04-22_001253` — PJ rebuild (2-speaker Canon, awaiting crop) — **live test case**

Use these to verify every screen. The crop-setup page specifically needs one 2-speaker and one 3-speaker episode to exercise both modes. Don't delete anyone's work.

## Process recommendation

1. Read the current `frontend/app.js` end to end. Get a feel for what's there.
2. Read the Pydantic models and a real `episode.json`.
3. Spend one pass on design-system decisions. Write them down.
4. Build a skeleton of the dashboard + episode detail to establish patterns.
5. Build the Crop Setup page — the hardest one. Everything else will feel easier after.
6. Build Clip Review next — the highest-value surface.
7. Fill in remaining screens.
8. Polish. A lot.

## Things the developer wants you to avoid

- Don't fall into vanilla-JS monolith again. If vanilla, split into modules. If a framework, commit fully.
- Don't ship the Tailwind CDN.
- Don't add `anthropic` SDK calls from the frontend (backend-only).
- Don't change the Pydantic models or routes.
- Don't invent config options; read what's in `config/config.toml`.
- Don't commit on `main` without `/clean` passing (project has a pre-commit gate sentinel).

## How to know you're done

- Every flow Sam walks in the "What Sam actually does" section above works in the new UI from start to finish without needing terminal help.
- Hitting any state (awaiting_crop_setup, processing, awaiting_longform_approval, ready_for_review, awaiting_backup_approval, error, cancelled, completed) produces a clear, human-readable UI.
- Every destructive action has explicit confirmation.
- The Crop Setup page feels like one tool, not six tools crammed together.
- The Clip Review page makes 10-clip-review feel like it takes 5-10 minutes, not 30-45.
- Real episodes load + render correctly against the existing backend.

Good luck. Sam is a non-technical user who deserves a serious, quality tool. Build it that way.
