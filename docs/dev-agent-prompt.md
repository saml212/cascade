# Dev Agent Prompt — Cascade Wishlist + Harness Dogfood

Copy the contents below into a fresh Claude Code session's first message. This is the handoff from the harness-build session to the first real dev-work session.

---

## Session goal

You are taking over the cascade repo after a multi-day harness build. Two jobs, in order:

1. **Work the wishlist** (`wishlist.md` in repo root) — the real podcast-pipeline backlog.
2. **Dogfood-critique the harness as you go** — the harness is new, built test-first with self-review, but it's had exactly zero real-world dev-agent use. You're the first production user. Write down everything that feels wrong.

Report back when you hit a natural stopping point with **(a)** what you completed and **(b)** a `harness-feedback.md` file listing every pain point and suggested fix.

## Start with a 2-minute environment sanity check

Run these and confirm everything's healthy before touching anything:

```bash
# Harness tests — should be 44/44 pass
bash .claude/profiles/tests/run-all-tests.sh

# Existing cascade tests — should still pass (some dev-work is uncommitted, so don't worry if a few fail)
.venv/bin/pytest tests/ -v --tb=short

# Skills registered — should see clean, autoresearch, deploy-team
ls .claude/skills/

# Subagents available — orchestrator, python-specialist, frontend-specialist, verifier
ls .claude/agents/

# Hooks wired in settings
cat .claude/settings.json | jq '.hooks | keys'
```

If anything's broken, stop and report it rather than work around it — that's a harness bug, log it.

## Priority order for wishlist work

Read `wishlist.md` for the full backlog. Work it in this order unless you have a reason to deviate:

### 1. Repo cleanup first (pre-flight)
There are ~15 modified Python/JS files from stalled dev work and 4 untracked test files in `tests/`. Your first task is to figure out what works, commit what's good, revert what's stale.

Approach: read `git status --short`, then for each modified file:
- Does it correspond to a complete-and-working change?
- Do tests pass if you reset just that file?
- Is there a `[LEARN]`-worthy gotcha in why it was left uncommitted?

Dispatch `python-specialist` for the Python files, `frontend-specialist` for `frontend/app.js`. Use `verifier` after changes land to confirm tests stay green. Commit in logical chunks (one per feature), not one giant commit — each through `/clean`.

Also: archive `docs/PLAN_2026-04-11.md` and `docs/PLAN_publishing_2026-04-12.md` to `docs/archive/` if they're stale plans (read them first). Rename `tests/test_lib_ass_render.py` if its name is confusing (check `lib/ass.py`).

### 2. API → `claude` CLI migration
Three files use the `anthropic` SDK: `agents/clip_miner.py`, `agents/metadata_gen.py`, `server/routes/chat.py`. Migrate all three to subprocess-invoke the `claude` CLI with `-p --output-format stream-json --verbose --model <tier>`. Keep Deepgram's direct httpx REST path — that stays on API.

Sequencing:
- clip_miner first (simplest, non-streaming, pure JSON output)
- metadata_gen second (same pattern)
- chat.py last (most complex — streaming to SSE endpoint, keep action-parsing intact)

Dispatch `python-specialist` per file; use `verifier` after each. Update `.env.example` (remove `ANTHROPIC_API_KEY` after all three are done) and `docs/configuration.md` "API Costs" section.

### 3. Apply `/autoresearch` to the clip-mining + metadata prompts
With migration done, use `/autoresearch` on the clip_miner prompt and the metadata_gen per-platform prompts. Score against 2-3 real test episodes. The wishlist has rubric sketches.

### 4. Design the multi-lens clip-mining team
This is where `/deploy-team` actually earns its keep. Write a team config with 4 agents (humor / drama / educational / quotable lenses), each scoring the same transcript independently. Main agent synthesizes into a final ranked clip list. Save the config template at `.claude/teams/configs/multi-lens-clip-mining.json`. Run it on one episode end-to-end as proof.

### 5. UX friction fixes (whichever you have time for)
Prioritize the status-signaling items (per-agent live status, longform render progress bar). Then the interactive clip workflow. Use `frontend-specialist` for the UI work, `python-specialist` for any server-side endpoints.

**Not in this session:** Small Council, Realm portal, anything marked "deferred" in the wishlist.

## How to use the harness (read once, apply throughout)

### Commits go through `/clean`
After any substantive change:
1. `git add <files>` — stage what you're about to commit
2. Invoke `/clean` — it runs ruff format, ruff check --fix, vulture, and an AI slop audit on the staged files only; writes a sentinel on pass
3. `git commit -m "..."` — the pre-commit-gate hook checks the sentinel and lets it through

If the gate blocks you, the message will explain how to unstick. **`CLEAN_BYPASS=1 git commit ...` is only for genuine emergencies or docs-only commits.** Overuse it and you're defeating the point.

