#!/usr/bin/env python3
"""
PreToolUse(Bash) hook — run ruff before git commit.

Fires only when the bash command contains `git commit`. Runs:
  ruff check --select F401,F811,F841 agents/ lib/ server/

If ruff finds issues → exit 2 (block), print what to fix.
If ruff isn't installed → pass silently.
"""
import json
import re
import subprocess
import sys

data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "")

# Only fire on git commit
if not re.search(r"\bgit\s+commit\b", cmd):
    sys.exit(0)

# Find ruff — prefer venv
ruff_bin = None
for candidate in [".venv/bin/ruff", "ruff"]:
    try:
        r = subprocess.run([candidate, "--version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            ruff_bin = candidate
            break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        continue

if ruff_bin is None:
    sys.exit(0)  # ruff not available, skip

result = subprocess.run(
    [ruff_bin, "check", "--select", "F401,F811,F841", "agents/", "lib/", "server/"],
    capture_output=True,
    text=True,
    timeout=30,
)

if result.returncode != 0:
    print("🔍 ruff found issues — fix before committing:")
    print(result.stdout[:2000])
    if result.stderr:
        print(result.stderr[:400])
    print()
    print("Auto-fix:  .venv/bin/ruff check --select F401,F811,F841 --fix agents/ lib/ server/")
    sys.exit(2)

sys.exit(0)
