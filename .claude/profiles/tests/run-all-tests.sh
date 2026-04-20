#!/usr/bin/env bash
# Hook test suite — calls each hook as a subprocess with known JSON input
# and checks exit codes + stdout patterns.
#
# Run directly: bash .claude/profiles/tests/run-all-tests.sh
#
# NOTE: running this via Claude Code's Bash tool will itself trigger the
# hooks (on the test runner command). That's fine — the test fixtures
# don't match any guard patterns. But if you hit weird blocks, run in
# your host shell outside Claude Code.

set -u

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

PASS=0
FAIL=0

# ── test helper ─────────────────────────────────────────────────────────────
check() {
  local label="$1"
  local expected_exit="$2"
  local actual_exit="$3"
  local stdout="$4"
  if [ "$expected_exit" = "$actual_exit" ]; then
    echo "  ✅ PASS  [$label]  exit=$actual_exit"
    PASS=$((PASS + 1))
  else
    echo "  ❌ FAIL  [$label]  exit=$actual_exit (expected $expected_exit)"
    echo "    stdout: $stdout"
    FAIL=$((FAIL + 1))
  fi
}

run_hook() {
  local hook="$1"
  local input="$2"
  local out
  out="$(echo "$input" | bash ".claude/hooks/$hook" 2>&1)"
  echo "$?|$out"
}

# ── safety-check.sh ─────────────────────────────────────────────────────────
echo ""
echo "── safety-check.sh ────────────────────────────────────────────────────"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"rm -rf /Volumes/1TB_SSD/cascade/episodes/ep_foo"}}')"
check "BLOCK: rm -rf on episode data" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"rm -fr /Volumes/1TB_SSD/cascade/episodes/ep_foo"}}')"
check "BLOCK: rm -fr flag-reversed on episode data" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"rm -r -f /Volumes/1TB_SSD/cascade/episodes/ep_foo"}}')"
check "BLOCK: rm -r -f separate flags" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"rm -rf /tmp/scratch"}}')"
check "ALLOW: rm on non-protected path" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git push origin main --force"}}')"
check "BLOCK: git push --force main" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git push origin main -f"}}')"
check "BLOCK: git push -f main" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git push origin main --force-with-lease"}}')"
check "ALLOW: force-with-lease to main" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git push origin feature-branch --force"}}')"
check "ALLOW: force push to feature branch" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git push origin main"}}')"
check "ALLOW: normal push to main" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git add .env"}}')"
check "BLOCK: git add .env (secret)" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git add .env.example"}}')"
check "ALLOW: git add .env.example" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"git reset --hard main"}}')"
check "BLOCK: git reset --hard main" 2 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"cat /Volumes/1TB_SSD/cascade/episodes/ep_foo/episode.json"}}')"
check "ALLOW: cat on episode (no rm)" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook safety-check.sh '{"tool_input":{"command":"echo \"rm -rf /Volumes/1TB_SSD/cascade/episodes/x in echo string\""}}')"
check "ALLOW: echo containing rm pattern (quoted string)" 0 "${result%%|*}" "${result#*|}"

# ── route-format.sh ─────────────────────────────────────────────────────────
echo ""
echo "── route-format.sh ────────────────────────────────────────────────────"

# This hook is PostToolUse and fail-open; we just verify it runs without error
# and matches the right language.
result="$(run_hook route-format.sh '{"tool_input":{"file_path":"/Users/samuellarson/Local/Github/cascade/agents/clip_miner.py"}}')"
exit_code="${result%%|*}"
out="${result#*|}"
if [ "$exit_code" = "0" ]; then
  if echo "$out" | grep -q "python"; then
    echo "  ✅ PASS  [route .py → python] exit=0, output: $(echo "$out" | head -1)"
    PASS=$((PASS + 1))
  else
    echo "  ⚠️  PASS  [route .py → python] exit=0 but no 'python' in output (ruff may not be installed): $out"
    PASS=$((PASS + 1))
  fi
else
  echo "  ❌ FAIL  [route .py → python] exit=$exit_code"
  FAIL=$((FAIL + 1))
fi

result="$(run_hook route-format.sh '{"tool_input":{"file_path":"/Users/samuellarson/Local/Github/cascade/README.md"}}')"
check "SILENT: .md file (no language match)" 0 "${result%%|*}" "${result#*|}"

# ── pre-commit-gate.sh ──────────────────────────────────────────────────────
echo ""
echo "── pre-commit-gate.sh ─────────────────────────────────────────────────"

result="$(run_hook pre-commit-gate.sh '{"tool_input":{"command":"ls -la"}}')"
check "ALLOW: non-commit command" 0 "${result%%|*}" "${result#*|}"

result="$(run_hook pre-commit-gate.sh '{"tool_input":{"command":"CLEAN_BYPASS=1 git commit -m test"}}')"
check "ALLOW: commit with CLEAN_BYPASS=1" 0 "${result%%|*}" "${result#*|}"

