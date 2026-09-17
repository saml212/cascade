# Episode review/release consolidation evidence

This record covers the frontend consolidation based on aggregate-publication candidate
`5c6124b4ba925dc348e5d98098e7beb78875c5e4` and production
`222b1a709d7ce74142d20e9616accac91e836a1d`.

## Source reduction

The production selector is every `.ts` and `.css` file below `frontend/src`. Counts use Git
blobs for the two bases and filesystem bytes for this revision; tests, docs, dependencies, and
generated bundles are excluded.

| Revision | Production files | Production lines |
|---|---:|---:|
| Deployed `222b1a7` | 47 | 15,118 |
| Aggregate candidate `5c6124b` | 47 | 15,120 |
| Corrected consolidated revision | 41 | 13,943 |

The corrected consolidation removes 1,177 production lines from its frozen base and leaves
1,175 fewer lines than deployed. The main replacement is 1,830 lines: 1,692 in
`screens/episode/index.ts`, 111 in `lib/episode-release.ts`, and 27 in the shared coalesced
refresh helper. The extra lines beyond the frozen consolidation fix request ordering, media
lifetime, route disposal, and narrow layout findings found during independent review.

## Canonical reads and refresh cadence

| UI fact or control | Canonical source | Refresh behavior |
|---|---|---|
| Episode identity, pipeline, backup readiness, metadata baseline | `GET /api/episodes/{id}` | One request at mount and every four seconds. In-flight calls coalesce; an explicit post-save read queues one latest rerun. |
| Longform proof and selected-short readiness | `GET /api/episodes/{id}/review` | Loaded at mount and after a relevant write. |
| Quality findings and release gate | `GET /api/episodes/{id}/quality` | Loaded at mount and after a relevant write. Exactly one `QualityReview` owns findings, repair, preview, and decisions. |
| Selected/base audio, trim, video, and provenance | `GET /api/episodes/{id}/delivery` | Loaded at mount and after a relevant write; only a preparing video polls every two seconds. |
| Queue and provider evidence | `GET /api/schedule` | Loaded after local projections at mount and after publication approval. It never blocks local projections and is absent from four-second and two-second polling. |

Each projection has one active request and at most one coalesced rerun. A slow Schedule read
cannot delay review, quality, or delivery. Leaving the screen stops queued projection and
episode-detail work from issuing another request. Schedule currentness fix
`02380302a0e6eb6dac0a065c5db426d3cc9fb8cd` remains integrated; the frontend preserves both
`false` and unknown currentness values.

## Preserved writes and guards

| Control | API contract retained |
|---|---|
| Metadata | `PATCH /api/episodes/{id}` with only changed fields. All ten fields, unsaved drafts, edits made during an in-flight save, success, and visible error state remain. Success explicitly rereads episode, review, quality, and delivery before reporting completion. |
| Trim | `PUT /api/episodes/{id}/delivery/trim`; invalid and non-finite ranges are rejected before the request. |
| Prepare video | `POST /api/episodes/{id}/delivery/video/prepare`; delivery polls only while the video is preparing. |
| Quality decisions | Existing `QualityReview` APIs and revision bindings are unchanged. |
| Publication approval | `POST /api/episodes/{id}/approve-publish` with exactly `{ "start_publication": false }`; the surface exposes no aggregate publication execution. |
| Backup | `POST /api/episodes/{id}/approve-backup` only for canonical `awaiting_backup` and a case-insensitive exact `back it up` match. The six inventory rows, episode workspace, configured Seagate destination text, and duration remain visible. |

Source Setup, Longform Review, and Clip Review retain their specialist routes and APIs. Active
audio and video nodes keep the same host across projection updates, preserving playback state;
a changed artifact identity releases the prior media element before replacement. Metadata and
backup inputs remain mounted while their surrounding projections update.

## Route, lifetime, and responsive evidence

The six aliases `/episodes/:id`, `/audio`, `/delivery`, `/metadata`, `/publish`, and `/backup`
share one decoded episode identity. Alias changes focus the selected section without remounting.
Optional trailing slashes select the same section. `/longform` and `/clips` still mount the
specialist reviews. A different episode or specialist screen disposes the current effect scope.
If a first mount throws, that scope now disposes all effects and cleanups before the router
allows an alias retry.

At 430×900, native review measured a 430 px document width with no horizontal overflow. The
header is non-sticky below the small breakpoint, the 220 px title fit on one line in the fixture,
the primary action wrapped below it, and every section remained reachable. Desktop behavior
retains the sticky header and wide grids.

## Validation

- Frontend: 77/77 Node tests passed, including the exact Schedule call-order and polling policy.
- TypeScript and bundle: `npx tsc --noEmit` and Vite passed; 43 modules, JavaScript 185.76 kB (56.31 kB gzip), CSS 27.85 kB (6.72 kB gzip).
- Schedule integration: unchanged backend passed 21/21 focused Python tests in the frozen review.
- Native disposable fixture: exact ten-field PATCH/readback succeeded; partial PATCH returned 422; an eligible exact uppercase backup confirmation reached a visible 405 while backup work stayed blocked. Clone progress/publish/backup files and all protected production hashes stayed unchanged.
- Fixture boundary: output root, target episode directory, and `episode.json` must be regular non-symlinks resolving below the declared disposable root before the import-time status write. Negative import checks rejected all three symlink cases.
- Static review: specialist editors and APIs, clip approvals/history and actual media state, longform source-clock controls, stable drafts, Schedule/history, AgentPanel, and EventFeed remain reachable.

All implementation and validation used isolated worktrees and a disposable cloned episode. No
provider, production episode, installed configuration, or production process was written.
