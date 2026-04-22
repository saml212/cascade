---
name: produce
description: Drive a cascade episode from ingest through publish + backup. Use when Sam wants to produce an episode end-to-end, says "status of episode X", "keep going on this episode", "publish this one", or hands off a new SD-card dump. Orchestrates the 4 human-review checkpoints (crop, longform approval, clip + metadata review, publish/backup) as conversations, fires the pipeline in between, dispatches subagents for clip-mining and metadata, monitors for stalls, and routes errors in plain language. This skill is the smart layer on top of the programmatic pipeline.
---

# /produce — episode production workflow

Sam (the filmmaker) films episodes and hands off everything else to this skill. The skill is a conversational operating mode over the cascade pipeline: status reporting, UI handoffs, editorial tooling, publish/backup approvals, and proactive quality monitoring. It replaces clicking-through-the-UI as the default path.

The pipeline itself stays programmatic (Python agents under `agents/`, FastAPI routes under `server/`). This skill decides *when* to run stages, *what* to check, *what* to ask Sam, and *how* to recover when things break. The cognitive load stays with the agent, not Sam.

**Always speak in plain language.** Never output `agent_name`, `episode_id`, filenames, route paths, or state codes to Sam unless he asks. Report what changed in terms of what he'll see or do next.

---

## Invocation

- `/produce` — Status sweep of `/Volumes/1TB_SSD/cascade/episodes/`. One-table summary: episode → state → next action.
- `/produce <episode_id>` — Drive that episode to the next human action.
- `/produce <episode_id> <instruction>` — Inline instruction (e.g. `/produce ep_X "cut the strip-club bit and combine clips 3 and 4"`). Parse, act, confirm.

---

## Entry logic (run on every invocation)

### Step 1 — preconditions
Fail fast with a one-sentence fix if any of these miss:

| Check | How | Fix (skill takes action, not Sam) |
|---|---|---|
| SSD mounted | `ls /Volumes/1TB_SSD/cascade/episodes/` | Tell Sam: "Plug the SSD in." (physical action only Sam can do) |
| Cascade server up | `lsof -iTCP:8420 -sTCP:LISTEN` (non-empty) | **Skill starts the server itself:** `nohup ./start.sh > /tmp/cascade-server.log 2>&1 &`. Poll `curl -s http://localhost:8420/api/episodes` until it responds (up to 30s). Only tell Sam if startup fails after two tries. |
| Episode exists | `ls /Volumes/1TB_SSD/cascade/episodes/<id>/episode.json` | "I can't find that episode — did you mean `<closest match>`?" |

**Server lifecycle is the skill's responsibility, not Sam's.** Start it when needed, restart it if it dies mid-session, leave it running between turns. Sam should never be asked to run `./start.sh` manually. The skill OWNS the server process.

### Step 2 — check for concurrent drivers (best effort)
Write/refresh a lock file: `/Volumes/1TB_SSD/cascade/episodes/<id>/.produce.lock` with the current timestamp. If the file exists with a timestamp < 5 min old AND Sam hasn't explicitly said "continue anyway," warn: "Another /produce session may be working on this — if that's wrong, delete `.produce.lock` and retry."

This is BEST-EFFORT — two Claude Code sessions can't coordinate perfectly, and state is persistent in `episode.json` so partial work is never lost. The lock is a cheap guard against Sam accidentally running two chats at the same time, not a distributed lock.

### Step 3 — read state
Load `episode.json`. Use `status` + side-state (clips present? metadata present? publish.json present?) to decide next move. Do NOT trust `status` alone — the pipeline can leave state stale if an agent crashed mid-update.

---

## State → action map

Every state corresponds to ONE of: "agent is doing something, wait and monitor" OR "Sam has the ball." Never leave Sam unsure which it is.

### ● `awaiting_crop_setup`  —  *Sam's move*
The pipeline stopped because it needs a visual crop that only a human can do.

**Skill does:**
1. Summarize what the episode is from `episode.json`: duration, speaker_count, audio setup (2-speaker DJI-only vs 3+ speaker H6E multi-track).
2. For H6E setup: describe which Zoom tracks (`Tr1`-`Tr4`) map to which speaker based on `audio_tracks`. E.g. "You've got 4 mic inputs recorded; you'll need to tell the UI which track belongs to which speaker when you draw the boxes."
3. Tell Sam: "Open cascade in the browser, go to this episode, and do the crop. When you're done, I'll pick it up automatically."
4. Start a monitoring loop (`ScheduleWakeup` ~300s, or just wait on his next turn). On wake: re-read episode.json, check if status moved. If yes → advance.

