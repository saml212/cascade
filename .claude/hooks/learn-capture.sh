#!/usr/bin/env bash
# Stop hook — parse [LEARN] blocks from the assistant's final response and
# append them to the developer's corrections JSONL.
#
# Input JSON on stdin: has transcript_path pointing to the session transcript.
# We read the latest assistant response from it, grep for [LEARN] blocks,
# and write JSONL records to .claude/memory/corrections/<developer>/corrections.jsonl
#
# Spec: harness-v2 §6 + §21 (sanitize developer identifier).
# Fail-open: any error → exit 0, log is lost but hook never breaks flow.

set -u

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
command -v jq >/dev/null 2>&1 || exit 0

# ── Sanitize developer identifier (path safety) ─────────────────────────────
DEVELOPER_RAW="$(git config user.email 2>/dev/null | cut -d@ -f1 || whoami 2>/dev/null || echo unknown)"
DEVELOPER="$(echo "$DEVELOPER_RAW" | tr -cd 'a-zA-Z0-9._-')"
[ -z "$DEVELOPER" ] && DEVELOPER="unknown"

CORRECTIONS_DIR="$REPO_ROOT/.claude/memory/corrections/$DEVELOPER"
mkdir -p "$CORRECTIONS_DIR"
CORRECTIONS_FILE="$CORRECTIONS_DIR/corrections.jsonl"

# ── Find the most recent assistant response ────────────────────────────────
INPUT="$(cat)"
TRANSCRIPT_PATH="$(echo "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null)"

# Source of text to scan for [LEARN] blocks:
# If transcript_path is present, read last assistant message; else scan input itself.
TEXT=""
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  # Transcript is typically JSONL; extract last assistant message's text content
  TEXT="$(python3 - "$TRANSCRIPT_PATH" <<'PY' 2>/dev/null
import json, sys
last_text = []
try:
    with open(sys.argv[1]) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                msg = json.loads(line)
            except: continue
            # Accept a range of message shapes — transcripts vary by harness version
            role = msg.get("role") or msg.get("message", {}).get("role")
            content = msg.get("content") or msg.get("message", {}).get("content")
            if role == "assistant" and content:
                last_text = []
                if isinstance(content, str):
                    last_text.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_text.append(block.get("text", ""))
except: pass
print("\n".join(last_text))
PY
)"
fi

# Fallback: scan stdin JSON for any [LEARN] blocks embedded directly
if [ -z "$TEXT" ]; then
  TEXT="$INPUT"
fi

[ -z "$TEXT" ] && exit 0

# ── Parse [LEARN] blocks ────────────────────────────────────────────────────
# Format:
#   [LEARN] Category: <rule-one-liner>
#   Mistake: <what>
#   Correction: <what>
#
# Python does the parsing — bash regex isn't up to multi-line extraction.
python3 - "$DEVELOPER" "$CORRECTIONS_FILE" <<PY 2>/dev/null
import json, os, re, sys, uuid
from datetime import datetime, timezone

developer = sys.argv[1]
out_file = sys.argv[2]

text = """$(echo "$TEXT" | sed 's/"""/"""/g')"""

# Find all [LEARN] blocks. Each is:
#   [LEARN] Category: <rule>
#   Mistake: <...>
#   Correction: <...>
pattern = re.compile(
    r'\[LEARN\]\s*([^:\n]+)\s*:\s*(.+?)\n'
    r'\s*Mistake\s*:\s*(.+?)\n'
    r'\s*Correction\s*:\s*(.+?)(?=\n\s*\[LEARN\]|\n\s*\n|\Z)',
    re.DOTALL,
)
count = 0
with open(out_file, "a") as f:
    for m in pattern.finditer(text):
        category = m.group(1).strip().lower().replace(" ", "-")
        rule = m.group(2).strip()
        mistake = m.group(3).strip()
        correction = m.group(4).strip()
        rec = {
            "id": f"corr_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "developer": developer,
            "category": category,
            "rule": rule,
            "mistake": mistake,
            "correction": correction,
            "promoted_to_team": False,
        }
        f.write(json.dumps(rec) + "\n")
        count += 1

if count:
    print(f"📚 learn-capture: saved {count} correction(s) to .claude/memory/corrections/{developer}/corrections.jsonl")
PY

exit 0
