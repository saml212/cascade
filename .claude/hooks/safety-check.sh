#!/usr/bin/env bash
# PreToolUse(Bash) — block destructive commands before they run.
#
# Blocks (exits 2 with message):
#   - rm on episode data directories or other protected paths
#   - git push --force / -f to main/master (all flag orderings)
#   - git commit of likely-secret files (.env, .pem, .key, credentials)
#   - git reset --hard on main/master
#
# Defense-in-depth, not a security boundary — an agent that wants to bypass
# can always use sh -c. Catches accidents, not adversaries. (§21)
#
# Sub-command splitting avoids false positives inside echo/cat/python args.

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PROFILE="$REPO_ROOT/.claude/profile.json"

command -v jq >/dev/null 2>&1 || exit 0
[ -f "$PROFILE" ] || exit 0

INPUT="$(cat)"
CMD="$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)"
[ -z "$CMD" ] && exit 0

# ── Split into sub-commands to avoid matching content inside quoted args ────
# Shell operators that separate independent commands: && || ; | ` $(
# We use a python one-liner for proper splitting (bash regex is fragile here).
SUBS="$(python3 - <<PY 2>/dev/null
import re, sys
cmd = """$(echo "$CMD" | sed 's/"""/"""/g')"""
parts = re.split(r'&&|\|\||;|\||' + '`' + r'|\$\(', cmd)
for p in parts:
    p = p.strip()
    if p:
        print(p)
PY
)"
[ -z "$SUBS" ] && SUBS="$CMD"  # fallback

# ── Guard 1: rm on protected paths ──────────────────────────────────────────
BLOCK_PATHS="$(jq -r '.safety.block_rm_paths[]?' "$PROFILE" 2>/dev/null)"
while IFS= read -r sub; do
  # All rm flag orderings: -rf, -fr, -Rf, -fR, -r -f, -f -r, --recursive --force, etc.
  if echo "$sub" | grep -qE '^(sudo +)?rm(\s+-[a-zA-Z]+)*(\s+-[a-zA-Z]+)*\s'; then
    while IFS= read -r protected; do
      [ -z "$protected" ] && continue
      if echo "$sub" | grep -qF "$protected"; then
        echo "🛑 BLOCKED: rm on protected path"
        echo "   Sub-command: $sub"
        echo "   Protected:   $protected"
        echo "   These paths hold irreplaceable data. Delete manually in Finder if you're sure."
        exit 2
      fi
    done <<< "$BLOCK_PATHS"
  fi
done <<< "$SUBS"

# ── Guard 2: git push --force / -f to main/master ───────────────────────────
BLOCK_BRANCHES="$(jq -r '.safety.block_force_push_branches[]?' "$PROFILE" 2>/dev/null)"
while IFS= read -r sub; do
  if echo "$sub" | grep -qE '^git\s+push\b'; then
    # Allow --force-with-lease (safe)
    if echo "$sub" | grep -q -- '--force-with-lease'; then
      continue
    fi
    # Detect --force or -f anywhere after "push"
    if echo "$sub" | grep -qE '\s(--force|-f)(\s|$)'; then
      # Check branch: explicit main/master OR bare (unknown target = conservative)
      is_blocked=0
      while IFS= read -r branch; do
        [ -z "$branch" ] && continue
        if echo "$sub" | grep -qE "(\s|/)${branch}(\s|$)"; then
          is_blocked=1
          break
        fi
      done <<< "$BLOCK_BRANCHES"

      # Bare `git push --force` with no explicit branch/remote → conservative block
      # Positional args (non-flag tokens): git push [remote] [refspec]
      positional_count="$(echo "$sub" | awk '{for(i=1;i<=NF;i++) if($i !~ /^-/) print $i}' | wc -l | tr -d ' ')"
      if [ "$positional_count" -lt 4 ]; then
        is_blocked=1
      fi

      if [ "$is_blocked" = "1" ]; then
        echo "🛑 BLOCKED: force push to main/master (or unspecified target)"
        echo "   Sub-command: $sub"
        echo "   Never force push to main. Create a revert commit, or use --force-with-lease on a feature branch."
        exit 2
      fi
    fi
  fi
done <<< "$SUBS"

# ── Guard 3: git add of likely secrets ──────────────────────────────────────
while IFS= read -r sub; do
  if echo "$sub" | grep -qE '^git\s+add\b'; then
    if echo "$sub" | grep -qE '\.(env|pem|key|crt|pfx|p12)\b|credentials|\.aws/|\.ssh/'; then
      # .env.example is fine (it's a template)
      if ! echo "$sub" | grep -qE '\.env\.example\b|\.env\.sample\b'; then
        echo "🛑 BLOCKED: git add of likely-secret file"
        echo "   Sub-command: $sub"
        echo "   Add secrets to .gitignore and commit them out-of-band if truly needed."
        exit 2
      fi
    fi
  fi
done <<< "$SUBS"

# ── Guard 4: git reset --hard on main/master ────────────────────────────────
while IFS= read -r sub; do
  if echo "$sub" | grep -qE '^git\s+reset\s+--hard'; then
    while IFS= read -r branch; do
      [ -z "$branch" ] && continue
      if echo "$sub" | grep -qE "(\s|/)${branch}(\s|$)"; then
        echo "🛑 BLOCKED: git reset --hard on $branch"
        echo "   Sub-command: $sub"
        echo "   This destroys uncommitted work. Use git stash or create a branch first."
        exit 2
      fi
    done <<< "$BLOCK_BRANCHES"
  fi
done <<< "$SUBS"

exit 0
