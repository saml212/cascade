# Cascade — 14-agent podcast pipeline

Ingests camera + multi-track audio, produces longform (16:9) + shorts (9:16) + metadata + thumbnails, publishes, backs up.

## Architecture
- [Pipeline & Agents](docs/architecture.md) — agent system, DAG, stage behaviors
- [Shared Libraries](docs/libraries.md) — `lib/ffprobe`, `lib/srt`, `lib/encoding`, `lib/paths`, `lib/audio_mix`, `lib/audio_enhance`
- [Server & Frontend](docs/server.md) — FastAPI routes, chat agent actions, vanilla JS SPA

## Workflow
- [Commands](docs/commands.md) — setup, CLI pipeline, tests, API endpoints
- [Configuration](docs/configuration.md) — `config.toml`, `.env`, dependencies
- [Error Handling](docs/error-handling.md) — resuming failed runs, agent output shapes

## Quality Systems
- [Anti-Slop Pipeline](docs/anti-slop-pipeline.md) — `/clean` skill, 3-step gate before commits
- [Memory System](docs/memory-system.md) — `[LEARN]` blocks, corrections, team memory
- [Profile](.claude/profile.json) — tooling config (formatter, dead-code, complexity)

## Key Rules
- ONE main agent. Subagent specialists and agent teams are tools the main agent dispatches, not workflows the developer manages.
- **Cognitive burden is on the main agent**, not the developer. The developer should never have to remember to type `/clean` or `/verify` — hooks and main-agent judgment handle it.
- **Skills and hooks are general-purpose but cascade-scoped.** A skill should be reusable across tasks (e.g., `/clean` works on any changed file set, not just agents/) but its defaults and profile tooling reflect cascade's stack (Python + vanilla JS, ruff/vulture, ffmpeg pipeline). Write once, apply broadly within cascade — don't hardcode specific agent names or episode IDs.
- Every token fights for its place — skills over MCP, concise over comprehensive.
- macOS SD-card resource forks (`._*.MP4`) must be filtered: `if not f.name.startswith("._"):`
- Deepgram SDK v5 broke compatibility — use httpx REST directly.
- ffmpeg 8.x: `-shortest` not `-fflags +shortest`; `-use_editlist 0` for platform compliance.

## Self-Correcting Memory

When you are corrected or discover a mistake, emit a `[LEARN]` block in your final response. The Stop hook captures it automatically.

```
[LEARN] Category: One-line rule
Mistake: What went wrong
Correction: What the right approach is
```

See [memory-system.md](docs/memory-system.md) for how this compounds into loaded rules over time.

## Skills & Subagents (main-agent triggers)

### Skills — invoke autonomously, don't wait for the developer
- `/clean` — **MUST run before every `git commit`**. Hook-enforced (pre-commit gate checks a sentinel). Runs ruff + vulture + simplifier + AI slop audit on staged files.
- `/autoresearch <target>` — iteratively optimize any text artifact (prompts, docs, specs). Use when the user says "make this better", "iterate on", "keep improving", or when a prompt underperforms.
- `/deploy-team <purpose>` — summon a multi-agent team with shared thread + live dashboard. See the rubric below — this is NOT the default for parallel work.

### Subagents (Agent tool) — the default for focused work
- `orchestrator` (opus) — cross-cutting planning, decomposition across agents/lib/server/frontend boundaries, sequencing
- `python-specialist` (sonnet) — `agents/`, `lib/`, `server/`, `tests/` implementation
- `frontend-specialist` (sonnet) — vanilla JS in `frontend/`
- `verifier` (sonnet) — runs tests, ruff, vulture; reports pass/fail with specifics (does NOT fix)
- Built-ins available: `Explore` (codebase research), `Plan` (architect), `general-purpose` (multi-step arbitrary tasks)
- Plugin agents active: `differential-review:differential-review`, `sharp-edges:sharp-edges`, `skill-improver:skill-improver`

### Subagent vs. `/deploy-team` — pick the right tool

**Default to subagents** (Agent tool) for:
- Research / codebase exploration → `Explore`
- Focused implementation → `python-specialist` / `frontend-specialist`
- Bug fixing → `python-specialist` + `verifier`
- Quality gate → `verifier`
- Security review of a specific change → `differential-review` or `sharp-edges`
- Cross-cutting planning → `orchestrator`
- Any "do X and come back with the result" task → the appropriate subagent

**Only `/deploy-team`** when ALL three hold:
1. The problem is **genuinely ambiguous** — different perspectives will produce materially different answers
2. Parallel compute **actually buys** something (not just speed — distinct lenses that cross-pollinate via the shared thread)
3. You want **real-time observability** (dashboard, intervention mid-run)

Good `/deploy-team` jobs: multi-lens clip mining (humor/drama/educational/quotable — each scores clips differently), multi-perspective PR review (security + correctness + simplicity), architecture decision with three plausible paths.

Bad `/deploy-team` jobs: "find all uses of X" (→ Explore), "fix this bug" (→ python-specialist), "audit this function" (→ single sharp-edges call), "implement this feature" (→ python-specialist). If one specialist can handle it, a team is just overhead.

### Known gaps (not yet built)
- `/publish-memory` — would promote developer corrections to `.claude/memory/team/`. Documented in `docs/memory-system.md` but not implemented. For now, promote manually by writing to `.claude/memory/team/<slug>.md`.

## Backlog
[wishlist.md](wishlist.md) — tasks to hand to a dev-agent session.
