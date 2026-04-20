#!/usr/bin/env python3
"""
Stop hook — show a diff summary of Python files changed since last commit.

Runs at end of each Claude turn. If any .py files differ from HEAD,
prints a reminder so the dev knows uvicorn needs restarting before
running the pipeline or tests.
"""
import subprocess
import sys

try:
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
except Exception:
    sys.exit(0)

if result.returncode != 0:
    sys.exit(0)

all_changed = result.stdout.strip().splitlines()
py_files = [f for f in all_changed if f.endswith(".py")]

if not py_files:
    sys.exit(0)

print(f"\n📦 {len(py_files)} Python file(s) modified vs HEAD:")
for f in py_files[:12]:
    print(f"   {f}")
if len(py_files) > 12:
    print(f"   … and {len(py_files) - 12} more")
print("   → Restart uvicorn or clear __pycache__ before running the pipeline.\n")

sys.exit(0)
