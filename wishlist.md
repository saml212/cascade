# Cascade Wishlist

Running list of things to hand to a dev agent once the harness is built. Organized by theme, not priority — the dev agent should be given a subset per session.

## API → `claude` CLI migration (use Max subscription, not API billing)

- [ ] `agents/clip_miner.py` — replace Anthropic SDK call with `claude -p` subprocess. Stream-JSON output; parse clip JSON blocks from response.
- [ ] `agents/metadata_gen.py` — same pattern. Per-platform metadata generation.
- [ ] `agents/thumbnail_gen.py` — uses OpenAI for image, but any Claude calls in here should move too.
- [ ] `server/routes/chat.py` — the 13-action chat agent. Biggest migration: streaming needs to work for the UI.
- [ ] `chat.py` `auto_trim` action — currently calls Claude API; migrate.
- [ ] Update `CLAUDE.md` "API Costs per Episode" section to reflect subscription use.
- [ ] Remove `ANTHROPIC_API_KEY` requirement from `.env.example` once migration is complete (unless something still needs it).

## UX friction fixes (user-facing)

### Better status signaling (no notifications needed, per user)
- [ ] Per-agent live status on the episode detail page — show which agent is currently running, elapsed time, ETA if known.
- [ ] Longform render progress bar (percent of segments rendered).
- [ ] Transcribe progress (minutes uploaded / processed).
- [ ] Make the four `awaiting_*` states visually distinct in the UI (current: unclear which gate you're on).

### More interactive clip workflow
- [ ] Inline clip editor: trim start/end by dragging a waveform, not by typing numbers.
- [ ] Preview re-render for single clips in under ~10s (currently full re-encode).
- [ ] Threshold-based auto-approval: clip_miner scores above N auto-approve, below review. Config knob in `config.toml`.
- [ ] Bulk actions: "approve top 5 by score", "reject anything under 20s".
- [ ] Side-by-side clip compare view.

### Chat agent safety
- [ ] `delete_clip` action requires explicit user confirmation modal.
- [ ] `rerender_longform` shows cost/time estimate before confirming.
- [ ] All destructive chat actions log to `chat_history.json` with a revert-hint.

### Crop setup (must stay manual, but easier)
- [ ] Show the reference frame at a larger size — current UI makes placement fiddly.
- [ ] Snap-to-face hints on the reference frame (optional overlay) — user still confirms.
- [ ] Save/load crop presets by guest — if Todd comes back, reuse his crop.
- [ ] Better zoom level preview — scrubber with thumbnails showing what each zoom looks like.

## Harness things the dev agent should also tackle

- [x] ~~Hooks, subagents, settings.json~~ — built 2026-04-20 (see `.claude/`)
- [x] ~~Plugin noise pruned~~ — `disabledPlugins` in `.claude/settings.json` kills testing-handbook-skills, mutation-testing, rust-analyzer-lsp, supply-chain-risk-auditor, insecure-defaults, fp-check, property-based-testing
- [x] ~~ruff added to requirements.txt~~ — will be installed on next `./start.sh`
- [ ] `/autoresearch` on the clip_miner prompt — score clip quality across a few test episodes, iterate.
- [ ] `/autoresearch` on metadata_gen per-platform prompts.
- [ ] Agent team tool (v2 §8) for multi-lens clip mining (humor, drama, educational, quotable). Defer until Tier 1+2 pay off.

## Repo cleanup (for the `dev` or `clean` subagent)

- [ ] Decide fate of untracked tests: `test_agent_podcast_feed.py`, `test_agent_publish.py`, `test_lib_ass.py`, `test_lib_ass_render.py`. Commit or delete.
- [ ] Rename `tests/test_lib_ass_render.py` or confirm it's intentionally named (tests lib/ass.py's render path, not a nonexistent `ass_render` module).
- [ ] Commit or revert the uncommitted changes in `clip_miner.py`, `longform_render.py`, `audio_enhance.py`, etc. Git status shows 15+ modified files — backlog of stalled work.
- [ ] Decide fate of `docs/PLAN_2026-04-11.md` and `docs/PLAN_publishing_2026-04-12.md` — archive to `docs/archive/` if complete, or update if still active.
- [ ] Verify new `audio_enhance.py` config keys (`audio_compressor_threshold`, `audio_compressor_ratio`, `audio_denoise_mix`) are in `config.example.toml` and documented.

## Questions for a future session

- Is chat_history.json meant to persist forever, or rotate? Unbounded growth.
- Does `thumbnail_gen` really need OpenAI, or can it be replaced with a local model now that image gen has improved?
- Should `podcast_feed`, `publish`, `backup` be extracted to a separate "post-production" pipeline with its own scheduling?
