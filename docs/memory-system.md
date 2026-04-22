# Memory system

**Goal:** corrections captured once compound into loaded rules automatically. No manual steps, no remembering to type `/publish-memory`, no reliance on agent judgment to save.

**Architecture:** single SQLite DB at `~/.claude/memory/memory.db`. Two tiers in one table via the `project` column (`NULL` = global, repo-name = per-repo). Adapted from pro-workflow's design with fixes for their known gaps (no dedup, no auto-tiering, no context-threshold capture).

## How rules reach your context

1. On **SessionStart**, a user-level hook queries the DB for active rules relevant to this repo (repo-tier for this repo + all global-tier). Writes them to `<repo>/.claude/memory/rules-compiled.md`.
2. `CLAUDE.md` links that file as the session's active rules. It auto-loads when you open CLAUDE.md in context at session start.

## How new rules get captured

1. When the assistant emits a `[LEARN]` block in a response:
   ```
   [LEARN] <category-slug>: One-line rule
   Mistake: What went wrong
   Correction: What the right approach is
   ```
2. The `Stop` hook in cascade (`.claude/hooks/learn-capture.sh`) parses blocks from the final response.
3. It calls `~/.claude/scripts/memory/insert.py` which:
   - Normalizes the rule text (lowercase, collapse whitespace, strip surrounding punctuation)
   - Checks for an active duplicate in the same project → if found, increments `hit_count` instead of inserting (automatic dedup)
   - Otherwise inserts a new row at `project = <repo-name>` (repo-tier by default)
4. Fenced code blocks in the response are stripped before parsing — `[LEARN]` examples in docs/chat don't false-capture.

## How rules get promoted to global

Empirical, not asked: when the same normalized rule appears across **3+ distinct repos**, the consolidator promotes it to global tier automatically and marks the repo-tier copies as `superseded_by` the new global row.

Run via `~/.claude/scripts/memory/consolidate.py` manually or on a schedule. It's idempotent — re-running after a promotion is a no-op.

## Context-threshold save (PreCompact backstop)

When Claude Code auto-compacts a session (context gets large), the `PreCompact` hook (`~/.claude/hooks/memory-pre-compact.sh`) sweeps the full transcript for any `[LEARN]` blocks the per-turn Stop hook missed. Inserts land in SQLite before compaction eats the raw text. No agent judgment needed, no token-count watcher.

## Files

| Path | Purpose |
|------|---------|
| `~/.claude/memory/memory.db` | The SQLite store (single DB, two tiers) |
| `~/.claude/scripts/memory/lib.py` | Shared insert/dedup/promote logic |
| `~/.claude/scripts/memory/insert.py` | CLI: insert from a text blob on stdin |
| `~/.claude/scripts/memory/migrate-jsonl.py` | One-shot: legacy JSONL + team/*.md → SQLite |
| `~/.claude/scripts/memory/surface.py` | Write `rules-compiled.md` for a project |
| `~/.claude/scripts/memory/consolidate.py` | Deep pass: promote cross-repo rules to global |
| `~/.claude/scripts/memory/extract-from-transcript.py` | Parse a transcript file, insert found blocks |
| `~/.claude/hooks/memory-session-start.sh` | SessionStart: migrate legacy + surface rules |
| `~/.claude/hooks/memory-pre-compact.sh` | PreCompact: sweep transcript for missed blocks |
| `.claude/hooks/learn-capture.sh` (repo) | Stop hook: per-turn `[LEARN]` capture |

## Schema

One table `memories` with columns: `id, ts, project, category, rule, rule_normalized, mistake, correction, origin_session, origin_repo_path, hit_count, last_hit, superseded_by, status`. FTS5 available but not wired yet (future work). Full DDL in `~/.claude/scripts/memory/lib.py::SCHEMA_SQL`.

## Legacy migration

On first SessionStart after install, `migrate-jsonl.py` imports any existing:
- `.claude/memory/corrections/<dev>/corrections.jsonl` → repo-tier rows
- `.claude/memory/team/*.md` → global-tier rows (parses `**Rule:**` lines)

Source files get a `.migrated` suffix so the migration is idempotent.

## Manual queries (if needed)

```bash
# Top 20 rules for this repo + globals
python3 ~/.claude/scripts/memory/surface.py --repo "$(pwd)" --limit 20

# Dry-run: what would promote to global?
python3 ~/.claude/scripts/memory/consolidate.py --dry-run

# Raw SQL
sqlite3 ~/.claude/memory/memory.db "SELECT category, rule, hit_count FROM memories WHERE status='active' ORDER BY hit_count DESC LIMIT 10"
```
