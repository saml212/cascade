# Cascade Design System

**Status:** Proposal, awaiting Sam's sign-off.
**Scope:** Frontend redesign — visual language, component vocabulary, and technical choices for the cascade UI.

This document is the canonical reference. Every pixel and interaction in the redesigned frontend should trace back to something here. If the doc says it, the UI says it. If the doc doesn't cover it, add a section first, then build.

---

## 1. The one-sentence brief

Cascade is the cockpit for a serious podcast creator — the tool between a shoot and a published episode. It should feel like **Linear's discipline crossed with a broadcast editor's control room**: dark, warm, editorial, unmistakably a creator's instrument.

---

## 2. Aesthetic commitments

### Tone

**Editorial, warm, confident, precise.** Not a SaaS dashboard. Not a neon developer tool. Closer to *The New Yorker* laid out by a broadcast engineer. The UI should feel like something a creator is proud to keep open on a second monitor.

### Three non-negotiables

1. **Dark-first, warm-dark.** Pure black is cold and industrial. Cascade's canvas is a warm near-black that reads like dimmed studio paper — `#0B0B0C` base, not `#000`.
2. **Status is the loudest element.** On any screen, the single most legible thing at a glance is *what stage this episode is in and what is blocking it.* Status gets color, motion, and size before anything else.
3. **Typography carries the personality.** Iconography is restrained and monochromatic. What makes cascade *feel* like cascade is the type system — an editorial serif for section headings and empty states, a characterful sans for the UI, a warm mono for coordinates and timecodes.

### Explicit non-goals

- No neon / cyberpunk / synthwave.
- No glassmorphism or heavy frosted blur as decoration.
- No purple-to-blue gradients. No "AI sparkles" iconography. No gradient meshes.
- No rainbow speaker colors. A curated 4-speaker palette, chosen.
- No emoji in product UI. Icons are custom or Lucide (monochromatic, 1.5px stroke).
- No marketing-esque page-load animations on working screens. Motion is for **state changes**, not decoration.

---

## 3. Typography

Three faces, each chosen for a specific job.

| Role | Family | Why |
|------|--------|-----|
| **Display / editorial** | **Instrument Serif** | Variable serif with genuine personality. Used for section titles ("Today's episode"), empty states, celebratory moments, and the marquee headline on the dashboard. Magazine feel; immediately signals "this is a tool for a creator, not a compliance dashboard." Free via Google Fonts. |
| **Body / UI** | **Satoshi** | Characterful modern sans — tighter than Inter, warmer than Geist, with distinctive lowercase `a` and `g`. Used for every button, label, table, and body paragraph. Free via Fontshare. |
| **Mono / numeric** | **JetBrains Mono** | Warm-leaning monospace with excellent tabular digits. Used for timecodes, clip durations, pixel coordinates, IDs, and any column of numbers. Free via Google Fonts. |

### Type scale

All sizes use `rem`, anchored at 16px = 1rem.

| Token | Size / Line-height | Use |
|-------|--------------------|-----|
| `display-xl` | 48 / 52, Instrument Serif italic | Dashboard hero ("Today's episode"), celebration screens |
| `display-lg` | 32 / 38, Instrument Serif | Screen-level section titles |
| `display-md` | 24 / 30, Instrument Serif | Empty states, modal titles |
| `heading-lg` | 20 / 28, Satoshi 600 | Panel headings |
| `heading-md` | 16 / 24, Satoshi 600 | Card titles, subsection headings |
| `heading-sm` | 13 / 18, Satoshi 600, letter-spacing 0.04em, uppercase | Meta-labels ("STATUS", "DURATION") |
| `body` | 14 / 22, Satoshi 400 | Default body text |
| `body-lg` | 16 / 26, Satoshi 400 | Long-form reading (clip hooks, compelling reasons) |
| `body-sm` | 13 / 20, Satoshi 400 | Secondary/helper text |
| `code` / `timecode` | 13 / 18, JetBrains Mono 500, tabular-nums | Coordinates, durations, clip IDs |
| `code-sm` | 11 / 16, JetBrains Mono 500, tabular-nums | Inline IDs, chip labels |

### Editorial rule

**Italic Instrument Serif is reserved for moments of voice**, not decoration. Appropriate uses: dashboard greeting ("*Today's episode*"), empty states ("*Nothing to review yet.*"), completion screens ("*Episode live.*"). Never use it for column headers, buttons, or chrome.

---

## 4. Color

Warm-dark palette. Every value has a named token; no raw hex in components.

### Surface tiers

