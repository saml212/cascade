# Recovery and release workflow

This document describes the current maintainer workflow for recovering an
episode and producing local files safely. It does not authorize publishing.

## Start the application

Run `./start.sh` from the repository root. The script:

1. verifies `uv`, Node.js, npm, and an ffmpeg build with the `ass` filter;
2. preserves an unusable `.venv`, creates a Python 3.12 environment when
   needed, and installs the lean runtime requirements;
3. builds the canonical TypeScript/Vite frontend and starts FastAPI on
   `127.0.0.1:8420`.

Optional DeepFilterNet dependencies are separate:

```bash
uv pip install --python .venv/bin/python -r requirements-restoration.txt
```

There is one frontend: `frontend/src` builds to `frontend/dist`, which FastAPI
serves. Do not restore a fallback UI or edit generated files in `frontend/dist`
by hand.

## Three-step UI workflow

1. **Picture and sound** imports and stitches the source, confirms crops,
   assigns logical microphone tracks, and checks sync. A new episode initially
   runs only ingest, stitch, and audio analysis so paid transcription and
   rendering do not begin before this review.
2. **Episode details** saves titles, descriptions, guest details, tags, and
   platform links. Saves are minimal PATCH requests: editing a title cannot
   overwrite a URL written concurrently by a background job. Typing during an
   active save remains visibly unsaved.
3. **Prepare for upload** chooses the release range and builds verified local
   podcast audio and upload video. Delivery status is fingerprinted against the
   source, trims, crop and volume settings, canonical audio, and relevant
   configuration. The UI says **Ready for upload** only when both verified
   artifacts are current. Audio-only readiness never implies video readiness.

Preparation creates local downloads. Publishing remains a separate explicit
action with its own approval flow.

## Production contract

The local delivery files are recovery outputs, not the complete release
package. A release package contains a fresh speaker-cut full episode, selected
speaker-aware vertical clips, captions, thumbnails, platform copy, automated QA,
and a proposed schedule. An episode cannot be called ready when required clips
are absent or when its full video is only the continuous-wide recovery export.

Speaker names, visible seats, diarization labels, and recorder channel numbers
are separate evidence. A recorder channel identifies a microphone, not a person.
Use every recorder session, preserve overlapping speech, and keep shot changes
independent from audio gating. A microphone dropout makes the surviving channel
look dominant, so channel level within a digital-zero or near-zero finding does
not establish speaker identity. Quarantine the turn unless transcript semantics
or independent, unconfounded evidence supports it. Damaged speech must remain
visible for review; never conceal it with invented words.

Local rendering and review must not depend on a published YouTube URL. The API
must expose machine-readable manifests, source and output metadata, bounded
audio/video previews, QA results, review decisions, and artifact downloads.
Humans and agents may use the UI for visual inspection, but every durable action
and state transition belongs behind an API. Editorial approval and permission
to publish are separate revision-bound decisions. No publishing is authorized
by this workflow.

## Production roadmap

1. Reproduce and diagnose Christopher Reynolds's reported speech dropouts from
   original media through every derived audio artifact. Establish one artifact
   manifest and remove the publish-first dependency from local short rendering.
2. Prove one full package with a current speaker-cut master and three strong
   shorts. Arnold Gray is a useful fast editorial pilot because prior candidates
   exist; his older `longform.mp4` is reference material, not a release master.
   Christopher remains the first fresh audio and speaker-attribution proof.
3. Apply the approved style to Christopher, Arnold, and Ty Simpson-Kane / Garrett
   Schmidt. Refresh selections from current transcripts and produce all review
   assets together; six to ten clips per episode is a starting editorial target,
   not a quota.
4. Replace process-local workers and the schedule proposal with a footage-drop
   watcher, durable jobs, restart recovery, platform acknowledgements, and
   duplicate-safe distribution. Persist request IDs before polling and confirmed
   destination URLs after completion.
5. Add a daily producer/community loop backed by actual comments, post status,
   account health, and performance metrics. Record metric freshness and small,
   traceable editorial experiments. Public replies and outreach require their
   own authorization.

The implementation should keep reusable roles small: production coordinator,
audio/video editor, clip editor, publisher, and community/growth editor. During
this implementation effort, assign coding and verification to GPT-5.6 Sol
subagents at ultra effort, checkpoint small local commits, and have the root
agent exercise both the API and the actual UI before calling a slice complete.

Upload-Post exposes upload, scheduling, analytics, and comment APIs, subject to
the connected accounts: <https://docs.upload-post.com/api/reference/>. Spotify
video delivery depends on the show's actual hosting path:
<https://support.spotify.com/us/creators/article/video-episodes-for-shows-not-hosted-with-spotify/>.
Choose and verify those account paths before enabling distribution.

## Recover incorrect source ordering

Keep original camera media mounted or available in the archive. First inspect
the proposed order without changing the episode:

