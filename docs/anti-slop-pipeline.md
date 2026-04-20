# Anti-Slop Pipeline

**Hard gate before every `git commit`.** Blocks the commit if the `/clean` skill hasn't been run since the last change to staged files.

## Three steps

### 1. Static cleanup (fast, auto-fix)
- Python (`agents/`, `lib/`, `server/`): `ruff check --select F401,F811,F841,C901 --fix` + `vulture --min-confidence 80`
- JS (`frontend/`): `knip` (if installed), manual review otherwise
- Complexity: max function length 80 lines, max cyclomatic complexity 8 (Python)
- Applies to **changed files on the current branch**, not the entire codebase

### 2. Anthropic's `code-simplifier` plugin
```bash
claude plugin install code-simplifier
```
Reduces nesting, eliminates redundancy, improves naming, replaces nested ternaries. Review diff before accepting.

### 3. AI slop audit
The skill runs an AI review pass on changed files, looking for:
- Restating comments (`# foo_bar does foo_bar` above `def foo_bar`)
- Single-use helpers (inline them)
- Unnecessary error handling (catch-rethrow without adding context)
- Debug artifacts (`print(`, `console.log` in non-test files)
- Bloated docstrings that repeat the function signature
- Over-abstraction serving hypothetical future needs
- Unnecessary type annotations

## Sentinel file enforcement

On successful pass, `/clean` writes `.claude/.state/clean-ok-<staged-sha>`.

The `pre-commit-gate.sh` hook (PreToolUse, matcher `Bash`) detects `git commit` commands and:
1. Computes the current staged files' hash
2. Checks if `.claude/.state/clean-ok-<hash>` exists
3. If not → exits 2, blocks the commit, tells the main agent to run `/clean` first

This is how we keep cognitive burden off the developer — they never have to remember `/clean`. The hook blocks until it runs.

## Thresholds (NEW code only)

| Scope | Max function lines | Max cyclomatic complexity |
|-------|-------------------|--------------------------|
| Python | 80 | 8 |
| JavaScript (frontend) | 50 | 8 |

Existing code that exceeds these is not flagged (would be noise). The check runs only on files modified on the current branch.

## The `/clean` skill

See `.claude/skills/clean/SKILL.md`. Invoked automatically by the main agent before any commit; the gate hook enforces.
