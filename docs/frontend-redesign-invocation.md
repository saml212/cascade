# Paste this into a fresh Claude Code session

Open a new Claude Code session in this repo (`/Users/samuellarson/Local/Github/cascade`) with the frontend-design skill installed. Paste everything below the `---` as your first message.

---

You're taking over the cascade frontend redesign. Your full brief is at `docs/frontend-redesign-handoff.md` — read it end to end before you do anything else, then work against it.

## Your mandate, short version

1. Rebuild the cascade frontend (`frontend/`) from scratch into a professional, opinionated UI that matches the seriousness of the product. The current UI is functional but visually flat and interactionally confusing. The reference quality bar is Linear / Raycast / Superhuman, not a typical admin dashboard.

2. The backend is FROZEN. FastAPI routes, Pydantic schemas, episode.json on disk — none of these change. You rebuild the frontend as a drop-in replacement that reads/writes the existing API. If you think an endpoint is missing, STOP and flag it to Sam — don't add routes.

3. Use the frontend-design skill actively. Propose a design system before building. Document it at `docs/design-system.md`. Then build from that system.

4. Design as if the cascade UI is always running alongside a Claude Code agent session — status changes should be legible enough that the agent's future chat panel can echo them in human language. Leave architectural space for an agent panel (right rail or bottom dock). Don't build the panel now.

## Where to start

1. Read `docs/frontend-redesign-handoff.md` — the full brief. Everything you need is in there.
2. Read the current `frontend/app.js` end to end (it's ~4000 lines, plan for that).
3. Read the Pydantic models in `server/routes/*.py` — those define your API contract.
4. Read one real `episode.json` from `/Volumes/1TB_SSD/cascade/episodes/ep_2026-02-17_234937/episode.json` so you see the actual data shape.
5. Propose your design system. Discuss it with Sam before building anything visual.
6. Build the Dashboard + Episode Detail skeleton first to establish patterns.
7. Then Crop Setup (the hardest page — the brief breaks it down into 6 concerns that are fighting for space).
8. Then Clip Review (the biggest UX payoff).
9. Fill in remaining screens.
10. Polish relentlessly.

## Ground rules

- Don't ship the Tailwind CDN (currently used; a known problem). Compile via CLI/PostCSS, or switch CSS approach entirely.
- Don't add `anthropic` SDK to frontend — backend only.
- Don't change Pydantic models or routes.
- `/clean` must pass before any commit. This project has a pre-commit-gate sentinel — if a commit blocks, run `/clean` first.
- Don't destroy real episode data — there are 5 live episodes on disk, some mid-flight.
- If you want to change framework (vanilla JS → React/Svelte/etc), discuss with Sam first — significant scope addition.

## Live test cases

Five real episodes on disk. Use them to verify every screen:
- `ep_2026-02-17_234937` — PJ Greenbaum, 2-speaker Canon, `ready_for_review` with clips + longform
- `ep_2026-03-02_073400` — Laura + Todd, 2-speaker Canon, `ready_for_review`
- `ep_2026-03-18_204203` — Tug Life, 3-speaker H6E, `ready_for_review`
- `ep_2026-04-16_235129` — 3-speaker H6E, `awaiting_crop_setup` — mid-flight test case, **don't mutate state**
- `ep_2026-04-22_001253` — PJ rebuild, 2-speaker Canon, `awaiting_crop_setup` — mid-flight test case

## Deliverables

1. `docs/design-system.md` — colors, typography, spacing, components, voice. Written before building.
2. Rebuilt frontend replacing `frontend/`. Ship alongside the old UI at `/new/` route while developing if feasible.
3. Updated README or frontend docs for how to build/serve/extend the new UI.
4. Verification: every flow in the brief's "what Sam actually does" section walks cleanly on real episodes.

Good luck. Sam is a non-technical podcast creator who deserves a serious tool. Build it that way.
