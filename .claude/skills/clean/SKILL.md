---
name: clean
description: Anti-slop pipeline — runs ruff + vulture (static cleanup), code-simplifier plugin (if installed), and an AI slop audit on changed files. On pass, writes the `.claude/.state/clean-ok-<hash>` sentinel that pre-commit-gate.sh requires. MUST run before every `git commit`. Triggers on commit intent, before PRs, "clean up this code", "prep for commit".
---

# /clean — anti-slop pipeline before commit

The main agent invokes this **before any `git commit`**. The pre-commit-gate hook blocks commits until the sentinel file is present for the current staged-set hash.

## Design (general, cascade-scoped)
- Works on any set of changed files on the branch (not hardcoded to `agents/`).
- Static commands come from `.claude/profile.json` `anti_slop.static.*` (so cascade's Python tools are the default, but the same structure works for any language added later).
- The AI audit prompt is general slop patterns (not cascade-specific checks).

## Steps — run in order, stop on failure

### Step 0: scope — staged files only
```bash
cd "$(git rev-parse --show-toplevel)"
STAGED=$(git diff --cached --name-only | sort -u | grep -v '^$' || true)
```

**Scope is staged-only**, matching what pre-commit-gate hashes. If the
developer says "clean this code" and hasn't staged, tell them to stage first:
`git add <files>`.

This design prevents auto-fixing in-progress work the developer hasn't
explicitly signaled readiness for. It also means /clean cleans exactly the
bytes that will be committed.

### Step 1: static cleanup (ruff + vulture for Python; knip for JS)

Invoke `.claude/scripts/clean-step1-static.sh` — it reads `profile.json`, runs
the configured commands on changed files only, auto-fixes what's safe, and
reports remaining issues.

**Failure mode:** if ruff/vulture find issues that can't auto-fix, or if
they're not installed, the step reports and continues (tools-missing doesn't
block; genuine issues block). You MUST fix genuine issues before the skill
writes the sentinel.

### Step 2: code-simplifier plugin (optional)
```bash
if claude plugin list 2>/dev/null | grep -q code-simplifier; then
  # Plugin docs: run on each changed file via its CLI entry
  # (exact invocation depends on plugin version — check with claude plugin info code-simplifier)
  echo "ℹ  code-simplifier plugin detected — review its suggestions before accepting"
else
  echo "ℹ  code-simplifier not installed (claude plugin install code-simplifier) — skipping"
fi
```

### Step 3: AI slop audit

Read each changed file and check for these patterns (**ONLY** flag what
you find — don't pattern-match against a checklist if the code is clean):

**Restating comments** — a comment that just restates what the code already says:
```python
# Bad
# CreateSession creates a session
def create_session(): ...

# Fine — explains WHY
def create_session():
    # Session IDs must be UUIDv7 so ordering matches creation time.
    ...
```

**Single-use helpers** — a function defined, called exactly once, adds no clarity:
```python
# Bad
def _is_valid_id(x): return isinstance(x, str) and len(x) > 0
if _is_valid_id(id): ...

# Fine — inline it
if isinstance(id, str) and len(id) > 0: ...
```

**Unnecessary error handling** — catch-rethrow without adding context:
```python
# Bad
try:
    foo()
except Exception as e:
    raise e
```

**Debug artifacts** in non-test files:
- `print(` in `agents/`, `lib/`, `server/` (use `logger`)
- `console.log(` in `frontend/` non-test files

**Bloated docstrings** that repeat the signature without adding info.

**Over-abstraction** serving hypothetical future needs (e.g., a factory class that will only ever produce one type).

**Don't flag:**
- Existing code that wasn't touched — we gate new code only
- Conventional boilerplate (dataclass definitions, FastAPI route shapes, etc.)
- `print()` in test files or scripts (fine)
- Logging statements that provide real diagnostics

Report findings per file with a recommended fix. The main agent (or dispatched specialist) applies them.

### Step 4: write sentinel on pass

After all steps pass (or only had non-blocking skips like tools-missing):

```bash
STAGED_HASH="$(git diff --cached --name-only | sort | xargs -I{} sh -c 'git ls-files -s "{}" 2>/dev/null' | shasum -a 256 | cut -d' ' -f1)"
mkdir -p .claude/.state
touch ".claude/.state/clean-ok-$STAGED_HASH"
echo "✓ /clean: sentinel written — pre-commit-gate will allow commit"
```

**Important:** the sentinel must match the CURRENT staged set. If the
developer stages more files after /clean runs, the hash changes and the
sentinel is stale — /clean must run again.

## Failure discipline

If step 1 has genuine issues you can't auto-fix OR step 3 finds real slop:
- Do NOT write the sentinel.
- Report every finding clearly.
- Suggest the fix (or dispatch python-specialist / frontend-specialist to apply it).
- Re-run /clean after fixes.

## When the main agent invokes /clean

- Before any planned `git commit`.
- When the developer says "clean up this code", "prep for commit", "review before commit".
- After specialist work completes, as the gate.

**Don't** invoke /clean speculatively before work is finished — it runs against the current diff, so it's most useful *right before commit*.
