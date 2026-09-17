# Native review fixture for Episode Review/Release

Native review uses fresh disposable targets prepared by the aggregate-publication harness. A
target contains clone-local JSON and APFS-cloned outputs plus a bounded set of production-backed
media symlinks for playback. The two adapters divide write coverage:

- The original approval adapter permits GET/HEAD and the exact approval-only publish POST.
- The supplemental metadata/backup adapter permits GET/HEAD and one exact ten-field episode
  PATCH. It forces only the copied episode to `awaiting_backup`; every POST, including backup,
  returns 405 before route execution.

Both run without provider credentials. Never reuse a target that has recorded its one approval.
The preparation script refuses to replace an existing directory.

## Prepare and bind a disposable target

```bash
AGGREGATE_EVIDENCE=/Volumes/1TB_SSD/cascade/release-ready/2026-09-14-approved-release/code-simplification-audit-2026-09-16/aggregate-publication-retirement-implementation-2026-09-17
AGGREGATE_WORKTREE=/private/tmp/cascade-aggregate-publication-retirement-sol
WORKTREE=/private/tmp/cascade-frontend-episode-release-fixes-sol
MAIN_REPO=/Volumes/1TB_SSD/root-disk-offload/2026-08-04/Local/Github/cascade
MAIN_VENV="$MAIN_REPO/.venv"
HARNESS=/Volumes/1TB_SSD/cascade/review-harnesses/frontend-episode-release-fresh

test "$(git -C "$AGGREGATE_WORKTREE" rev-parse HEAD)" = \
  5c6124b4ba925dc348e5d98098e7beb78875c5e4
test ! -e "$HARNESS"
"$MAIN_VENV/bin/python" "$AGGREGATE_EVIDENCE/ui-harness/prepare.py" \
  --worktree "$AGGREGATE_WORKTREE" "$HARNESS"
ln -sfn "$HARNESS/config.toml" "$WORKTREE/config/config.toml"
test -e "$WORKTREE/frontend/node_modules" || \
  ln -s "$MAIN_REPO/frontend/node_modules" "$WORKTREE/frontend/node_modules"
npm --prefix "$WORKTREE/frontend" run build
```

The preparation and immutable-state checks are documented in
`aggregate-publication-retirement-implementation-2026-09-17/UI-HARNESS.md`.

## Launch approval-only review

Follow that harness document to launch its unchanged `approval_harness_app` on port 18420. Use
this path to verify current publication evidence and the exact
`{ "start_publication": false }` write. All other writes must remain blocked.

## Launch metadata and backup-guard review

The supplemental adapter and machine-readable verification live in:

```text
/Volumes/1TB_SSD/cascade/release-ready/2026-09-14-approved-release/code-simplification-audit-2026-09-16/frontend-episode-release-consolidation-review-2026-09-17
```

Launch it only against a disposable target with the explicit write opt-in:

```bash
REVIEW_EVIDENCE=/Volumes/1TB_SSD/cascade/release-ready/2026-09-14-approved-release/code-simplification-audit-2026-09-16/frontend-episode-release-consolidation-review-2026-09-17
mkdir -p "$HARNESS/home/.config"
cd "$REVIEW_EVIDENCE"
env -i \
  PATH="$PATH" \
  HOME="$HARNESS/home" \
  XDG_CONFIG_HOME="$HARNESS/home/.config" \
  CASCADE_OUTPUT_DIR="$HARNESS/episodes" \
  CASCADE_BACKGROUND_ASSETS_DIR="/Users/samuellarson/Library/Application Support/Cascade/background-assets" \
  CASCADE_ENABLE_DISPOSABLE_FIXTURE_WRITES=1 \
  UPLOAD_POST_USER=up \
  PYTHONPATH="$WORKTREE:$REVIEW_EVIDENCE" \
  "$MAIN_VENV/bin/python" -m uvicorn metadata_backup_fixture_app:app \
    --host 127.0.0.1 --port 18421
```

The adapter exits before its import-time clone write unless the configured output root is
outside production, the exact opt-in is present, and the output root, episode directory, and
`episode.json` are non-symlinks with resolved containment. The episode PATCH route writes only
that checked `episode.json`. Backup POST remains blocked with 405.

## Review matrix

Open `http://127.0.0.1:18421/#/episodes/ep_2026-02-17_234937` at desktop width and 430×900.

1. Play audio and prepared video, seek and pause, then move through all six aliases. Playback
   nodes and draft inputs must stay mounted while projection reads update.
2. Edit all ten metadata fields and save. Confirm the PATCH succeeds, exact values read back,
   the header updates, and review/quality/delivery revision state refreshes immediately. A
   partial API PATCH must return 422.
3. In Backup, confirm six inventory rows, the `Episode workspace` path, target, and duration.
   The displayed paths are inert production configuration text; all fixture writes remain under
   the disposable target.
4. Enter partial confirmation text and verify the button is disabled. Enter `BACK IT UP`, verify
   it enables, and confirm its POST returns a visible 405 without creating backup state.
5. Verify `/audio/`, `/delivery/`, `/metadata/`, `/publish/`, and `/backup/` trailing-slash aliases
   focus their matching sections. Visit specialist source, longform, and clip screens and return.
6. At 430×900, confirm no horizontal overflow, the action wraps below the title, the header does
   not consume the scroll viewport, and every section remains reachable.
7. Observe cadence: episode detail reads at four seconds, delivery at two seconds only while
   video preparation is active, and Schedule only after local initial projections or publication
   approval.

Stop the server and compare protected hashes. The completed run is recorded in
`fixture-verification-final.json`; it includes exact ten-field readback, 422/405 guards, clone
sidecar stability, protected production equality, native responsive measurements, and symlink
boundary checks.

Remove ignored bindings when review is complete:

```bash
rm -f "$WORKTREE/config/config.toml" "$WORKTREE/frontend/node_modules"
```
