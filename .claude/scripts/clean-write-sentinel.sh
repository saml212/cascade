#!/usr/bin/env bash
# Write the clean-ok sentinel for the current staged set.
# Called by the /clean skill after all checks pass.

set -eu

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

STAGED_HASH="$(git diff --cached --name-only | sort | \
  xargs -I{} sh -c 'git ls-files -s "{}" 2>/dev/null' | \
  shasum -a 256 | cut -d' ' -f1)"

if [ -z "$STAGED_HASH" ]; then
  echo "ℹ  clean: nothing staged — no sentinel needed"
  exit 0
fi

mkdir -p .claude/.state
SENTINEL=".claude/.state/clean-ok-$STAGED_HASH"
touch "$SENTINEL"
echo "✓ clean: sentinel written ($STAGED_HASH)"
echo "   pre-commit-gate.sh will now allow 'git commit' on this staged set"