**Skill does NOT:** try to guess the crop, recommend zoom values, or touch `crop-config`.

### ● `processing`  —  *pipeline is running*
Pipeline is firing a chain of agents. Sam doesn't need to do anything. The skill's job is to monitor and not alarm him unless something's wrong.

**Skill does:**
1. Read `progress.json` (if present) or `episode.json.pipeline.agents_completed` vs `.agents_requested` to know which stage is running.
2. Report the current stage and a rough ETA. Reference ETAs (wall-clock on a 90-min episode):

   | Stage | Typical | Worst |
   |---|---|---|
   | ingest | 5-10 s | 30 s |
   | stitch | 1 min | 5 min |
   | audio_analysis | 15 s | 1 min |
   | speaker_cut | 20-60 s | 3 min |
   | transcribe | 2-4 min | 10 min |
   | clip_miner | n/a (dispatched as subagent, see below) | |
   | longform_render | 15-40 min | 60 min |
   | shorts_render | 3-8 min | 15 min |
   | metadata_gen | 30-60 s | 3 min |
   | thumbnail_gen | 20-40 s | 2 min |
   | qa | 5-10 s | 30 s |
   | podcast_feed | 30-90 s | 5 min |
   | publish | 2-5 min | 15 min |
   | backup | 5-15 min | 30 min |

3. **Stall detection:** if a stage's modified timestamp on `progress.json` hasn't updated in > 2× its worst-case ETA, something's wrong. Alert Sam: "X has been running for Y min with no progress. Want me to check the log or restart from that stage?"
4. **Intercept before `clip_miner` runs:** if the next programmatic agent is `clip_miner`, abort the programmatic run and dispatch the **clip-miner subagent** instead (see below). Update `pipeline.agents_completed` to include clip_miner once the subagent finishes so programmatic longform_render will start.
5. On error (`status == "error"` or an agent raised): read the error message from the agent's output json (e.g. `ingest.json.error`), translate into plain language, offer a resume option.

## Publishing flow — longform FIRST, then shorts (the funnel requires it)

Shorts exist to funnel viewers to the longform. Without a live longform URL to point at, shorts have nothing to promote — posting them burns the virality window for nothing.

**Sequence (enforced by the code):**

1. Longform renders → Sam approves.
2. **Longform publishes first** (YouTube via Upload-Post, Spotify via RSS ingest) — shorts are NOT rendered or submitted yet.
3. YouTube takes 15 min to several hours to process the longform and return its public URL. Sam's RSS feed must be registered with Spotify for Podcasters (one-time; see `roadmap.md` item 3).
4. `/produce` polls Upload-Post's status endpoint OR Sam pastes the YouTube URL when YouTube emails him. URL lands in `episode.json.youtube_longform_url`.
5. **Now shorts can render.** The `shorts_render` agent has a hard gate: it refuses to run if `episode.json.youtube_longform_url` is empty and clips exist. This prevents shorts from ever shipping without a funnel.
6. `metadata_gen` runs with the URL available, so captions and descriptions reference `thelocalpod.link` (link-in-bio) — NOT the raw URL in comments (platforms de-rank that).
7. `publish` runs again, this time with shorts rendered. The longform block has an idempotency guard (skips re-upload if `youtube_longform_url` is already set), so only the shorts actually go out on the second run.

**What `/produce` fires at each transition (explicit agent lists, not the bundled approve-longform):**

After longform approval:
```
POST /api/episodes/<id>/resume-pipeline  agents=["podcast_feed", "publish"]
```
This publishes the feed (triggers Spotify ingest) and uploads the longform to YouTube. Shorts skip because none exist yet.

After YouTube URL returns:
1. Save URL to `episode.json.youtube_longform_url` (for now: Sam pastes; future: auto-poll).
2. `POST /api/episodes/<id>/resume-pipeline` with `agents=["shorts_render", "metadata_gen", "thumbnail_gen", "qa"]`.

After clip + metadata review and Sam's "ship it":
```
POST /api/episodes/<id>/resume-pipeline  agents=["publish"]
```
Longform block no-ops (idempotency); shorts upload with the funnel.

After shorts publish:
Sam gets the backup prompt.

### ● `awaiting_longform_approval`  —  *Sam's move*
The longform video is rendered. Sam needs to watch it and decide.