| Token | Hex | Use |
|-------|-----|-----|
| `--surface-canvas` | `#0B0B0C` | App background |
| `--surface-1` | `#141416` | Primary panels, cards |
| `--surface-2` | `#1C1C1F` | Nested panels, popovers, input backgrounds |
| `--surface-3` | `#26262A` | Raised elements, hover states on surface-2 |
| `--surface-inset` | `#08080A` | Wells, code blocks, the canvas *inside* a panel |

### Borders + dividers

| Token | Hex | Use |
|-------|-----|-----|
| `--border-subtle` | `#1F1F22` | Invisible-until-you-look dividers |
| `--border` | `#2A2A2F` | Default borders |
| `--border-strong` | `#3A3A40` | Focused inputs, selected cards |

### Text tiers

| Token | Hex | Use |
|-------|-----|-----|
| `--text-primary` | `#F3EEE3` | Warm off-white — headings, primary body |
| `--text-secondary` | `#B8B2A4` | Warm gray — labels, helper text |
| `--text-tertiary` | `#7A7466` | Muted — captions, disabled-but-readable |
| `--text-disabled` | `#4A4540` | Disabled chrome |
| `--text-on-accent` | `#0B0B0C` | Text on amber accent backgrounds |

### The single accent

One color carries every "live / active / processing / hot" signal. One.

| Token | Hex | Use |
|-------|-----|-----|
| `--accent` | `#F5A524` | Primary action buttons, processing progress, active nav, pulsing live indicator |
| `--accent-soft` | `#3A2A10` | Accent backgrounds (e.g. "processing" row tint) |

Amber, not red-orange, not yellow. Reads warm on dark without being a highlighter.

### Status colors

These do not compete with the accent. They appear *only* on status chips, progress bars, and confirmation dialogs.

| Token | Hex | Meaning |
|-------|-----|---------|
| `--status-success` | `#6FCF8E` | Complete, approved, live |
| `--status-working` | `#F5A524` | Processing, uploading — same as `--accent` |
| `--status-warning` | `#E8B059` | Attention needed, degraded |
| `--status-danger` | `#E26D5A` | Failed, blocking error |
| `--status-neutral` | `#7A7466` | Queued, paused, not-yet-started |

### Speaker palette

Four colors, chosen as a set. Used in crop overlays, clip-attribution chips, and waveform tinting. **Always used in order** — speaker 1 is always teal, speaker 2 is always coral, etc. Never shuffled. Four is the cap; if we ever need more speakers we choose a fifth deliberately.

| Slot | Hex | Name |
|------|-----|------|
| Speaker 1 | `#6BB7B7` | Seafoam |
| Speaker 2 | `#E8926D` | Coral |
| Speaker 3 | `#B394D6` | Wisteria |
| Speaker 4 | `#D9C56B` | Straw |

Wide-shot crop and ambient tracks use `--text-tertiary` outlines so they stay subordinate to speaker crops.

---

## 5. Spacing, radii, shadows

### Space scale

Base 4px. Tokens: `space-0` (0), `space-1` (4), `space-2` (8), `space-3` (12), `space-4` (16), `space-5` (24), `space-6` (32), `space-7` (48), `space-8` (64), `space-9` (96).

Default gutter inside a panel: `space-5` (24). Default gap between stacked panels: `space-6` (32). Dense tables tighten to `space-3`.

### Radii

| Token | Px | Use |
|-------|-----|-----|
| `radius-sm` | 4 | Inputs, chips |
| `radius-md` | 8 | Buttons, small cards |
| `radius-lg` | 12 | Panels, modals, video frames |
| `radius-full` | 999 | Circular badges, avatar-like indicators |

### Shadows

Shadows are subtle — we lift with light, not with dark drop shadows.

| Token | Definition | Use |
|-------|-----------|-----|
| `shadow-sm` | `0 1px 0 rgba(255, 240, 220, 0.04) inset, 0 1px 2px rgba(0,0,0,0.4)` | Raised cards |
| `shadow-md` | `0 1px 0 rgba(255, 240, 220, 0.05) inset, 0 4px 16px rgba(0,0,0,0.4)` | Popovers |
| `shadow-lg` | `0 1px 0 rgba(255, 240, 220, 0.06) inset, 0 12px 40px rgba(0,0,0,0.5)` | Modals |

The inset top-highlight (cream, 4–6% opacity) is the signature — it's what makes panels feel like they're catching warm studio light rather than sitting flat.

---

## 6. Motion

Ease curve (default for all state transitions): `cubic-bezier(0.2, 0.8, 0.2, 1)` ("out-expressive").