```bash
.venv/bin/python scripts/repair_source_order.py \
  --episode /path/to/episodes/EPISODE_ID \
  --staging-dir /path/with/enough/free/space \
  --order 'FIRST.MP4,SECOND.MP4,LAST.MP4'
```

Add `--apply` only after reviewing the complete map. The repair stages and
validates a full replacement before it removes the generated merged file.
Original camera files are never removed. The validated staging copy remains as
a recovery copy.

If the existing transcript was produced from the same unchanged camera chunks,
remap it without another ASR request:

```bash
.venv/bin/python scripts/remap_transcript_order.py \
  --episode /path/to/episodes/EPISODE_ID \
  --old-order 'FIRST.MP4,LAST.MP4,SECOND.MP4'
```

The command is also plan-only until `--apply` is added. It remaps word and
utterance timestamps, splits utterances crossing a moved seam, rebuilds SRT and
paragraph ordering, preserves speaker identifiers, and backs up the previous
transcript. It refuses to apply when downstream timestamp-based artifacts are
present; regenerate those artifacts after the source timeline is corrected.

## Playback and state guarantees

- Logical microphone previews concatenate every recorder segment for a track
  and apply the saved sync offset once, matching export behavior.
- Polling updates status without rebuilding active media controls or discarding
  unsaved editor fields.
- A single-file merged-source symlink keeps its backing source instead of being
  deleted during cleanup.
- The Longform tab prefers the current verified delivery video. An older render
  remains available but is labeled **Earlier render**.
- Dashboard and episode status pills use verified delivery state for display;
  pipeline actions continue to use the raw backend state.

## Revision-bound transcript corrections

Read the current correction document before proposing an edit:

```text
GET /api/episodes/{episode_id}/inspection/transcript/corrections
```

The response supplies `transcript_revision`, the source-clock correction
document, and its existing operations. Use stable operation IDs and preserve
those existing operations. Support each `replace_word` or `replace_range` with
current word IDs, times, speaker evidence, and a reason. A louder surviving mic
inside a known dropout is confounded evidence and must not drive a correction.

Before writing, apply the proposed operation set to a temporary copy of the
current canonical transcript and segments, run the canonical regrouping and
speaker alignment, and record the predicted shot boundaries. Keep ambiguous
turns outside the apply set as review evidence. Then send:

```text
POST /api/episodes/{episode_id}/inspection/transcript/corrections
{"expected_revision":"sha256:...","operations":[...]}
```

The endpoint upserts by operation ID, locally rebuilds the canonical transcript
and shot plan without a new ASR request, and rolls back on failure. A `409`
means the source revision changed; reread and rebase instead of forcing the
write. After success, reread the transcript and shot-plan endpoints and compare
them with the dry run. A speaker-label-only correction must leave the selected
audio fingerprint and selected audio bytes unchanged. Let render fingerprints
identify which videos need a new shot pass; do not regenerate audio solely
because speaker labels changed.

## Audio repair and exact output proof

Repair only a current, manifest-backed output:

```text
POST /api/episodes/{episode_id}/delivery/video/repair-audio
POST /api/episodes/{episode_id}/clips/{clip_id}/repair-audio
```

The longform operation is asynchronous; poll the delivery and pipeline status
endpoints. A successful repair stages a replacement, normalizes canonical audio,
copies the reviewed H.264 stream, validates the completed file, and installs it
atomically. Do not infer completion from the initial response. Require the
current render record to contain:

- `output.audio_loudness.verification.safe: true` with measured LUFS and true
  peak inside the configured policy;
- `provenance.audio_remaster.video_copy_verification.status: pass` with equal
  input/output packet SHA-256 values and packet counts;
- `output.size_bytes` and `output.mtime_ns` matching the exact reviewable file.

Inspect the exact output through `/inspection/frame` or `/inspection/preview`
using `target=longform` or `target=short`, the correct `source` or `output`
clock, and `clip_id` for shorts. The replacement has a new output revision and
invalidates prior editorial or clip approval even though its video packets are
unchanged.

For a review-required audio finding, use its `review.inspection_request` from
`GET /api/episodes/{episode_id}/quality` or `/audio-qc` to fetch the exact
longform preview. Record a local decision only with:

```text
POST /api/episodes/{episode_id}/audio-qc/findings/{finding_id}/review
{"decision":"accepted","reviewer":"...","evidence_note":"...","expected_report_fingerprint":"...","expected_finding_fingerprint":"...","expected_output_revision":"..."}
```

The three expected revisions bind the decision to the report, finding, and
rendered output. This review does not approve the episode or authorize
publication.

## Select a short version for distribution

Preview and approve **Base** and **Motion background** independently. Approval
does not select a version, and selection does not approve it. A clip with no
saved selection uses Base.

Read `review.distribution` from
`GET /api/episodes/{episode_id}/review`, then select with:

```text
PUT /api/episodes/{episode_id}/clips/{clip_id}/distribution
{"variant_id":null,"expected_revision":"sha256:..."}
PUT /api/episodes/{episode_id}/clips/{clip_id}/distribution
{"variant_id":"background_motion_v1","expected_revision":"sha256:..."}
```

