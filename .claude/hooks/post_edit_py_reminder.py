#!/usr/bin/env python3
"""
PostToolUse(Edit|Write) hook — remind about __pycache__ when a Python file is modified.

Prints a one-line reminder so Claude (and the dev) remembers to restart
uvicorn or clear __pycache__ before running the pipeline or tests.
"""
import json
import sys

data = json.load(sys.stdin)
file_path = data.get("tool_input", {}).get("file_path", "")

if file_path.endswith(".py"):
    short = file_path.split("/cascade/")[-1] if "/cascade/" in file_path else file_path
    print(f"⚠  Python modified: {short}")
    print("   → Restart uvicorn (or kill & re-run ./start.sh) before testing the server.")
    print("   → Clear cache if running CLI: find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null; true")

sys.exit(0)