### Subagent-first, not team-first
**Default to dispatching subagents** (Agent tool) for everything:
- Research / exploration → `Explore` (built-in)
- Implementation → `python-specialist` / `frontend-specialist`
- Quality gate → `verifier`
- Security audit of a change → `differential-review` or `sharp-edges` plugins
- Cross-cutting planning → `orchestrator`
- Multi-step arbitrary → `general-purpose`

**Only `/deploy-team`** when ALL three hold:
1. Genuinely ambiguous problem — distinct perspectives produce materially different answers
2. Parallel compute actually buys cross-pollination via shared thread
3. Real-time observability matters (dashboard)

The multi-lens clip mining task (#4 above) is a genuine team job. Most other wishlist items are specialist jobs. **Do not default to `/deploy-team` for normal work** — it's overhead and it's observably slower. If you catch yourself reaching for a team when a specialist would do, that's itself a harness-feedback item worth logging.

### Emit `[LEARN]` blocks when you learn
When the developer corrects you or you discover a non-obvious gotcha, end your response with:

```
[LEARN] <category-slug>: <one-line rule>
Mistake: What went wrong concretely
Correction: What the right approach is
```

The Stop hook saves it to `.claude/memory/corrections/<dev>/corrections.jsonl`. Don't force `[LEARN]` blocks when nothing real was learned — the `correction-detect` hook will nudge you when appropriate.

### Safety hooks will stop you
`safety-check.sh` blocks `rm -rf` on episode data, force-push to main, `git add` of secrets, `git reset --hard main`. If you hit a block, the message explains what triggered. **Don't try to bypass with `sh -c` or other tricks** — if the block is wrong, that's a harness bug, log it.

## Harness critique — what to capture as you work

Keep a running `harness-feedback.md` file in the repo root (gitignored or not — I'll decide later). For each issue, note:

```
## <category>: <one-line>
**File:** <path:line> (or "CLAUDE.md", "/clean skill", etc.)
**Severity:** blocker / friction / polish
**What I hit:** <concrete event>
**Suggested fix:** <if you have one>
```

Areas to watch:

### Hooks
- Are you getting any **false blocks**? (safety-check rejecting something safe, pre-commit-gate rejecting when /clean did run?)
- Are **block messages clear**? Could you act on them immediately or did you need to dig?
- Any **performance drag**? (hooks add ~100ms per Bash call — does it feel annoying?)
- Any **bypasses** you notice that should be tighter?

### Skills
- **`/clean`**: does the sentinel workflow feel right? Does the static pass catch real slop or miss it? Is the AI audit too pedantic or too loose? Does it cost more than it saves?
- **`/autoresearch`**: when you use it, does the loop converge? Is the rubric-writing burden reasonable or too heavy? Does `lessons.md` help?
- **`/deploy-team`**: dashboard usable? Intervention flow (thread post, DM to agent, pause/resume) actually work when you try them? Findings in `thread.md` easy to synthesize?

### Memory
- Does emitting `[LEARN]` feel natural? Or forced?
- Does `correction-detect`'s nudge trigger too often? Too rarely?
- Does `rules-compiled.md` actually help on session start, or is it background noise?
- `/publish-memory` doesn't exist yet — did you need it? (If yes: add to the feedback list as "blocker" and note the use case.)

### Subagents
- Are the system prompts (in `.claude/agents/*.md`) accurate for what you asked them to do?
- Did `python-specialist` follow the "no `anthropic` SDK imports" rule? Did `verifier` stay strict about not fixing?
- Did `orchestrator`'s "default to specialist" rubric hold up when you were deciding whether to team?
- Were there tasks where you **wished a subagent existed** that doesn't? (Auditor? Researcher? Planner beyond the built-in Plan?)

### Profile + settings
- Is `.claude/profile.json` shaped right? Any language configs that are wrong for cascade?
- Permissions allowlist in `.claude/settings.json` — anything missing that forced repeated approvals?

### Design boundaries
- When did you reach for `/deploy-team` vs. a regular subagent? Did the harness make that choice clear, or were you second-guessing?
- Did you ever feel pushed toward a tool that didn't fit the problem?

## Reporting back

When you hit a stopping point (anywhere from "wishlist items 1-2 done" to "whole wishlist done"), surface:

1. **Completed work** — list with commit hashes
2. **In-progress work** — what's mid-flight and why you stopped there
3. **`harness-feedback.md`** — all observations, severity-ordered
4. **Any `[LEARN]` blocks you emitted** — check `.claude/memory/corrections/<dev>/corrections.jsonl`

The developer will take your feedback and do another harness quality pass. **Be specific and be blunt.** Vague feedback ("/clean feels clunky") doesn't help; specific feedback ("/clean reran 3× in 20 minutes because I kept amending commits, each run took ~8 seconds of wall clock — maybe skip the full audit on amend?") does.

Good luck. Start with the sanity check.