**Skill does:**
1. Prime the review: report duration, render time, QA warnings from `qa/qa.json` (if `qa` ran before longform — it usually runs after but may warn ahead).
2. **Proactive quality checks** (flag, don't fix):
   - **2-speaker dual-mono sanity check**: if speaker_count == 2 and there's no H6E audio (`audio_tracks == []`), run `ffprobe`/ffmpeg on the final audio_mix.wav and check L/R correlation. If correlation < 0.9, flag: "Audio is panned, not dual-mono — you'll hear only speaker 1 on your left ear. Fix this before approving." (See TODO on verifying the `pan=` fix.)
   - **Audio sync confidence**: if `episode.json.audio_sync.confidence` < 0.8, warn: "Audio sync confidence is low. Listen for drift toward the end."
   - **Length sanity**: if duration < 10 min or > 3 hr, warn — probably a bad trim.
3. Run `auto_trim` action proactively to propose intro/outro trim points. Present as SUGGESTION: "AI thinks the real conversation starts at 1:45 and ends at 78:12. Trim those? Or you want to pick different points?"
4. Wait for Sam's feedback. He'll come back with things like "cut 12:34-13:01" or "trim start to 2:15, also cut the strip-club bit around minute 42."
5. For each ask, use the chat endpoint's existing actions: `edit_longform` (cut/trim_start/trim_end), `auto_trim`, `rerender_longform`. Or POST directly to the route.
6. For a **mid-episode cut** like the strip-club moment: see "Mid-episode cut flow" below — it's a multi-step search-and-confirm.
7. Batch edits before firing `rerender_longform` — the render is expensive (15-40 min). Ask "any more cuts before I re-render, or run it now?"
8. On Sam's "looks good" → `POST /api/episodes/<id>/approve-longform`.

### ● `processing` (post-longform: shorts_render → metadata_gen → thumbnail_gen → qa → podcast_feed)  —  *pipeline running*
Same monitoring as the first `processing` block. No subagent dispatch here (clip-miner has already run pre-longform per the interception above).

### ● `awaiting_longform_urls`  —  *async wait*
**This is the two-phase pause between longform publish and shorts render.**

After Sam approves the longform, `/produce` fires longform publish (no shorts yet). Then it enters this phase: wait for YouTube's public URL to come back (15 min to several hours after submit) and wait for Spotify RSS ingest (~1 hour, assuming the feed is registered with Spotify).

**Skill does:**
1. Write state to `episode.json`: either a new `status: "awaiting_longform_urls"` OR just leave as `processing` + check for `youtube_longform_url`.
2. If URL polling is available: poll `/api/uploadposts/status` every 5-15 min until YouTube returns the video URL. Save to `episode.json.youtube_longform_url` when it lands.
3. If URL polling isn't available: tell Sam "longform uploaded. YouTube will process for 15 min to a few hours. Paste the URL here when it's live, or send me the YouTube email." On next turn, extract URL and save.
4. For Spotify URL: if registered with Spotify for Podcasters, the RSS ingest happens automatically within ~1 hour. Sam may or may not paste the Spotify URL — not strictly required to proceed, but richer funnel if he does.
5. Once `youtube_longform_url` is set: announce "Longform is live. Rendering shorts now with the funnel." Fire `resume-pipeline` with `["shorts_render", "metadata_gen", "thumbnail_gen", "qa"]`.

### ● `ready_for_review`  —  *Sam's move*
**This is the shorts-publish gate, reached ONLY AFTER the longform is live and URL-ed.** The pipeline lands at `ready_for_review` after shorts_render + metadata_gen + thumbnail_gen + qa complete in phase 2.

Clip + metadata review happens here as ONE conversation. This is the biggest UX win: stop making Sam click through 10 clips × 8 platforms of metadata.

**Skill does:**
1. Read `clips.json`, `metadata/metadata.json`, and any existing `episode.json.guest_context`.
2. If `guest_context` is empty: "Tell me about the guest in 2 sentences — what they do, what's interesting about them for the audience." Persist Sam's answer to `episode.json.guest_context`.
3. For each clip (top-ranked first):
   - One-line summary: "Clip 3, 52s, ranked #2, speaker_0: [title]. Opens with '[hook line]'. Ends on [last line]."
   - Current AI metadata per-platform (collapsed to YouTube + TikTok by default; show others on ask).
   - Ask Sam's verdict: keep / reject / retitle / combine with adjacent / trim.
4. Dispatch the `metadata-writer` subagent (TODO: build) with guest context + clip info + Sam's notes per-platform. Apply results via `update_platform_metadata` action.
5. Support natural-language requests:
   - "combine 3 and 4" → check timestamps touch (< 5s gap). Update clip_3 to span both; delete clip_4. If gap > 5s, ask Sam whether to stitch or skip.
   - "reject the nuclear-safety one" → find by title match, confirm, apply `reject_clip`.
   - "retitle clip 7 to something about the tugboat" → dispatch metadata-writer with directional context for clip 7.
6. Before moving to publish, verify each clip has: title, at least YouTube + TikTok metadata, virality_score (from clip-miner), status in {approved, rejected}.
7. On Sam's "ship it" → `POST /api/episodes/<id>/approve-publish`. Pipeline fires podcast_feed + publish; skill enters monitoring mode.

### ● `approved`  —  *legacy state, approve-publish pending*
Older UI path set this when clips were approved but before publish was triggered. Treat same as `ready_for_review` for /produce purposes.

### ● `processing` (publish running)  —  *pipeline running*
Monitor `publish.json` as it updates. When done:
- If all platforms submitted: advance to `awaiting_backup_approval`.
- If per-platform failures (X post failed, etc.): alert Sam with the specific platform + error message (the surfacing this session added). Ask whether to retry or skip and move on.

### ● `awaiting_backup_approval`  —  *Sam's move*
Publish is out. Offer backup: "Ready to copy this episode folder to the Seagate drive and clear the SD card?" On yes → `POST /api/episodes/<id>/approve-backup`.

### ● `cancelled` / `error`  —  *diagnose and resume*
1. Read the error message from the most recent agent's output json (e.g. `stitch.json.error`).
2. Translate to plain language: "Stitch failed because X. Probably because Y. I can try Z to fix it."
3. Ask Sam before taking action. Common fixes:
   - Missing source file → re-run `ingest`.
   - ffmpeg exit code → check disk space, permissions.
   - API timeout → retry.
4. If Sam approves → `POST /api/episodes/<id>/resume-pipeline`.

---

## How /produce interleaves with the programmatic pipeline

The Python pipeline has a `run-pipeline` route that fires all 14 agents in order. /produce does NOT use that route — it would run the API-driven `clip_miner` and cost Sam real money. Instead:

- **Initial production from SD cards:** Sam fires `run-pipeline` via the UI or CLI; it runs through `ingest → stitch → audio_analysis` and pauses at `awaiting_crop_setup`. /produce picks up from there.
- **After crop:** Save crop-config (Sam does this in UI). /produce then uses `POST /api/episodes/<id>/resume-pipeline` with **explicit agents list** `["speaker_cut", "transcribe"]`.
- **After transcribe:** /produce dispatches the `clip-miner` subagent. Once it writes clips.json, /produce fires `resume-pipeline` with `["longform_render"]`.
- **After longform renders + Sam approves:** /produce POSTs `/approve-longform`, which now fires `["podcast_feed", "publish"]`. This publishes the LONGFORM only (shorts/ dir is empty so publish skips them). RSS updates trigger Spotify auto-ingest.
- **After YouTube returns the URL (async wait):** save to `episode.json.youtube_longform_url`. /produce dispatches `metadata-writer` subagent (writes metadata.json), then fires `resume-pipeline` with `["shorts_render", "thumbnail_gen", "qa"]`. `shorts_render` now unblocks because URL is set.
- **After shorts render + Sam approves clip/metadata review:** /produce POSTs `/approve-publish`, which now fires `["publish"]` only. The longform upload block in publish.py idempotent-skips (URL already set); shorts upload with youtube_first_comment funnel.
- **After publish:** backup approval as normal.

**Cost-safety gates (all three paid-API agents):**

- `agents/clip_miner.py` — hard-raises if `clips.json` missing AND `CASCADE_ALLOW_API_CLIP_MINER` ≠ "1"
- `agents/metadata_gen.py` — hard-raises if `metadata/metadata.json` missing AND `CASCADE_ALLOW_API_METADATA_GEN` ≠ "1"
- `agents/thumbnail_gen.py` — hard-raises if `thumbnail.png` missing AND `CASCADE_ALLOW_API_THUMBNAIL_GEN` ≠ "1"

Each refuses to consume API tokens unless explicitly opted in. The canonical path is to dispatch the corresponding subagent (clip-miner, metadata-writer — thumbnail subagent TBD) to produce the artifact first, then let the pipeline agent idempotent-skip.

`server/routes/chat.py` has been fully migrated to the claude CLI subprocess — no API consumption from the chat endpoint or the auto_trim action.

**CRITICAL — cost-safety rules for /produce orchestration:**

1. **NEVER call `POST /api/episodes/<id>/resume-pipeline` with an empty body.** The default is "run all remaining agents," which includes `clip_miner` / `metadata_gen` / `thumbnail_gen` — all three now hard-raise without their artifacts present. A blanket resume will fail fast, but it's wasteful — always pass an explicit `agents` list.

2. **Never fire a pipeline stage that requires a paid-API agent's output without first dispatching that agent's subagent counterpart.** The flow must be: transcribe → dispatch clip-miner subagent → longform_render → (publish longform + URL wait) → dispatch metadata-writer subagent (writes metadata.json) → shorts_render → (thumbnail_gen still needs a subagent path — for now, opt in manually with `CASCADE_ALLOW_API_THUMBNAIL_GEN=1` if you want it) → qa → review → publish shorts.

3. **Post-crop resume must be TWO steps, not one:**
   - Step A: `resume-pipeline` with `agents=["speaker_cut", "transcribe"]` ONLY.
   - Step B: After transcribe completes, dispatch `clip-miner` subagent (Agent tool), wait for `clips.json`.
   - Step C: `resume-pipeline` with `agents=["longform_render"]`.
   - Step D: After longform review + approval: `agents=["podcast_feed", "publish"]` (longform-only because shorts_render will gate on URL; fine).
   - Step E: Wait for YouTube URL, save to `episode.json.youtube_longform_url`.
   - Step F: `agents=["shorts_render", "metadata_gen", "thumbnail_gen", "qa"]`.
   - Step G: Clip review → `agents=["publish"]` (longform idempotent, shorts upload).
   - Step H: `approve-backup`.

4. **If the pipeline halts with `status: "error"` AND the thread lock is stale (resume-pipeline returns 409 but no work is happening):** `POST /cancel-pipeline` first, then resume with an explicit agent list. The cancel call can itself time out if the server is stuck — if it does, restart the server (see Server lifecycle section).

## Clip-mining subagent dispatch

**Why this matters:** The programmatic `clip_miner` agent uses the paid Anthropic API. Sam's Max subscription gives him generous Claude Code quota. Dispatching a `clip-miner` subagent via the Agent tool runs on that quota — effectively free per episode.

**When to dispatch:**
- Before the programmatic pipeline would run `clip_miner` (i.e. after `transcribe` completes).
- When Sam says "re-mine the clips" after a mid-episode cut or editorial change.
- When /produce is resuming an episode that has `diarized_transcript.json` but missing/stale `clips.json`.

**Dispatch call:**
```
Agent(
  subagent_type="clip-miner",
  description="Mine clips from <episode_id>",
  prompt="Mine clips for episode_dir=<absolute-path-to-episode-dir>. Use the criteria in your system prompt. Guest context: <guest_context or 'none yet'>. Existing longform_edits to respect: <json of longform_edits or 'none'>. Report back with the summary format."
)
```

**After subagent finishes:**
1. Verify `clips.json` and `episode_info.json` were written.
2. Read `episode_info.json` to get guest_name → **rename episode directory** to `ep_<date>_<slug>` where slug is a URL-safe version of guest_name. Update all paths in episode.json. Inform Sam.
3. Mark `clip_miner` as completed in `episode.json.pipeline.agents_completed`.
4. Trigger the next programmatic stage (longform_render) via `resume-pipeline`.

**Failure recovery:**
- Subagent crashes or returns empty clips.json → offer to retry or fall back to the programmatic `clip_miner` (which requires `ANTHROPIC_API_KEY` and costs money, so ask Sam first).
- Subagent returns clips that overlap a `longform_edits[type=cut]` range → reject, re-dispatch with stronger prompt emphasizing the cut.

**Token awareness:**
Clip-mining on a 90-min episode reads a ~30-40k-token transcript. Repeated re-mining on the same day burns Max-subscription quota. If Sam asks for > 3 re-mines in an hour, warn: "This is your Nth re-mine — want to batch your edits before I run clip-mining again?"

---

## Mid-episode cut flow (the strip-club case)

Example: Sam says "there's a bit where the guest talks about getting thrown out of a strip club for swiping a stripper's ass with a credit card — cut it."

1. Search `diarized_transcript.json` for relevant terms. Use several variants: "strip club", "credit card", "thrown out", "swiped". Look for multi-word matches within a 30-second window.
2. If a match found: present the matched window with 15s of context before/after, with clear timestamps. "Found it at 42:08-42:52. Here's what was said: [transcript snippet]. Cut this whole range?"
3. If NO match: "I couldn't find it automatically. Can you tell me roughly when in the episode it happens? Or paste a sentence you remember?"
4. On Sam's confirm: apply `edit_longform` with `type=cut`, `start_seconds=X`, `end_seconds=Y`, `reason=<sam's words>`. Persist the reason — it feeds the clip-miner subagent so it knows to avoid that range.
5. Ask: "More cuts before I re-render? Or run it now?"
6. On "run it": `rerender_longform`. If `clips.json` already exists (clip-miner already ran), also offer to re-mine clips so no short pulls from the cut range.
7. **Partial re-render optimization:** `longform_render` already supports segment-level resume. If the cut affects only segments N-M of K, only those need re-rendering. Today the agent's logic is "skip already-rendered seg files" — if the cut changes a segment's boundaries, that segment needs deletion first. Ensure this works before declaring the partial-rerender optimization in place (TODO).

---

## Clip combining flow

Example: "combine clips 3 and 4."

1. Read both clips' `start_seconds` and `end_seconds`.
2. Check the gap: `clip_4.start - clip_3.end`.
3. If gap < 5s: merge silently. New clip has `start = clip_3.start`, `end = clip_4.end`. Delete clip_4. Retitle via metadata-writer (both original hooks as context).
4. If gap 5-30s: confirm with Sam. "There's 18s between them. That 18s would be included — do you want it, or do you want me to stitch the two clean bits together (requires a new render)?"
5. If gap > 30s: warn and suggest a stitch-render instead.

---

## Mid-pipeline human critique loop

Sam will say things like "this audio sounds muddy" or "the color is off." Don't gaslight him — capture the critique and act.

1. Acknowledge in plain terms: "Got it — <paraphrase>."
2. Append to `episode.json.feedback[]` with timestamp + Sam's words. Over time this informs default config changes and future episodes.
3. Diagnose: run ffprobe / audio analysis / visual frame extraction as needed.
4. Propose a specific fix (e.g. "bump DFN mix from 0.5 to 0.65 and re-render audio_mix.wav") and confirm before applying.
5. If fix requires re-render: be explicit about cost in minutes, and offer to queue other edits first.

---

## Publish scheduling

The `publish` agent accepts a schedule or generates a default (1 short/weekday, 2/weekend, starting tomorrow morning).

Before firing publish:
1. Show Sam the effective schedule: "Clip 1 goes out tomorrow morning (Wed 9am PT), clip 2 Wed evening, clip 3 Thu morning..." Use the generated schedule from `metadata.json.schedule` or call `_schedule_to_datetime` via a read-only compute.
2. Offer 3 quick toggles: "slower (1/day)", "faster (2/weekday, 3/weekend)", or "custom (tell me)."
3. Persist Sam's choice on `episode.json.schedule_override` so future episodes default to it.

---

## Monitoring & recovery protocol

Claude Code is turn-based — I don't get push notifications when an agent crashes at 3am. To compensate, I spawn a **background watcher daemon** when the pipeline starts, and I read its events file as the FIRST action on every turn.

### Start the watcher
When any pipeline stage fires for an episode, immediately spawn:
```bash
bash .claude/scripts/produce-monitor.sh <episode_id> &  # run_in_background
```

It polls every 10 seconds and emits JSONL events to `/tmp/produce-<episode_id>-events.jsonl`:
- `start` — watcher launched
- `status-change` — episode.json status transitioned
- `stage-completed` — new entry in agents_completed
- `error` — new ERROR / Traceback / CRITICAL in `/tmp/cascade-server.log`
- `stage-stalled` — progress.json modified time > 2× stage's worst-case ETA
- `episode-dir-missing` — SSD unplugged mid-run

### Every turn, start here
Before responding to Sam's message, I:
1. Read `/tmp/produce-<id>-events.jsonl` tail (last 20 lines).
2. Diff against what I already told Sam about.
3. If anything new surfaced (error, pause state, completion) — lead with that, don't wait for him to ask.

### Detect an error
Signatures in the cascade log and episode files:
- `Traceback (most recent call last):` in `/tmp/cascade-server.log` — Python agent crashed
- `ERROR:` or `CRITICAL:` — explicit log error
- ffmpeg exit != 0 — encoding failure (read the last 500 chars of stderr from the agent's output JSON)
- `progress.json` mtime > 2× stage ETA — stalled
- `episode.json.status == "error"` — pipeline itself flagged

### Stop a run
- **Graceful:** `POST /api/episodes/<id>/cancel-pipeline` — pipeline checks cancellation between agents.
- **Force a stuck agent:** find the uvicorn worker PID via `lsof -iTCP:8420`, kill just the worker (not the parent — the parent's `--reload` spawns a new worker; killing the parent ends the server).
- **Nuclear:** `kill -9` all Python on 8420, then `nohup ./start.sh &` to restart.

### Change code mid-run
- Edit the file normally — uvicorn `--reload` picks it up.
- If the stuck agent is currently running: cancel pipeline → edit → resume.
- If the agent already completed: edit, invalidate downstream artifacts (see dependency map below), resume with the affected stages.

### Resume from the correct spot — dependency map

When I fix a bug in agent X, I delete specific artifacts and resume with a specific agent list so downstream doesn't short-circuit on stale data:

| Fix in | Delete these files | Resume with |
|---|---|---|
| `lib/audio_enhance.py` | `work/audio_mix.wav`, `longform.mp4` | `["longform_render"]` — segs stay (video-only), audio re-mixes, re-muxes |
| `agents/speaker_cut.py` | `segments.json`, `work/*channel*.npy`, everything downstream | `["speaker_cut", "transcribe", "longform_render"]` |
| `agents/transcribe.py` | `transcript.json`, `diarized_transcript.json`, `clips.json`, `shorts/*.mp4`, `metadata/` | `["transcribe"]` + re-dispatch clip-miner subagent |
| clip-miner subagent | `clips.json`, `episode_info.json`, `shorts/*.mp4`, `metadata/` | re-dispatch subagent, then `["shorts_render", "metadata_gen", "thumbnail_gen"]` (gated on URL) |
| `agents/longform_render.py` (video chain) | `work/longform_seg_*.mp4`, `longform.mp4`, `longform_render.json` | `["longform_render"]` |
| `agents/longform_render.py` (audio mux only) | `longform.mp4` (NOT segs) | `["longform_render"]` — segs skip, fresh mux |
| `agents/shorts_render.py` | `shorts/*.mp4`, `subtitles/*.srt` | `["shorts_render"]` |
| `agents/metadata_gen.py` | `metadata/metadata.json` | `["metadata_gen"]` |
| `agents/thumbnail_gen.py` | `thumbnails/*` | `["thumbnail_gen"]` |
| `agents/publish.py` | `publish.json` + clear `youtube_longform_url` if re-publishing longform | `["publish"]` |
| `agents/podcast_feed.py` | `feed.xml`, `podcast_audio.mp3`, `podcast_feed.json` | `["podcast_feed"]` |

**General rule:** each cascade agent is idempotent if its output artifacts are fresh. When in doubt about what to invalidate, ask before deleting.

### Server lifecycle
- **I own the server process.** Sam should never run `./start.sh` manually.
- **Start:** `nohup ./start.sh > /tmp/cascade-server.log 2>&1 & disown`
- **Health check:** `lsof -iTCP:8420 -sTCP:LISTEN` (listener present) AND `curl -sf http://localhost:8420/api/episodes/` (responds under 5s)
- **If port bound but unresponsive (like a zombied --reload parent):** kill -9 all uvicorn, relaunch.
- **If multiple listeners on 8420:** normal — uvicorn `--reload` uses parent + worker processes.

## Quality monitoring (run on every state transition)

- **Server up:** `lsof -iTCP:8420 -sTCP:LISTEN` non-empty
- **SSD mounted:** `/Volumes/1TB_SSD/cascade/` exists
- **Episode consistent:** `episode.json.status` matches side-state (e.g. clips.json exists iff clip_miner has completed)
- **Pipeline responsive:** `progress.json` modified in last 2× ETA of current stage
- **No orphan locks:** `.produce.lock` timestamp is fresh

When a check fails, alert Sam in plain language with a specific next step. Don't auto-fix unless it's clearly transient (network blip, one-shot ffmpeg hiccup) AND Sam pre-approved auto-retry for this episode.

---

## Rate-limit awareness (Max subscription)

Claude Code runs on Sam's Max subscription. Heavy /produce sessions can hit rate limits, especially when:
- Clip-mining on a long episode (30-40k tokens per dispatch)
- Dispatching metadata-writer once per clip × per-platform fields
- Running auto_trim plus several edit cycles

Soft budget per episode:
- Clip-mining: 1 dispatch nominal, up to 3 if edits cause re-mines
- Metadata-writer: 1 dispatch per clip + 1 for longform = ~11 total per episode
- Total subagent dispatches per episode: ~15 nominal

If rate-limited, tell Sam plainly: "Hit the Max hourly cap. I'll pause for 20 min and pick back up — nothing is lost."

---

## Subagents dispatched

- **`clip-miner`** (defined in `.claude/agents/clip-miner.md`) — reads transcript, produces clips.json + episode_info.json. Replaces API-driven agents/clip_miner.py.
- **`metadata-writer`** (TODO — build at `.claude/agents/metadata-writer.md`) — produces per-platform metadata in Sam's voice given guest context + clip info.
- **`performance-analyst`** (deferred v2) — analyzes historical publish.json + per-platform analytics to inform schedule + metadata style. Build once 2-3 episodes are published.

---

## Tools the skill uses

### Direct API (preferred for button-equivalent actions)
- `POST /api/episodes/<id>/crop-config` — save crop (user submits via UI; skill just waits)
- `POST /api/episodes/<id>/approve-longform` — resume into post-longform stages
- `POST /api/episodes/<id>/approve-publish` — run podcast_feed + publish (added 2026-04-21)
- `POST /api/episodes/<id>/approve-backup` — run backup
- `POST /api/episodes/<id>/resume-pipeline` — resume after crash or editorial change
- `GET /api/episodes/<id>` — current state

### Chat endpoint (for editorial actions — routes to existing action handlers)
- `POST /api/episodes/<id>/chat` with natural-language message. Backend parses into: update_clip_metadata, update_clip_times, reject_clip, add_clip, delete_clip, rerender_short, approve_clips, reject_clips, update_platform_metadata, update_longform_metadata, update_episode_info, edit_longform, rerender_longform, auto_trim. Full docs in `server/routes/chat.py`.

### File reads (source of truth)
- `episode.json` — status, speaker_count, audio_tracks, audio_sync, clips, longform_edits, feedback, guest_context, schedule_override
- `clips.json` — per-clip decisions
- `metadata/metadata.json` — per-platform metadata
- `qa/qa.json` — duration/sync warnings
- `diarized_transcript.json` — word-level timing (for cut timestamps, strip-club search)
- `publish.json` — per-platform submit results (now with error details after 2026-04-21)
- `progress.json` — current agent + progress fraction

---

## Report format (every turn Sam sees)

1. One-line summary of where we are.
2. What's next: either "pipeline running, I'll ping when it hits the next pause" OR "your move: <specific thing>."
3. Any warnings or surprises.

No file paths, no state codes, no commit hashes, unless Sam asks.

---

## Never do without asking

- Approve publish (destructive, posts to real accounts)
- Approve backup (clears SD card if configured)
- Reject a clip (editorial judgment)
- Retitle a clip without Sam's directional input
- Re-render the longform (expensive)
- Re-mine clips (costs Max-subscription quota)
- Change `config/config.toml` defaults (this is a separate workflow, not /produce)

---

## What /produce does NOT do

- Crop setup (must be visual / UI)
- Watching the longform for quality (that's Sam's ears and eyes)
- Writing replacement audio (Sam re-records or flags for research)
- Publishing without explicit confirmation
- Modifying the pipeline code itself (that's python-specialist)

---

## Concerns and limitations (known, documented, flag when relevant)

### Currently-flagged edge cases
- **Mid-cut after shorts already rendered:** re-mining clips via subagent is now cheap; prefer that over remapping short timestamps. Document in the Sam-facing report.
- **Clip combining with non-adjacent clips:** forces a multi-source render that `shorts_render` may not support today. If detected, dispatch python-specialist to check `shorts_render` for multi-source input, and flag if the code path doesn't exist.
- **Pipeline lock 409:** if an action returns 409 "pipeline already running," poll `GET /api/episodes/<id>` every 30s until status settles, then retry.
- **SSD unplugged mid-session:** detect missing `/Volumes/1TB_SSD/`, tell Sam to re-plug; no auto-reconnect.
- **Server crash mid-render:** a render can survive server restart if the Python child process persisted; if not, use `resume-pipeline`.

### Deferred
- Performance-analyst subagent (needs post-publish analytics data that doesn't exist yet)
- Audio-sync correction UI overhaul (this is a frontend rewrite, separate skill/project)
- Audio EQ quality research (separate `/autoresearch` task)
- Partial longform re-render on mid-cut (optimization, not correctness)
- Learning schedule defaults from Sam's `schedule_override` history

---

## TODO (to build during real episode runs)

- [ ] `metadata-writer` subagent at `.claude/agents/metadata-writer.md`
- [ ] `performance-analyst` subagent once post-publish data exists
- [ ] Verify 2-speaker dual-mono `pan=` fix is applied to real `audio_mix.wav` output
- [ ] Longform partial re-render after mid-episode cut (verify segment-resume handles boundary changes)
- [ ] Audit `shorts_render` for multi-source input (for clip-combining with non-adjacent clips)
- [ ] Frontend audio-sync correction UI overhaul (deferred; separate project)
