# Cascade frontend

Vanilla TypeScript + Vite + compiled Tailwind. No framework runtime. ~80-line
signals primitive, hash router, typed API client. Production bundle is ~25 KB
gzipped. See `docs/design-system.md` at the repo root for visual/component
decisions.

## Develop

```bash
cd frontend
npm install
npm run dev           # Vite dev server on http://localhost:8421
```

The dev server proxies `/api/*` and `/media/*` to the FastAPI backend on
`localhost:8420`, so run uvicorn in a separate shell:

```bash
# from repo root
./start.sh            # or: uv run uvicorn server.app:app --reload --port 8420
```

Open http://localhost:8421 for HMR-enabled development.

## Build for production

```bash
npm run build         # tsc --noEmit then Vite, outputs to frontend/dist/
```

`server/app.py` serves `frontend/dist/` by default. After `npm run build`,
refresh the browser at http://localhost:8420 and the new UI is live.

## Emergency rollback to the old UI

The previous vanilla-JS monolith lives at `../frontend-legacy/`. To serve it
instead of the new build, set the env var and restart uvicorn:

```bash
CASCADE_LEGACY_UI=1 ./start.sh
```

Unset the variable (or remove `frontend-legacy/`) to go back.

## Layout

```
src/
  main.ts                 # entry: routes + mounts shell
  lib/
    signals.ts            # reactive primitive (signal / effect)
    router.ts             # hash router
    api.ts                # typed fetch client, one fn per backend route
    format.ts             # timecode, duration, status → plain-English
    events.ts             # pipeline event stream (poll-backed, SSE-ready)
    dom.ts                # h() builder for HTML/SVG
  state/
    episodes.ts           # global episode list + per-episode detail polling
    ui.ts                 # agent-panel collapsed, toast
  components/
    Shell.ts              # three-column shell (nav / main / agent panel)
    NavRail.ts, AgentPanel.ts
    Button.ts, StatusPill.ts, ProgressBar.ts, EventFeed.ts
    icons.ts              # inlined 1.5px-stroke SVGs
  screens/
    dashboard.ts
    new-episode.ts
    episode/              # detail shell + per-section renderers
    crop-setup.ts         # full-width crop + speaker / track editor
    clip-review.ts        # editorial surface + chat dock
    longform-review.ts    # player + cut timeline + edit input
    publish.ts            # platform readiness + publish CTA
    backup.ts             # dangerous-action confirmation
    schedule.ts, analytics.ts, not-found.ts
  styles/
    index.css             # Tailwind + tokens (surface, ink, accent, speaker)
```

## Design tokens

CSS variables in `src/styles/index.css` — `--surface-canvas`, `--text-primary`,
`--accent`, `--speaker-1..4`, etc. Tailwind config exposes them as utilities
(`bg-canvas`, `text-ink-primary`, `bg-accent`, `bg-speaker-1`).

Display font: **Bricolage Grotesque** (variable). UI: **Satoshi**. Numeric /
code: **JetBrains Mono**.

## Adding a screen

1. Create `src/screens/my-screen.ts`. Export a `MyScreen(target, params)` fn.
2. Register the route in `src/main.ts`:
   `route('/my-path/:id', ({ id }) => MyScreen(main, id));`
3. Use components from `src/components/`; stick to tokenized classes
   (`bg-surface-1`, `text-ink-primary`, etc.).
4. For data fetching, use `api.*` from `src/lib/api.ts`. If you need a new
   endpoint, **stop** and flag it — the Pydantic models on the backend are
   the contract.

## Live event stream (forward-looking)

`src/lib/events.ts` exposes `subscribe(episodeId, onEvent)`. Today it's
poll-backed against `/api/episodes/:id/pipeline-status`. When the backend
adds an SSE endpoint (`/api/episodes/:id/events`), swap the implementation
in that one file — call sites stay the same.
