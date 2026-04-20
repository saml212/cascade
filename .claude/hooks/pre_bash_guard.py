#!/usr/bin/env python3
"""
PreToolUse(Bash) guard hook — two safety rails:
  1. Block rm commands that touch episode data directories.
  2. Block git push --force / -f to main, master, or an unknown tracking branch.
     Force pushes to explicitly-named non-main branches are allowed.
     --force-with-lease is never blocked (it's safe).

Exit 2 = block the tool call. Exit 0 = allow.
Stdout is shown to Claude as context on block.
"""
import json
import re
import sys


def _sub_commands(cmd: str) -> list[str]:
    """
    Split a shell command on operators that separate independent sub-commands
    (&&, ||, ;, |, backtick, $( ) and return stripped sub-command strings.
    Approximate — just enough to avoid matching patterns inside echo args.
    """
    parts = re.split(r"&&|\|\|?|;|`|\$\(", cmd)
    return [p.strip() for p in parts if p.strip()]


def _is_force_to_main(sub: str) -> bool:
    """
    Return True if this git push sub-command is a force push to main/master
    (or an unknown tracking branch, which we treat conservatively).
    """
    if not re.match(r"^git\s+push\b", sub):
        return False

    # --force-with-lease is safe — never block it
    if "--force-with-lease" in sub:
        return False

    has_force = bool(re.search(r"\s(--force|-f)\b", sub))
    if not has_force:
        return False

    # Parse positional (non-flag) tokens: [git, push, <remote>, <branch>]
    tokens = sub.split()
    positional = [t for t in tokens if not t.startswith("-")]

    if len(positional) >= 4:
        # Explicit branch/refspec specified — block only if it's main/master
        branch = positional[3]
        is_main = branch in ("main", "master") or re.search(r"/(main|master)$", branch)
        return bool(is_main)
    else:
        # No explicit branch → pushing to tracking branch (unknown) → conservative block
        return True


data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "")

subs = _sub_commands(cmd)

# ── Guard 1: episode data deletion ───────────────────────────────────────────
for sub in subs:
    if re.match(r"^rm\s", sub) and "/cascade/episodes/" in sub:
        print("🛑 BLOCKED: rm on episode data directory.")
        print(f"   Sub-command: {sub[:160]}")
        print("   Episode data is irreplaceable. Delete manually in Finder if you're sure.")
        sys.exit(2)

# ── Guard 2: force push to main / master ─────────────────────────────────────
for sub in subs:
    if _is_force_to_main(sub):
        print("🛑 BLOCKED: force push to main/master (or unknown tracking branch).")
        print(f"   Sub-command: {sub[:160]}")
        print("   Never force push to main. Create a revert commit instead.")
        print("   Tip: --force-with-lease is allowed if you need atomic push safety.")
        sys.exit(2)

sys.exit(0)
