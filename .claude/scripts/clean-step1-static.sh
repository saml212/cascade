#!/usr/bin/env bash
# /clean Step 1: static cleanup on changed files.
#
# Reads .claude/profile.json for language configs. For each changed file,
# runs the matching language's format + lint-fix + dead-code commands.
# Reports what was auto-fixed and what still needs manual attention.
#
# Exit codes:
#   0 — clean (no issues, OR only auto-fixed issues)
#   1 — genuine issues remain (blocks sentinel write)
#   (tool-missing is not a failure; logged as SKIPPED)

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

# PATH expansion (hooks / skills run with minimal PATH)
for p in "$HOME/go/bin" "$HOME/.local/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
  [ -d "$p" ] && export PATH="$p:$PATH"
done
[ -d "$REPO_ROOT/.venv/bin" ] && export PATH="$REPO_ROOT/.venv/bin:$PATH"

PROFILE="$REPO_ROOT/.claude/profile.json"
command -v jq >/dev/null 2>&1 || { echo "clean: jq required"; exit 0; }
[ -f "$PROFILE" ] || { echo "clean: profile.json missing"; exit 0; }

# ── Collect STAGED files only ───────────────────────────────────────────────
# /clean operates on what the developer has explicitly staged for commit.
# This keeps scope aligned with pre-commit-gate (which hashes the staged
# set) and prevents touching in-progress work the developer hasn't staged.
# Workflow: `git add <files> && /clean && git commit`
STAGED="$(git diff --cached --name-only | sort -u | grep -v '^$' || true)"

if [ -z "$STAGED" ]; then
  echo "ℹ  clean-step1: nothing staged — stage files first (git add), then re-run /clean"
  exit 0
fi

ISSUES=0
SKIPPED=""

# ── For each changed file, route to the right language's commands ───────────
while IFS= read -r f; do
  [ -z "$f" ] && continue
  [ -f "$f" ] || continue

  ext=".${f##*.}"

  # Find language profile for this extension
  lang="$(jq -r --arg ext "$ext" '
    .languages
    | to_entries
    | map(select(.value.extensions | index($ext)))
    | first // empty
    | .key // empty
  ' "$PROFILE" 2>/dev/null)"
  [ -z "$lang" ] && continue

  # Check path filter
  path_match="$(jq -r --arg lang "$lang" --arg rel "$f" '
    .languages[$lang].paths[]?
    | select($rel | startswith(.))
    | "yes"
  ' "$PROFILE" 2>/dev/null | head -n1)"
  [ "$path_match" != "yes" ] && continue

  # Read commands
  fmt_cmd="$(jq -r --arg lang "$lang" '.languages[$lang].format_command // empty' "$PROFILE")"
  lint_cmd="$(jq -r --arg lang "$lang" '.languages[$lang].lint_fix_command // empty' "$PROFILE")"
  dead_cmd="$(jq -r --arg lang "$lang" '.languages[$lang].dead_code_command // empty' "$PROFILE")"

  # Run each command. Auto-fix commands go quiet; report non-zero exits as issues.
  run() {
    local cmd_template="$1"; local label="$2"
    [ -z "$cmd_template" ] || [ "$cmd_template" = "null" ] && return 0

    local first_token="${cmd_template%% *}"
    local tool_name="${first_token##*/}"

    # Allowlist check (same as route-format.sh)
    local allowlist
    allowlist="$(jq -r '.safety.format_allowlist[]?' "$PROFILE")"
    if ! echo "$allowlist" | grep -qx "$tool_name"; then
      echo "clean: '$tool_name' not in allowlist, skipping $label for $f" >&2
      return 0
    fi

    # Tool installed?
    if ! command -v "$first_token" >/dev/null 2>&1 && ! command -v "$tool_name" >/dev/null 2>&1; then
      SKIPPED="${SKIPPED:+$SKIPPED, }$tool_name"
      return 0
    fi

    local quoted
    quoted="$(printf '%q' "$f")"
    local expanded="${cmd_template//\{file\}/$quoted}"

    local out rc
    out="$(cd "$REPO_ROOT" && eval "$expanded" 2>&1)"
    rc=$?
    if [ "$rc" != "0" ] && [ -n "$out" ]; then
      echo "◦ $label ($tool_name) flagged $f:"
      echo "$out" | sed 's/^/    /'
      ISSUES=$((ISSUES + 1))
    fi
  }

  run "$fmt_cmd"  "format"
  run "$lint_cmd" "lint-fix"
  run "$dead_cmd" "dead-code"

done <<< "$STAGED"

# ── Report ──────────────────────────────────────────────────────────────────
echo ""
if [ -n "$SKIPPED" ]; then
  # Dedupe
  SKIPPED_U="$(echo "$SKIPPED" | tr ',' '\n' | awk '{$1=$1}1' | sort -u | paste -sd, - | sed 's/,/, /g')"
  echo "ℹ  clean-step1: tools not installed: $SKIPPED_U"
  echo "   → brew install them / add to venv before /clean can enforce all rules"
fi

if [ "$ISSUES" = "0" ]; then
  echo "✓ clean-step1: static checks clean"
  exit 0
else
  echo "✗ clean-step1: $ISSUES file(s) have issues that need fixing"
  exit 1
fi
