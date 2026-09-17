# Episode review/release consolidation evidence

This record covers the frontend consolidation based on frozen aggregate-publication candidate
`5c6124b4ba925dc348e5d98098e7beb78875c5e4` and production
`222b1a709d7ce74142d20e9616accac91e836a1d`.

## Source reduction

The production selector is every `.ts` and `.css` file below `frontend/src`. Tests,
documentation, generated bundles, and dependency files are excluded. Counts use Git blobs so
ignored build output cannot affect them.

| Revision | Production files | Production lines |
|---|---:|---:|
| Deployed `222b1a7` | 47 | 15,118 |
| Frozen aggregate candidate `5c6124b` | 47 | 15,120 |
| Consolidated code through `8d1635a` | 40 | 13,823 |

The consolidation removes 1,297 production lines from its frozen base and leaves 1,295 fewer
production lines than deployed `222b1a7`. The audited nine-file surface was 3,072 lines at
`222b1a7` and 3,074 lines at `5c6124b`. Its replacement is 1,740 lines: 1,630 in
`screens/episode/index.ts` and 110 in `lib/episode-release.ts`. Shared router and route-table
support accounts for a net 37 lines outside that replacement.

The deleted destination-color assertion belonged only to the removed Publish layout. The clip
metadata destination-field test remains. That test change is excluded from every production
line claim above.

## Canonical read projections

| UI fact or control | Canonical source | Presentation rule |
|---|---|---|
| Episode identity, pipeline, backup readiness, metadata baseline | `GET /api/episodes/{id}` | The existing episode-detail poll remains the authority. |
| Longform proof and approval | `GET /api/episodes/{id}/review` | Uses `longform.canonical_render` and `longform.approval`; it does not derive proof from episode status. |
| Selected-short readiness | `GET /api/episodes/{id}/review` | Counts only selected clips for which `clipDistributionReady` validates the chosen version and current approval. |
| Quality findings and release gate | `GET /api/episodes/{id}/quality` | Exactly one full `QualityReview` owns finding, repair, preview, and decision controls. Publication blockers come from `release_gate.blockers`. |
| Selected/base audio and provenance | `GET /api/episodes/{id}/delivery` | Plays `selected_audio_download_url` when present, otherwise labels the historical output explicitly. Source-clock and currentness wording follows delivery provenance. |
| Trim, prepared video, metrics, and download | `GET /api/episodes/{id}/delivery` | Stateful media and trim controls are retained while their artifact identity is unchanged. |
| Queue and provider evidence | `GET /api/schedule` | Items and evidence are filtered only by exact `episode_id`. States, URLs, errors, and `artifact_current` are displayed from the API without recomputation. |

Schedule currentness fix `02380302a0e6eb6dac0a065c5db426d3cc9fb8cd` was integrated as
`916f092`. The frontend preserves both `false` and unknown values; it never substitutes the
episode status, a receipt count, or the globally selected gameplay variant.

## Preserved writes and guards

| Control | Existing API contract retained |
|---|---|
| Metadata | `PATCH /api/episodes/{id}` with only changed fields from the saved baseline. All ten fields, unsaved state, edits made during an in-flight save, success, and visible error state remain. |
| Trim | `PUT /api/episodes/{id}/delivery/trim`; invalid and non-finite ranges are rejected before the request. |
| Prepare video | `POST /api/episodes/{id}/delivery/video/prepare`; delivery polling continues only while the returned state is preparing. |
| Quality decisions | Existing `QualityReview` APIs and revision bindings are unchanged. |
| Publication approval | `POST /api/episodes/{id}/approve-publish` with exactly `{ "start_publication": false }`. The screen exposes no aggregate publication execution. |
| Backup | `POST /api/episodes/{id}/approve-backup` only after the canonical awaiting-backup gate and a case-insensitive exact `back it up` match. The input remains mounted while typing and resets after success. |

The six backup rows, exact SSD source, exact Seagate target, and duration remain visible. Source
Setup, Longform Review, and Clip Review keep their existing routes and APIs.

## Route and lifetime evidence

The following aliases share `episode:{decoded-id}` and update the focused section without
remounting the episode screen:

- `/episodes/:id`
- `/episodes/:id/audio`
- `/episodes/:id/delivery`
- `/episodes/:id/metadata`
- `/episodes/:id/publish`
- `/episodes/:id/backup`

`/episodes/:id/longform` mounts the existing Longform Review and
`/episodes/:id/clips` mounts the existing Clip Review. A different episode, a specialist screen,
or another top-level screen disposes the current effect scope. Shared identity is committed only
after a successful mount, so an initial handler failure can be retried through another alias.

The router tests cover all aliases, simulated browser history, decoded IDs, different-episode
disposal, both specialist handoffs, and first-mount failure followed by retry. The single mount
also proves that alias changes do not recreate projection requests, the delivery timer, metadata
draft state, or stable-control registries.

## Validation

- Frontend: 70/70 Node tests passed after removal of the obsolete layout-only assertion.
- TypeScript: `npx tsc --noEmit` passed.
- Production bundle: Vite built 42 modules; JavaScript 184.62 kB (55.82 kB gzip), CSS 27.75 kB (6.70 kB gzip).
- Schedule integration: `tests/test_routes_schedule.py` passed 21/21 with the installed project virtual environment.
- `/clean`: staged static checks, diff checks, and the manual touched-file audit passed before every implementation commit.

All implementation and validation work used the isolated worktree. No production episode,
provider, media, installed configuration, or running production process was written.
