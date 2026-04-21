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

**Skills** — invoke these autonomously, don't wait for the developer:
- `/clean` — **MUST run before every `git commit`**. Hook-enforced (pre-commit gate checks sentinel). Runs ruff + vulture + simplifier + AI slop audit.
- `/autoresearch <target>` — iteratively optimize any text artifact (prompts, docs, specs). Use when user says "make this better", "iterate on", or when a prompt underperforms.
- `/deploy-team <purpose>` — summon a multi-agent team with shared thread + live dashboard. Use when a problem is ambiguous, needs multi-lens review (security + correctness + simplicity), or is big enough to warrant parallel compute.
- `/publish-memory` — promote a developer correction to team memory after 2+ occurrences.

**Subagents** (invoke via the Agent tool):
- `orchestrator` (opus) — cross-cutting coordination and decomposition
- `python-specialist` (sonnet) — `agents/`, `lib/`, `server/` implementation
- `frontend-specialist` (sonnet) — vanilla JS in `frontend/`
- `verifier` (sonnet) — runs tests, lint, format as quality gate

## Backlog
[wishlist.md](wishlist.md) — tasks to hand to a dev-agent session.
