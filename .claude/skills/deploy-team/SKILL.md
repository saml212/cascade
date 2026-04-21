---
name: deploy-team
description: Summon a multi-agent team to work in parallel on an ambiguous problem. Each agent works in an isolated git worktree, coordinates via a shared thread, and is observable via a live Chrome dashboard. Use when a problem is genuinely ambiguous (needs multi-lens review — security + correctness + simplicity), when parallel compute buys different perspectives not just speed, or when stuck and want fresh takes. NOT for fire-and-forget parallelism — use the native Agent tool for that.
---

# /deploy-team — summon a multi-agent team

Multi-perspective collaboration, not mass parallelism. The main agent deploys
this when it decides a problem deserves parallel compute from different
angles. The dashboard lets the developer observe + intervene.

## When the main agent summons a team

- **Multi-lens review** before a big commit or PR — security + correctness + simplicity
- **Architecture validation** — "should this be one service or two?" gets a real debate
- **Cross-repo impact analysis** — if cascade ever grows
- **Clip-mining exploration** (from the wishlist) — humor / drama / educational / quotable lenses on the same transcript
- **Any genuinely ambiguous design decision** where different perspectives reduce risk

## When NOT to

- Simple task that one specialist handles (use the native Agent tool)
- Time-critical work (teams have 30s+ spin-up overhead)
- Fire-and-forget parallel dispatch (use Agent tool instead)

## Flow

1. **Main agent composes team config** (JSON). See `.claude/teams/schema.json` for the shape.
2. **Main agent shows config to developer** for approval (this is non-trivial compute/time; get a ✓ before spending it).
3. **Main agent invokes the orchestrator:**
   ```bash
   # One-time setup (only needed first run, or after package.json changes)
   (cd .claude/teams/orchestrator && npm install)

   # Run a team
   node .claude/teams/orchestrator/index.js <path-to-team-config.json>
   ```
4. **Dashboard opens in Chrome automatically.** The developer observes, posts to the shared thread, DMs specific agents, or stops the team.
5. **On completion**, `result.json` and `thread.md` land in `.team-runs/<run-id>/`. Main agent reads them to synthesize back to the developer.

## Team config shape (minimal example)

```json
{
  "name": "pre-pr-review",
  "purpose": "Multi-perspective review of the pending changes",
  "target": { "base": "main" },
  "context_files": [".team-runs/pre-pr-review/diff.md"],
  "parallel": true,
  "max_iterations_per_agent": 10,
  "max_total_minutes": 15,
  "agents": [
    {
      "name": "security-reviewer",
      "motivation": "Find auth bypasses, injection risks, hardcoded secrets",
      "model": "sonnet",
      "prompt": "Read context/diff.md and flag any security concerns in the staged changes. Post findings to the shared thread."
    },
    {
      "name": "simplicity-advocate",
      "motivation": "Find over-abstraction, single-use helpers, dead code",
      "model": "haiku",
      "prompt": "Read context/diff.md. For each changed file, identify any slop (single-use helpers, bloated docstrings, unnecessary abstraction). Post findings to the shared thread."
    }
  ]
}
```

## Important constraints (from harness-v2 spec §8 + §21)

- **Agent names must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$`** — enforced by the orchestrator; invalid names throw before any git/file op (command injection defense).
- **`context_files` are mandatory for anything outside the worktree.** Claude Code sandbox-protects `.claude/` and everything outside the worktree. The orchestrator copies context files into `<run-dir>/context/` and agents read from there.
- **Run state in `.team-runs/`** (gitignored), not `.claude/runs/` — `.claude/` is sandbox-protected and agents can't write to it.
- **Worktrees are kept after the run** — inspect diffs in `.team-runs/<run-id>/worktrees/<agent>/`. Clean up manually when done: `git worktree remove <path>`.

## Cognitive burden guidance

The developer never types `/deploy-team` — the main agent decides to summon. The main agent is responsible for:
1. Recognising an ambiguous-enough problem
2. Composing a good team config (2–4 agents usually, distinct lenses)
3. Showing the config for approval
4. Synthesizing results back

When the main agent is uncertain whether to deploy a team, a good heuristic: "Would I want three humans with different backgrounds weighing in?" If yes → team. If the question is just "which of two obvious options" → don't team, just pick and explain.

## Dependencies

- Node.js ≥ 20 (confirmed: v25 on this machine)
- `npm install` in `.claude/teams/orchestrator/` — installs `express`
- macOS: uses `open` to launch Chrome
- Linux: uses `xdg-open`