| Duration token | ms | Use |
|---------------|-----|-----|
| `motion-fast` | 120 | Hover, focus, button press |
| `motion-base` | 220 | Most UI transitions |
| `motion-slow` | 400 | Panel reveals, page transitions |
| `motion-celebrate` | 800 | Status completions, publish success |

### Motion principles

- **Loud on state change, silent elsewhere.** No hover jiggles. No "AI thinking" shimmer on static content. When the pipeline moves from `processing` to `ready_for_review`, the status chip briefly scales up (1.0 → 1.06 → 1.0 over 600ms) and its color transitions amber → green with a 400ms dwell on white at the apex — a single, confident flash.
- **Progress uses scanlines.** The processing-progress bar isn't a smooth indeterminate shimmer — it's a 2px vertical scanline sweeping right at a fixed cadence. Broadcast reference, not developer-tool reference.
- **Live indicators pulse on a 2s breathing curve.** Amber dot beside "processing" status. Not 1s (anxious), not 4s (dead). 2s is the heartbeat.
- **Page entry: no staggered slide-in.** The app is a working tool — arriving at a screen should be instant. The one exception is the dashboard on first load: its episode list fades in over 240ms with a 30ms stagger per row.

---

## 7. Layout architecture

### Three-column shell

```
┌─────┬─────────────────────────────────────┬─────────────────┐
│     │                                     │                 │
│ Nav │           Main content              │  Agent panel    │
│ 72  │           (fluid)                   │  380px (resvd)  │
│     │                                     │                 │
└─────┴─────────────────────────────────────┴─────────────────┘
```

- **Left nav (72px, icon-only).** Dashboard, New Episode, Schedule, Analytics. Settings at bottom. Icons + subtle labels on hover. Active item gets the amber accent as a 2px left border + slight surface-2 background.
- **Main content.** Fluid width, max 1440px, centered. Screens that need full width (crop setup, longform review, clip review) can opt out of the max-width and expand edge-to-edge.
- **Agent panel (right rail, 380px, reserved).** Always present in the layout. Today: renders an "Agent" header + a single placeholder card reading *"Agent chat arrives in Phase C. For now, agents speak through this panel's status feed."* Below the placeholder, a live event log of the current episode's pipeline events (the existing poll-based status translated into plain English). This panel is collapsible (chevron in top-right) — collapsed state is 48px wide with just the "Agent" spine label rotated 90°.

The agent panel is architecturally load-bearing: every screen assumes it might be present. Pages that need maximum canvas (crop setup, longform review) auto-collapse it on entry and remember the choice per-user.

### Status-first information hierarchy

On every screen, the first visual element after the page title is the **status pill** — always in the same position (top-right of the main content, above the fold), always the same component. Status is never buried inside a card; it's chrome-level information.

---

## 8. Component vocabulary

Each component gets its own file in `src/components/` with a consistent API. This is the complete set; anything new starts with a design-system PR.

### Status & signals

- **StatusPill** — capsule with colored dot + label + optional ETA. Eight states: `queued`, `processing`, `awaiting_crop`, `awaiting_longform_review`, `awaiting_clip_review`, `awaiting_publish`, `live`, `error`. Each state has a fixed color, icon, and plain-English label — never raw codes.
- **ProgressBar** — two variants: *continuous* (0–100%, used for long renders with known ETA) and *stepped* (14-segment pill representing the 14 pipeline agents, each filled/empty/current/errored). Scanline sweep on the active segment.
- **LiveDot** — 8px amber dot with 2s breathing pulse.
- **EventFeed** — the plain-English status stream. Each entry: timestamp (mono, tertiary), icon, sentence. Auto-scrolls; pinnable.

### Inputs

- **Button** (`primary` amber-filled, `secondary` surface-2, `ghost` no-bg, `destructive` with red-bordered confirmation), three sizes (`sm` 28, `md` 36, `lg` 44). Always has loading state (spinner replaces label, button keeps width).
- **IconButton** — 36px square variant.
- **TextInput**, **TextArea**, **NumberInput** (with up/down chevrons + scroll-wheel nudge), **Select** (custom — no native chrome), **Toggle** (sliding thumb, amber when on).
- **Slider** — range slider with visible, grippable thumb (12px amber circle with white-cream highlight), numeric readout to the right of the track. Used for zoom, volume, sync offset.
- **ConfirmationModal** — destructive actions require the user to type the episode title (for delete) or click a second "yes, I'm sure" button that only enables after 800ms.

### Surfaces