# Actual `git commit -m test` — expect BLOCK (no sentinel exists for current staged set)
# Only meaningful if there's something staged
if [ -n "$(git diff --cached --name-only)" ]; then
  result="$(run_hook pre-commit-gate.sh '{"tool_input":{"command":"git commit -m test"}}')"
  check "BLOCK: git commit without /clean sentinel" 2 "${result%%|*}" "${result#*|}"
else
  echo "  ℹ️  SKIP  [git commit without sentinel] — nothing staged to test against"
fi

# ── correction-detect.sh ────────────────────────────────────────────────────
echo ""
echo "── correction-detect.sh ───────────────────────────────────────────────"

result="$(run_hook correction-detect.sh '{"prompt":"that'"'"'s wrong, you should have used lib/ffprobe"}')"
exit_code="${result%%|*}"
out="${result#*|}"
if [ "$exit_code" = "0" ] && echo "$out" | grep -q "LEARN"; then
  echo "  ✅ PASS  [NUDGE: correction language triggers [LEARN] reminder]"
  PASS=$((PASS + 1))
else
  echo "  ❌ FAIL  [NUDGE: correction language] exit=$exit_code, output: $out"
  FAIL=$((FAIL + 1))
fi

result="$(run_hook correction-detect.sh '{"prompt":"add a new agent that does X"}')"
exit_code="${result%%|*}"
out="${result#*|}"
if [ "$exit_code" = "0" ] && ! echo "$out" | grep -q "LEARN"; then
  echo "  ✅ PASS  [SILENT: neutral prompt (no nudge)]"
  PASS=$((PASS + 1))
else
  echo "  ❌ FAIL  [SILENT: neutral prompt] exit=$exit_code, output: $out"
  FAIL=$((FAIL + 1))
fi

# ── learn-capture.sh ────────────────────────────────────────────────────────
echo ""
echo "── learn-capture.sh ───────────────────────────────────────────────────"

# Create a fake transcript file with [LEARN] blocks. Use a recognizable
# sentinel category so we can remove the test entry afterward.
TRANSCRIPT=$(mktemp)
TEST_SENTINEL="hook-test-sentinel-$$"
cat > "$TRANSCRIPT" <<TRANSCRIPT_EOF
{"role":"user","content":"test"}
{"role":"assistant","content":[{"type":"text","text":"Here is what I learned:\n\n[LEARN] ${TEST_SENTINEL}: always use lib/foo\nMistake: used raw subprocess instead of lib/foo helper\nCorrection: call lib.foo.bar() instead"}]}
TRANSCRIPT_EOF

result="$(echo "{\"transcript_path\":\"$TRANSCRIPT\"}" | bash .claude/hooks/learn-capture.sh 2>&1; echo "EXIT:$?")"
exit_code="$(echo "$result" | awk 'END{print}' | cut -d: -f2)"
out="$(echo "$result" | sed '$d')"

DEV_SLUG="$(git config user.email | cut -d@ -f1 | tr -cd 'a-zA-Z0-9._-')"
CORRECTIONS_FILE=".claude/memory/corrections/${DEV_SLUG}/corrections.jsonl"

if [ "$exit_code" = "0" ] && [ -f "$CORRECTIONS_FILE" ] && grep -q "$TEST_SENTINEL" "$CORRECTIONS_FILE"; then
  echo "  ✅ PASS  [CAPTURE: [LEARN] block saved to corrections.jsonl]"
  PASS=$((PASS + 1))
else
  echo "  ❌ FAIL  [CAPTURE: [LEARN] block] exit=$exit_code"
  echo "    output: $out"
  echo "    corrections file: $CORRECTIONS_FILE"
  [ -f "$CORRECTIONS_FILE" ] && echo "    contents: $(cat "$CORRECTIONS_FILE")"
  FAIL=$((FAIL + 1))
fi

# Clean up the test entry from the real corrections file
if [ -f "$CORRECTIONS_FILE" ]; then
  grep -v "$TEST_SENTINEL" "$CORRECTIONS_FILE" > "$CORRECTIONS_FILE.tmp" || true
  mv "$CORRECTIONS_FILE.tmp" "$CORRECTIONS_FILE"
  # If the file is now empty, remove it and the dev dir
  [ ! -s "$CORRECTIONS_FILE" ] && rm -f "$CORRECTIONS_FILE" && rmdir ".claude/memory/corrections/${DEV_SLUG}" 2>/dev/null || true
fi
rm -f "$TRANSCRIPT"

# ── compile-corrections.sh ──────────────────────────────────────────────────
echo ""
echo "── compile-corrections.sh ─────────────────────────────────────────────"

result="$(bash .claude/scripts/compile-corrections.sh 2>&1; echo "EXIT:$?")"
exit_code="$(echo "$result" | awk 'END{print}' | cut -d: -f2)"
if [ "$exit_code" = "0" ] && [ -f ".claude/memory/rules-compiled.md" ]; then
  echo "  ✅ PASS  [COMPILE: writes rules-compiled.md]"
  PASS=$((PASS + 1))
else
  echo "  ❌ FAIL  [COMPILE] exit=$exit_code, output: $result"
  FAIL=$((FAIL + 1))
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "======================================================================"
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" = "0" ] && exit 0 || exit 1
