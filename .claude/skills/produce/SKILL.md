---
name: produce
description: Operate a Cascade podcast episode through local ingest, evidence review, editing, rendering, QA, release preparation, and separately authorized publishing. Use for producing, recovering, inspecting, continuing, or reporting the status of a Cascade episode.
---

# Cascade producer

Drive the work through Cascade's FastAPI service. The UI is a useful human media-review surface; it is not the source of durable state. Prefer API reads and writes so another agent can inspect and continue the same episode. Read [`docs/recovery-workflow.md`](../../../docs/recovery-workflow.md) for maintained setup, recovery, and verification details rather than copying those procedures here.

## Establish current state

Use `GET /openapi.json` for the live contract. Resolve the episode with `GET /api/episodes/`, then read these views before acting:

- `GET /api/episodes/{id}/review` for playable files, exact render freshness, clip selection, metadata completeness, and revision-bound approvals.
- `GET /api/episodes/{id}/quality` for current QA, findings, release blockers, and audio repair state.
- `GET /api/episodes/{id}/pipeline-status` for active work, progress, and errors.
- `GET /api/episodes/{id}/delivery` for local audio/video preparation and verified downloads.

Do not infer readiness from `episode.json.status`, filenames, file existence, or a green UI label. A reviewable older file may remain playable while its `current` or release state is false.

If the API is unavailable and no render is active, start it with `./start.sh`. Inspect the existing process and logs before restarting it. Never kill every `uvicorn` process indiscriminately.

## Work autonomously with evidence

Complete reversible local work already authorized by the user's production request. Find trim points, defects, clip boundaries, and crop choices from the transcript and media; do not make the user supply timestamps you can discover.

Use bounded inspection endpoints instead of arbitrary filesystem paths:

- `/inspection/transcript` and `/inspection/shot-plan` expose current source-clock evidence.
- `/inspection/frame` and `/inspection/preview` inspect source, longform, or short media in an explicit `source` or `output` clock.
- `/inspection/audio-preview` compares bounded recorder or camera inputs.
- `/audio-qc/findings/{finding_id}/preview` and `/audio-qc/repair-plan/{entry_id}/preview` expose original and grounded fallback evidence.

Sample actual frames across crop transitions, speaker changes, overlaps, and captions. An agent may save evidence-backed crop settings through `/crop-config`; the UI lets the user review or override them. Keep source timestamps and edited/output timestamps labeled separately.

Preserve original recordings and prior reviewable exports. Stage replacements, verify duration, streams, fingerprints, and representative media, then publish them atomically. Never conceal damaged speech with generated words or synthetic audio. Report objective loudness, ASR, waveform, correlation, and timing evidence accurately; do not claim perceptual listening when none occurred.

## Run only the needed work

Use `POST /api/episodes/{id}/run-agent/{agent_name}` for one stage or `/resume-pipeline` with an explicit `agents` list. Poll `/pipeline-status` or its event stream for asynchronous work. Treat `409` and `422` responses as current-state or validation evidence and resolve the stated dependency; do not create a second job system or edit status files to bypass a gate.

After picture, microphone mapping, sync, trims, or metadata changes, rerun only the stages whose input fingerprints became stale. Before expensive renders, inspect the server's storage preflight and preserve its configured free-space reserve.

For source-channel continuity:

1. Run `qa`, then read `/audio-qc` even if the run returns a failed-quality response; the report is still persisted.
2. Review retained probable findings before removed or heuristic findings.
3. When grounded repair is supported, create a repair plan and candidate, inspect bounded previews and the full candidate, then select it through `/audio-qc/repair-candidate/select`.
4. Rerun QA after selection so output proof binds the exact selected bytes. Selection does not make unresolved findings safe and does not approve release.

## Review local media before publication

Prepare and inspect the complete local package before any public action:

1. Prepare verified delivery audio and the current speaker-cut longform through `/delivery/prepare` and `/delivery/video/prepare`.
2. Select clip candidates with `/clips/{clip_id}/select`, render locally with `/clips/{clip_id}/render`, and inspect the exact rendered short, captions, bounds, crop, and enabled-platform copy.
3. Approve a clip only after its current render exists. Candidate selection is not final approval; changing pixels, timing, or copy invalidates the prior approval revision.
4. Run QA against current audio, longform, shorts, thumbnails, and metadata. Missing, stale, failed, or review-required evidence remains visible as a blocker.
5. Record longform editorial approval with `/approve-longform` only after the current rendered revision has been reviewed. This is separate from permission to publish.

Local short rendering must not depend on a public YouTube URL. Do not publish a longform merely to unlock short production.

## External actions

Publishing and destructive backup/SD cleanup require explicit authorization in the current user conversation. A request to produce, repair, render, review, or select a candidate does not authorize public posting. Before an authorized publish action, reread `/review` and `/quality`; require current render and copy revisions, approved rendered clips, and the current QA gate. Use `/approve-publish` only for that explicit action, then record platform acknowledgements and returned URLs without treating submission as confirmed publication.

Do not create recurring producer or community jobs unless the user explicitly requests an automation. Public replies, outreach, scheduling, analytics experiments, and account changes each stay within their separately authorized scope.

When reporting progress, lead with the episode's actual current state, what changed, the evidence that supports it, and the next unresolved blocker. Give the user a concrete review action only when human judgment is truly needed.