- **Card** — surface-1, radius-lg, 24px padding, optional header slot.
- **Panel** — like Card but full-bleed inside its column; used as the dominant layout primitive on detail pages.
- **Popover** — surface-2, shadow-md, radius-md, 16px padding.
- **Modal** — surface-2, shadow-lg, radius-lg, 32px padding, max 560px wide, darkens canvas to `rgba(0,0,0,0.6)` behind.
- **Toast** — bottom-right stack, surface-2, auto-dismiss 5s, manual dismiss on hover. Max three visible.

### Editorial

- **ClipCard** — the hero component of the clip review screen. 9:16 thumbnail left (autoplay muted on hover), metadata right (title in `heading-md`, hook in `body-lg` italic from `Instrument Serif`, score chip, duration, speaker chip), action row below (`keep` / `reject` / `retitle` / `combine` / `trim`). Per-platform metadata collapses into an accordion under the main row.
- **SpeakerChip** — small chip with the speaker's swatch color + name.
- **Timecode** — mono, tabular, formatted `m:ss` or `h:mm:ss` depending on duration.

### Audio-specific

- **WaveformCanvas** — renders peaks as vertical lines. Accepts multiple tracks (camera, H6E) with per-track tint. Supports: zoom via scroll, pan via drag, click-to-seek, offset overlay.
- **MixerRow** — stem label, assignment chip (read-only, editable in Speakers panel), mute / solo, fader (slider with visible thumb + dB readout), live VU meter (2-bar LED-style, amber when peaking).
- **SyncOffsetControl** — a compact horizontal slider (±10s range) with a numeric input to the right, ±0.1 / ±1 / ±5 / ±10 buttons, and a "reset to auto-detected" action. Scroll wheel on the numeric input nudges by 0.01s.
- **CropCanvas** — the crop editor. Image or live video frame as background, per-speaker 9:16 + 16:9 rectangles overlaid. 9:16 rect is dashed, 16:9 rect is dotted, same speaker color. Wide-shot rect is neutral. Click places the currently-selected crop for the currently-selected speaker. Keyboard: `1`–`4` selects speaker, `9` / `6` toggles shorts/longform.

---

## 9. Voice & copy

Cascade talks like a production assistant, not a robot and not a startup.

| Don't say | Do say |
|-----------|--------|
| "Status: awaiting_crop_setup" | "Waiting for you to set up crops" |
| "Pipeline failed at speaker_cut" | "Speaker cut failed — couldn't find clear cuts between speakers. Retry?" |
| "Are you sure you want to delete this episode?" | "This will delete the PJ Greenbaum episode and everything on disk. Type the title to confirm." |
| "Clip approved." | "Kept." |
| "Upload successful." | "Live on YouTube, TikTok, Instagram, X." |
| "Re-rendering clip" | "Rendering the new cut — about 90 seconds." |

### Numbers are always contextualized

- Not "Duration: 2437" → "Duration: 40m 37s"
- Not "Offset: 0.427" → "H6E leads camera by 427ms"
- Not "Score: 8" → "Virality 8/10"

### Empty states are an opportunity

Every empty state uses Instrument Serif for the headline, Satoshi for the body. Examples:
- Dashboard with no episodes: *"Nothing on deck."* — "Plug in an SD card or hit New Episode."
- Clip review before clip-mining runs: *"The clip miner hasn't run yet."* — "It'll go once the longform is approved."
- Analytics: *"Too early to tell."* — "Performance data lands a week after each episode publishes."

---

## 10. Screen inventory

These are the screens being built, each with a planned layout shape. Details live in per-screen design docs that will land as they're built.

| Screen | Route | Layout shape |
|--------|-------|--------------|
| Dashboard | `/` | Hero greeting + "Today's episode" spotlight + episodes table |
| New Episode | `/new` | Single-column wizard, 1–2 steps |
| Episode Detail | `/episodes/:id` | Sticky summary header + scrolling sections (Overview, Longform, Clips, Audio, Metadata). Agent panel visible by default. |
| Crop Setup | `/episodes/:id/crop-setup` | Full-width, agent panel collapsed by default. Split layout: video + canvas left, speakers + sync right. **Not tabbed** — everything visible, arranged as a single editing surface. |
| Longform Review | `/episodes/:id/longform` | Full-width. Player top, cut timeline below, edit-request input docked bottom. |
| Clip Review | `/episodes/:id/clips` | List of ClipCards, one expanded at a time. Chat input docked bottom. |
| Publish | `/episodes/:id/publish` | Schedule timeline (week view) + per-platform status column |
| Backup | `/episodes/:id/backup` | Confirmation screen with explicit dangerous-action UI |
| Schedule | `/schedule` | 7-day calendar, clips laid on platform-colored lanes |
| Analytics | `/analytics` | Placeholder ("Too early to tell.") for now |

