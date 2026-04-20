# Self-Correcting Memory System

**Goal:** corrections compound into loaded rules so the same mistake doesn't happen twice.

Inspired by pro-workflow, but spec-faithful (§6 of harness-v2 spec): JSONL + git-based, no SQLite, no npm dependency. Auditable with `cat`.

## Two tiers

### Tier 1 — Developer corrections (private, per-dev)
- Path: `.claude/memory/corrections/<developer>/corrections.jsonl`
- **Gitignored.** Never shared.
- Auto-captured: when the assistant emits a `[LEARN]` block, the `Stop` hook parses it and appends a record.
- Compiled to `rules-compiled.md` on demand (or session start).

### Tier 2 — Team memory (shared, permission-gated)
- Path: `.claude/memory/team/<slug>.md`
- **Committed to git.**
- Index: `.claude/memory/MEMORY-TEAM.md`
- Published via `/publish-memory` skill — requires explicit developer approval before a correction promotes from Tier 1 to Tier 2.

## The `[LEARN]` block convention

When the assistant is corrected or discovers a mistake, it emits this block in its final response:

```
[LEARN] Category: One-line rule summary
Mistake: What went wrong, concretely
Correction: What the right approach is
```

The block's *presence* is the consent signal. No extra confirmation needed for capture.

## JSONL record shape

```json
{
  "id": "corr_20260420_abcd",
  "timestamp": "2026-04-20T10:00:00Z",
  "developer": "slarson",
  "category": "ffprobe-usage",
  "rule": "All ffprobe calls go through lib/ffprobe, never raw subprocess",
  "mistake": "Called subprocess.run(['ffprobe', ...]) in agents/ingest.py",
  "correction": "Use lib.ffprobe.probe() and helpers",
  "promoted_to_team": false
}
```

## Hooks

| Hook | Event | What |
|------|-------|------|
| `learn-capture.sh` | `Stop` | Parses `[LEARN]` blocks from the assistant's final response, appends to JSONL |
| `correction-detect.sh` | `UserPromptSubmit` | Nudges when correction language detected (so the assistant knows to emit a `[LEARN]` block) |

## Compile & load

```bash
bash .claude/scripts/compile-corrections.sh
# Reads corrections.jsonl + team/*.md → writes rules-compiled.md
```

CLAUDE.md references `rules-compiled.md` so it loads at session start.

## Promotion workflow (`/publish-memory`)

1. Developer invokes `/publish-memory` (or the assistant suggests after seeing the same correction 2+ times)
2. Skill shows the correction as a proposed team memory
3. Developer approves → file written to `.claude/memory/team/<slug>.md`
4. Commit with attribution
5. Team sees it on next `git pull`
