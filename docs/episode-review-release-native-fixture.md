# Native review fixture for Episode Review/Release

Create a fresh disposable target with the aggregate-publication harness, then run this candidate
through its fail-closed adapter. The target contains clone-local JSON and variant outputs plus a
bounded inventory of production-backed media symlinks for real playback. Its adapter permits
GET/HEAD and the exact approval-only POST; every other write is rejected before route execution.
This supports actual audio/video review, alias/history checks, unsaved metadata drafts, and
visible write-error checks without production, provider, or media writes.

The earlier `aggregate-publication-retirement-5c6124b-v2` target has already recorded its one
approval and cannot be reused. Prepare a new target name; the preparation script refuses to
replace an existing directory.

## Prepare a fresh copied episode

```bash
EVIDENCE=/Volumes/1TB_SSD/cascade/release-ready/2026-09-14-approved-release/code-simplification-audit-2026-09-16/aggregate-publication-retirement-implementation-2026-09-17
AGGREGATE_WORKTREE=/private/tmp/cascade-aggregate-publication-retirement-sol
MAIN_REPO=/Volumes/1TB_SSD/root-disk-offload/2026-08-04/Local/Github/cascade
MAIN_VENV="$MAIN_REPO/.venv"
HARNESS=/Volumes/1TB_SSD/cascade/review-harnesses/frontend-episode-release-ab0d4e0

test "$(git -C "$AGGREGATE_WORKTREE" rev-parse HEAD)" = \
  5c6124b4ba925dc348e5d98098e7beb78875c5e4
test ! -e "$HARNESS"
"$MAIN_VENV/bin/python" "$EVIDENCE/ui-harness/prepare.py" \
  --worktree "$AGGREGATE_WORKTREE" "$HARNESS"
```

Preparation reads the audited production episode, APFS-clones its episode tree, replaces the 14
review media inputs with exact production-backed symlinks, and rebases only clone-local approval
and currentness records. The resulting paths are:

```text
/Volumes/1TB_SSD/cascade/review-harnesses/frontend-episode-release-ab0d4e0
```

Its preparation and immutable-state evidence are documented in:

```text
/Volumes/1TB_SSD/cascade/release-ready/2026-09-14-approved-release/code-simplification-audit-2026-09-16/aggregate-publication-retirement-implementation-2026-09-17/UI-HARNESS.md
```

## Bind and build

```bash
WORKTREE=/private/tmp/cascade-frontend-episode-release-sol
MAIN_REPO=/Volumes/1TB_SSD/root-disk-offload/2026-08-04/Local/Github/cascade
HARNESS=/Volumes/1TB_SSD/cascade/review-harnesses/frontend-episode-release-ab0d4e0
EVIDENCE=/Volumes/1TB_SSD/cascade/release-ready/2026-09-14-approved-release/code-simplification-audit-2026-09-16/aggregate-publication-retirement-implementation-2026-09-17

git -C "$WORKTREE" merge-base --is-ancestor 8d1635a HEAD
test -f "$HARNESS/manifest.json"
ln -sfn "$HARNESS/config.toml" "$WORKTREE/config/config.toml"
test -e "$WORKTREE/frontend/node_modules" || \
  ln -s "$MAIN_REPO/frontend/node_modules" "$WORKTREE/frontend/node_modules"
npm --prefix "$WORKTREE/frontend" run build
```

## Launch through the fail-closed adapter

The original launcher is pinned to the earlier aggregate commit, so launch its unchanged adapter
directly with the candidate first on `PYTHONPATH`. The environment is cleared before start and
contains no provider key or token. The Cloudflare account ID is a non-secret release identity
needed by the read-only quality projection.

```bash
MAIN_VENV="$MAIN_REPO/.venv"
ACCOUNT_ID="$($MAIN_VENV/bin/python - <<'PY'
from dotenv import dotenv_values
print(dotenv_values('/Volumes/1TB_SSD/root-disk-offload/2026-08-04/Local/Github/cascade/.env')['CLOUDFLARE_ACCOUNT_ID'])
PY
)"

mkdir -p "$HARNESS/home/.config"
cd "$EVIDENCE/ui-harness"
env -i \
  PATH="$PATH" \
  HOME="$HARNESS/home" \
  XDG_CONFIG_HOME="$HARNESS/home/.config" \
  CASCADE_OUTPUT_DIR="$HARNESS/episodes" \
  CASCADE_BACKGROUND_ASSETS_DIR="/Users/samuellarson/Library/Application Support/Cascade/background-assets" \
  UPLOAD_POST_USER=up \
  CLOUDFLARE_ACCOUNT_ID="$ACCOUNT_ID" \
  PYTHONPATH="$WORKTREE:$EVIDENCE/ui-harness" \
  "$MAIN_VENV/bin/python" -m uvicorn approval_harness_app:app \
    --host 127.0.0.1 --port 18420
```

The adapter refuses production output roots, validates the copied-state manifest and input links,
and verifies that its release gate is approval-ready before opening the socket.

## Review matrix

Open `http://127.0.0.1:18420/#/episodes/ep_2026-02-17_234937` and review at desktop
and narrow widths.

1. Play the full-length audio and prepared video; seek, pause, resume, and confirm provenance and currentness copy.
2. Navigate through Review, Audio, Release files, Episode copy, Publication, and Backup. Use browser back/forward and confirm playback position, metadata drafts, QualityReview state, and delivery polling do not restart on aliases.
3. Enter unsaved text in several metadata fields, move among aliases, and confirm all ten values remain. Saving should return the adapter's visible 405 error and retain the draft for retry.
4. Confirm the release facts keep longform proof, selected-short readiness, and Schedule queue/provider evidence separate. Inspect any `artifact_current: false` warning as supplied by Schedule.
5. Enter partial backup text, confirm the action stays disabled, then enter `BACK IT UP`. The enabled POST should return the adapter's visible 405 error and must not start backup work.
6. Open `/episodes/ep_2026-02-17_234937/longform` and `/episodes/ep_2026-02-17_234937/clips`; confirm they mount the existing specialist reviews. Returning to the episode route should create a fresh episode screen as designed.
7. Confirm Source Setup, Longform Review, Clip Review, Schedule/history, AgentPanel, and EventFeed remain reachable and the browser console has no errors.
8. Review Publication last. The adapter permits the exact `{ "start_publication": false }` approval once and writes only cloned `episode.json`; follow the existing harness document to verify receipts and progress remain byte-identical.

Stop the server before verification. Remove the ignored config binding when native review is done:

```bash
rm -f "$WORKTREE/config/config.toml"
```

Keep the worktree commit unchanged throughout review and record its exact final SHA with the
native decision.