---

## 11. Technical choices (requires sign-off)

This is where I need a decision before I start writing code. My recommendation is opinionated; push back if you disagree.

### 11.1 Framework: **vanilla TypeScript + ES modules + a ~80-line reactive primitive**

Not React, not Svelte, not Vue. Reasons:
- The app is a thin UI shell over a FastAPI backend. There's no distributed state, no routing beyond hash, no server rendering. A framework is overhead.
- Canvas-heavy work (crop editor, waveforms, VU meters) is imperative anyway; React reconciliation gets in the way.
- The current 4000-line monolith's problem is *scope*, not vanilla. Splitting into ES modules (one file per screen, one file per component) fixes it.
- Zero framework runtime means the production bundle is <50KB gzipped.

What I *will* add: a small signals primitive (`signal`, `computed`, `effect` — ~80 lines) to keep reactive state clean. Not Solid-style fine-grained, just enough to avoid manual DOM surgery.

### 11.2 Build: **Vite + TypeScript + compiled Tailwind**

- Replaces Tailwind CDN (the known problem). Tailwind compiles via PostCSS in the Vite pipeline.
- Vite gives HMR in dev, tree-shaken prod builds, TS support without Babel config.
- Dev server on port `8421`, proxies `/api/*` to the FastAPI server on `8420`.
- Prod build outputs to `frontend/dist/`; FastAPI serves `dist/` as static.
- Jest stays for unit tests; add Vitest for component tests (shares Vite config).

### 11.3 Parallel rollout

Keep the current `frontend/` alive as `frontend-legacy/`. New build lives in `frontend/`. A flag in FastAPI (`CASCADE_LEGACY_UI=1`) serves the old one if anything breaks mid-rollout. Flag removed once we cut over. This is cheap insurance — one `if` in `server/app.py`.

### 11.4 Directory structure

```
frontend/
  src/
    main.ts                     # entry, mounts router + renders shell
    lib/
      signals.ts                # ~80-line reactive primitive
      api.ts                    # typed client, one fn per backend route
      router.ts                 # hash router
      format.ts                 # timecode, duration, plain-English status
      events.ts                 # poll-based pipeline event stream (SSE-ready)
    state/
      episodes.ts               # global episode store
      pipeline.ts               # active pipeline poller
      ui.ts                     # sidebar collapsed, agent panel collapsed, etc.
    components/
      {Button,StatusPill,...}.ts
      audio/
        {WaveformCanvas,MixerRow,SyncOffsetControl}.ts
      crop/
        CropCanvas.ts
    screens/
      dashboard.ts
      episode/
        index.ts                # shell
        overview.ts
        longform.ts
        clips.ts
        audio.ts
      crop-setup.ts
      clip-review.ts
      publish.ts
      backup.ts
      schedule.ts
      analytics.ts
    styles/
      index.css                 # Tailwind + tokens + @layer overrides
      fonts.css                 # @font-face declarations, self-hosted
  index.html
  vite.config.ts
  tailwind.config.ts
  postcss.config.js
  package.json
  tsconfig.json
  dist/                         # build output, gitignored
frontend-legacy/                # current app.js, frozen, removed on cutover
```

### 11.5 SSE-readiness

The backend is poll-only today. I'll build the event stream behind an interface (`subscribe(episodeId, onEvent)`) that's poll-backed now. When an `/api/episodes/:id/events` SSE endpoint lands, swapping the implementation is a one-file change with no call-site impact.

---

## 12. What I need from Sam before I start coding

Short answers are fine.

1. **Typography: Instrument Serif + Satoshi + JetBrains Mono** — approve, or want me to propose alternatives?
2. **Color: warm-dark with single amber accent** — approve, or prefer a cooler palette (more blue-leaning)?
3. **Framework: vanilla TypeScript + signals, not React/Svelte** — approve, or want me to go to a framework? (My strong rec: vanilla.)
4. **Build tool: Vite + compiled Tailwind** — approve? (Fixes Tailwind CDN issue.)
5. **Parallel rollout via `frontend-legacy/` + env flag** — approve, or just replace?
6. **Right-rail agent panel reserved from day one** — approve, or wait until Phase C?
7. **Three-column shell with 72px icon nav** — approve, or prefer a top-nav shape?
8. **Crop Setup as a single editing surface (not steps, not tabs)** — approve, or want me to split it into Setup → Sync → Crop steps like the handoff mentioned as an option?

Once these are answered, I build the scaffolding + Dashboard + Episode Detail skeleton first (pattern-establishing), then Crop Setup (hardest), then Clip Review (highest-value), then the rest.
