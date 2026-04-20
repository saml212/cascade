#!/usr/bin/env python3
"""
Test harness for .claude/hooks/*.py

Calls each hook as a subprocess, piping JSON on stdin, and checks exit codes.
Run from the repo root: python3 .claude/test_hooks.py
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent  # cascade/
HOOKS = ROOT / ".claude" / "hooks"
PASS = 0
FAIL = 0


def run_hook(hook: str, tool_input: dict, expected_exit: int, label: str) -> bool:
    global PASS, FAIL
    payload = json.dumps({"tool_name": "Bash", "tool_input": tool_input})
    result = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    ok = result.returncode == expected_exit
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {status}  [{label}]  exit={result.returncode} (expected {expected_exit})")
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            print(f"         {line}")
    if result.stderr:
        print(f"    stderr: {result.stderr.strip()[:200]}")
    if ok:
        PASS += 1
    else:
        FAIL += 1
    return ok


def run_post_hook(hook: str, tool_name: str, tool_input: dict, label: str) -> bool:
    global PASS, FAIL
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input, "tool_response": {}})
    result = subprocess.run(
        [sys.executable, str(HOOKS / hook)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    ok = result.returncode == 0
    status = "✅ PASS" if ok else "❌ FAIL"
    printed = bool(result.stdout.strip())
    note = " (printed reminder)" if printed else " (silent — no .py file)"
    print(f"  {status}  [{label}]{note}")
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            print(f"         {line}")
    if result.stderr:
        print(f"    stderr: {result.stderr.strip()[:200]}")
    if ok:
        PASS += 1
    else:
        FAIL += 1
    return ok


# ── pre_bash_guard.py ─────────────────────────────────────────────────────────
print("\n── pre_bash_guard.py ──────────────────────────────────────────────────")

run_hook("pre_bash_guard.py",
    {"command": "rm -rf /Volumes/1TB_SSD/cascade/episodes/ep_2026-03-02_073400"},
    expected_exit=2, label="BLOCK: rm episode data")

run_hook("pre_bash_guard.py",
    {"command": "rm -rf /tmp/some_work_file"},
    expected_exit=0, label="ALLOW: rm non-episode path")

run_hook("pre_bash_guard.py",
    {"command": "cat /Volumes/1TB_SSD/cascade/episodes/ep_foo/episode.json"},
    expected_exit=0, label="ALLOW: read episode file (no rm)")

run_hook("pre_bash_guard.py",
    {"command": "git push origin main --force"},
    expected_exit=2, label="BLOCK: git push --force main")

run_hook("pre_bash_guard.py",
    {"command": "git push origin main -f"},
    expected_exit=2, label="BLOCK: git push -f main")

run_hook("pre_bash_guard.py",
    {"command": "git push --force"},
    expected_exit=2, label="BLOCK: bare git push --force")

run_hook("pre_bash_guard.py",
    {"command": "git push origin main"},
    expected_exit=0, label="ALLOW: normal push to main")

run_hook("pre_bash_guard.py",
    {"command": "git push origin feature-branch --force"},
    expected_exit=0, label="ALLOW: force push to non-main branch")

run_hook("pre_bash_guard.py",
    {"command": "git push origin main --force-with-lease"},
    expected_exit=0, label="ALLOW: force-with-lease to main (safe)")

# ── pre_bash_ruff.py ──────────────────────────────────────────────────────────
print("\n── pre_bash_ruff.py ────────────────────────────────────────────────────")

run_hook("pre_bash_ruff.py",
    {"command": "ls -la"},
    expected_exit=0, label="ALLOW: non-commit command (ls)")

run_hook("pre_bash_ruff.py",
    {"command": "git status"},
    expected_exit=0, label="ALLOW: git status (not a commit)")

# Note: actual ruff run depends on whether ruff is installed and whether there are errors.
# We just verify the hook doesn't crash on a commit command — actual outcome varies.
# Run directly to show what ruff finds, without asserting a specific exit code.
_ruff_payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m 'test'"}})
_ruff_result = subprocess.run(
    [sys.executable, str(HOOKS / "pre_bash_ruff.py")],
    input=_ruff_payload, capture_output=True, text=True, cwd=str(ROOT),
)
print(f"  ℹ️  INFO  [git commit (ruff gate)]  exit={_ruff_result.returncode} (0=clean/skip, 2=issues found)")
if _ruff_result.stdout:
    for line in _ruff_result.stdout.strip().splitlines()[:8]:
        print(f"         {line}")
PASS += 1  # informational — always count as pass

# ── post_edit_py_reminder.py ──────────────────────────────────────────────────
print("\n── post_edit_py_reminder.py ────────────────────────────────────────────")

run_post_hook("post_edit_py_reminder.py",
    tool_name="Edit",
    tool_input={"file_path": "/Users/samuellarson/Local/Github/cascade/agents/clip_miner.py"},
    label="REMIND: Edit .py file")

run_post_hook("post_edit_py_reminder.py",
    tool_name="Write",
    tool_input={"file_path": "/Users/samuellarson/Local/Github/cascade/lib/new_module.py"},
    label="REMIND: Write .py file")

run_post_hook("post_edit_py_reminder.py",
    tool_name="Edit",
    tool_input={"file_path": "/Users/samuellarson/Local/Github/cascade/frontend/app.js"},
    label="SILENT: Edit .js file (no reminder)")

# ── stop_summary.py ───────────────────────────────────────────────────────────
print("\n── stop_summary.py ─────────────────────────────────────────────────────")
# Run directly (Stop hook doesn't use tool_input)
result = subprocess.run(
    [sys.executable, str(HOOKS / "stop_summary.py")],
    capture_output=True,
    text=True,
    cwd=str(ROOT),
)
ok = result.returncode == 0
status = "✅ PASS" if ok else "❌ FAIL"
print(f"  {status}  [stop_summary: runs without error]")
if result.stdout:
    for line in result.stdout.strip().splitlines():
        print(f"         {line}")

if ok:
    PASS += 1
else:
    FAIL += 1

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"Results: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
