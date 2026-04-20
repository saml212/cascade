#!/usr/bin/env bash
# PostToolUse(Write|Edit) — auto-format the modified file based on language.
#
# Reads tool_input.file_path from stdin JSON. Looks up the matching language
# profile in .claude/profile.json, runs the format_command and lint_fix_command.
#
# Safety: the command's first token is validated against safety.format_allowlist
# BEFORE any shell evaluation (prevents supply-chain code exec via config).
# Failure is non-fatal — hook exits 0 on any error. Formatter output goes to
# stdout so Claude sees what was auto-applied.
#
# Spec: harness-v2 §5 + §21 (PATH expansion, allowlist before eval, worktree
# path resolution).

set -u  # intentionally NOT -e; fail open

# ── PATH expansion — hooks run with minimal PATH ─────────────────────────────
for p in "$HOME/go/bin" "$HOME/.local/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
  [ -d "$p" ] && export PATH="$p:$PATH"
done
# Prefer the project's venv over anything else
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if [ -d "$REPO_ROOT/.venv/bin" ]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

PROFILE="$REPO_ROOT/.claude/profile.json"
[ -f "$PROFILE" ] || exit 0

# Need jq
command -v jq >/dev/null 2>&1 || exit 0

# ── Read tool input ─────────────────────────────────────────────────────────
INPUT="$(cat)"
FILE_PATH="$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
[ -z "$FILE_PATH" ] && exit 0
[ -f "$FILE_PATH" ] || exit 0

# Make FILE_PATH relative to repo root for path matching
REL_PATH="$FILE_PATH"
case "$FILE_PATH" in
  "$REPO_ROOT"/*) REL_PATH="${FILE_PATH#$REPO_ROOT/}" ;;
esac

# Extract extension
EXT=".${FILE_PATH##*.}"

# ── Find matching language profile ──────────────────────────────────────────
# Iterate over languages, find one whose extensions contains $EXT
MATCHED_LANG="$(jq -r --arg ext "$EXT" '
  .languages
  | to_entries
  | map(select(.value.extensions | index($ext)))
  | first // empty
  | .key // empty
' "$PROFILE" 2>/dev/null)"

[ -z "$MATCHED_LANG" ] && exit 0

# Check path filters — file must live under one of this language's paths
PATH_MATCH="$(jq -r --arg lang "$MATCHED_LANG" --arg rel "$REL_PATH" '
  .languages[$lang].paths[]?
  | select($rel | startswith(.))
  | "yes"
' "$PROFILE" 2>/dev/null | head -n1)"

[ "$PATH_MATCH" != "yes" ] && exit 0

# ── Collect allowlist ───────────────────────────────────────────────────────
ALLOWLIST="$(jq -r '.safety.format_allowlist[]?' "$PROFILE" 2>/dev/null)"

# ── Run format_command, then lint_fix_command ───────────────────────────────
run_command() {
  local cmd_template="$1"
  [ -z "$cmd_template" ] || [ "$cmd_template" = "null" ] && return 0

  # Extract first token to check against allowlist
  local first_token="${cmd_template%% *}"
  # Strip path components from first_token (e.g., .venv/bin/ruff → ruff)
  local tool_name="${first_token##*/}"

  # Allowlist check
  if ! echo "$ALLOWLIST" | grep -qx "$tool_name"; then
    echo "route-format: skipping unknown tool '$tool_name' (not in allowlist)" >&2
    return 0
  fi

  # Substitute {file} with shell-quoted file path
  local quoted_file
  quoted_file="$(printf '%q' "$FILE_PATH")"
  local expanded="${cmd_template//\{file\}/$quoted_file}"

  # Run it from the repo root
  (cd "$REPO_ROOT" && eval "$expanded") >/dev/null 2>&1
  return 0
}

FORMAT_CMD="$(jq -r --arg lang "$MATCHED_LANG" '.languages[$lang].format_command // empty' "$PROFILE")"
LINT_CMD="$(jq -r --arg lang "$MATCHED_LANG" '.languages[$lang].lint_fix_command // empty' "$PROFILE")"

run_command "$FORMAT_CMD"
run_command "$LINT_CMD"

# Show a one-liner so the assistant sees what happened
echo "✓ route-format: $MATCHED_LANG rules applied to $REL_PATH"

exit 0