The selected file must be current and separately approved. Its exact media,
copy, and approval revision become part of the release identity and receipt.
A `409` means the selection or approval is stale; reread the review response
instead of forcing the write.

After any recorded submission, `change_locked` prevents switching that clip's
version. Existing Base receipts remain Base and are never treated as Motion
approval. A clip with prior receipts can be prepared for an explicit re-release
only after every requested destination is proven published, definitively failed,
or cancelled. First call
`POST /api/episodes/{episode_id}/check-upload-urls`. Cascade checks the exact
saved request or job ID against Upload-Post status and history and persists
terminal per-destination evidence. Submitted, scheduled, pending, queued,
inbox-only, unknown, incomplete, or identity-less records remain blocked; they
need provider reconciliation before a re-release can be prepared.

For a future Upload-Post job that is still uniformly queued, first preview the
exact cancellation:

```text
POST /api/episodes/{episode_id}/clips/{clip_id}/schedule-cancellation/preview
{"variant_id":"background_motion_v1","expected_revision":"sha256:...","request_id":"<new UUID>","actor":"...","reason":"..."}
```

Review the returned local receipt, provider job, profile, date, platforms, and
target. Send the response's `execute` body unchanged to the same path without
`/preview`. Cascade writes the request before the provider `DELETE`, retains the
original receipt, and marks it cancelled only after an exact successful delete,
calendar absence, and exact status/history evidence. If the response is
ambiguous, do not issue another delete; reconcile the stored operation. On
success, send the returned `next.body` to the returned re-release path.

When `review.distribution.re_release_allowed` is true, prepare the exact approved
target with:

```text
POST /api/episodes/{episode_id}/clips/{clip_id}/re-release
{"variant_id":"background_motion_v1","expected_revision":"sha256:...","request_id":"<new UUID>","actor":"...","reason":"..."}
```

Use `variant_id: null` to release Base again. Keep the same request ID when
retrying the same operation. The request binds the actor, reason, selected media
and copy revision, render fingerprint, and prior receipt-history fingerprint.
It creates a new release identity and invalidates publish approval, so review and
approve the new release plan before running publication. Prior receipts stay in
`publish.json`; the new receipt records its parent history and request ID.
Unknown or stale targets, changed receipt history, malformed state, and a second
unconsumed request fail with `409`. Never delete or rewrite receipt history to
bypass these checks.

Some pre-schema receipts cannot be reconciled because they have no saved media
or destination identity. The review response exposes each such receipt under
`unresolved_receipt_obligations`, labels an absent artifact identity as
`unknown`, and returns `re_release_history_revision`. If
`unresolved_history_acknowledgement_allowed` is true, an operator may prepare
only the current separately approved Motion replacement by adding:

```text
"acknowledge_unresolved_history_revision":"sha256:..."
```

Use the exact revision returned by the review response. This records the old
receipts as unresolved obligations inside the new release identity; it does not
mark them failed, cancelled, or published. Any change to the receipt history
invalidates the authorization. After fresh QA and publish approval, publication
requires an explicit reviewed destination subset and creates new receipts that
retain the acknowledgement and parent-history fingerprint. The empty publish
body remains blocked for this path.

## Publish an approved destination subset

Keep the global platform plan intact when an account is temporarily unavailable.
Give each selected clip an explicit future `publish_schedule`, approve the new
release revision, then preview the requested subset:

```text
POST /api/episodes/{episode_id}/publish-shorts/preview
{"destinations":["youtube","tiktok"],"clip_ids":["clip_01"],"request_id":"<new UUID>","actor":"...","reason":"...","expected_release_revision":"sha256:..."}
```

Review the exact artifact, transformed copy, profile, and date. Send the returned
`execute` object unchanged as `publish` in
`POST /api/episodes/{episode_id}/run-agent/publish`. Cascade saves an
`intent_recorded` short receipt before the first remote request. The final receipt
keeps the request identity and lists Instagram/X as deferred.

Use a new UUID and only the deferred destinations later. A disjoint wave may
share its own still-future clip date; a past date requires a new schedule and
fresh publish approval. An overlapping destination for the same selected
artifact is rejected across later episode revisions. The legacy empty publish
body is also rejected after a subset exists. Hashtags are normalized at send
time. YouTube/TikTok copy names the show and includes a copyable exact episode
hub URL without claiming caption links are clickable. X names the show without
asserting an unverified bio link.

## Verification

Run the Python suite and frontend checks before handing a recovery back for
release review:

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/pytest -q
cd frontend
node --test tests/*.test.mjs
npm run build
```

For media recovery, also inspect the repair plan, verify source file sizes, and
use `ffprobe` to confirm the rebuilt duration and primary audio/video streams.
Do not infer correctness from filename timestamps alone; camera segment order
and content continuity are the deciding evidence.
