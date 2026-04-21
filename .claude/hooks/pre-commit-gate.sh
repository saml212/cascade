#!/usr/bin/env bash
# PreToolUse(Bash) — block `git commit` unless /clean has been run on the
# current staged set. This is how we keep cognitive burden off the developer:
# the main agent must invoke /clean before it tries to commit.
#
# The /clean skill, on successful pass, writes:
#   .claude/.state/clean-ok-<hash>
# where <hash> is a sha256 of the staged file list + their content hashes.
#
# This hook recomputes that hash and checks for the sentinel file.
#
# Spec: harness-v2 §7 (anti-slop as hard gate before PR; for cascade, before commit).

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
command -v jq >/dev/null 2>&1 || exit 0

INPUT="$(cat)"
CMD="$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# Only fire on actual git commit invocations. Delegates splitting to shared
# helper so quoted strings like echo 'git commit foo' don't false-positive.
SPLITTER="$REPO_ROOT/.claude/scripts/split-subcommands.py"
HAS_COMMIT=""
if [ -f "$SPLITTER" ]; then
  # Trust the splitter fully — if it returns zero sub-commands starting with
  # "git commit", there's no commit happening, even if the raw string contains
  # it as a quoted argument. (This was a real bug in v1.)
  while IFS= read -r sub; do
    if echo "$sub" | grep -qE '^git\s+commit\b'; then
      HAS_COMMIT="yes"
      break
    fi
  done < <(python3 "$SPLITTER" "$CMD" 2>/dev/null)
else
  # Splitter missing — fall back to conservative whole-string match
  echo "$CMD" | grep -qE '\bgit\s+commit\b' && HAS_COMMIT="yes"
fi
[ "$HAS_COMMIT" != "yes" ] && exit 0

# Allow initial onboarding / docs-only commits with an explicit bypass
if echo "$CMD" | grep -qE 'CLEAN_BYPASS=1'; then
  echo "ℹ️  pre-commit-gate: CLEAN_BYPASS set — skipping"
  exit 0
fi

# ── Compute staged-files hash ───────────────────────────────────────────────
STAGED_HASH="$(cd "$REPO_ROOT" && git diff --cached --name-only | sort | \
  xargs -I{} sh -c 'git ls-files -s "{}" 2>/dev/null' | \
  shasum -a 256 | cut -d' ' -f1)"

[ -z "$STAGED_HASH" ] && exit 0  # nothing staged → let it proceed (will fail elsewhere)

SENTINEL="$REPO_ROOT/.claude/.state/clean-ok-$STAGED_HASH"

if [ ! -f "$SENTINEL" ]; then
  # Block messages go to stderr so Claude Code shows them to the user
  # (stdout on exit 2 is often swallowed).
  {
    echo "🛑 BLOCKED: /clean hasn't been run on this staged set"
    echo ""
    echo "   Staged files hash: $STAGED_HASH"
    echo "   Expected sentinel: .claude/.state/clean-ok-$STAGED_HASH"
    echo ""
    echo "   Invoke /clean first. The skill will run ruff + vulture + simplifier +"
    echo "   AI slop audit on the changed files, then write the sentinel on pass."
    echo ""
    echo "   To bypass for a docs-only or emergency commit:"
    echo "   CLEAN_BYPASS=1 git commit ..."
  } >&2
  exit 2
fi

echo "✓ pre-commit-gate: sentinel present ($STAGED_HASH)"
exit 0
