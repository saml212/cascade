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
independent from audio gating. Damaged speech must remain visible for review;
never conceal it with invented words.

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
