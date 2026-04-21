#!/usr/bin/env python3
"""Parse [LEARN] blocks from text on stdin and append them to corrections JSONL.

Usage: parse-learn-blocks.py <developer-slug> <corrections-file>
Reads text from stdin.

Skips [LEARN] blocks inside fenced code (```...```). This way, examples and
discussions in docs/chat don't false-positive.

Expected block format:
  [LEARN] <category-slug>: <rule one-liner>
  Mistake: <what went wrong>
  Correction: <right approach>
"""
import json
import re
import sys
import uuid
from datetime import datetime, timezone


BLOCK_RE = re.compile(
    r"\[LEARN\]\s*([^:\n]+?)\s*:\s*(.+?)\n"
    r"\s*Mistake\s*:\s*(.+?)\n"
    r"\s*Correction\s*:\s*(.+?)(?=\n\s*\[LEARN\]|\n\s*\n|\Z)",
    re.DOTALL,
)


def strip_code_fences(text: str) -> str:
    """Remove content between ``` fences so [LEARN] examples aren't captured.

    Uses a state machine rather than regex so nested/malformed fences degrade
    gracefully (text between an unclosed fence and EOF is still stripped).
    """
    out = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue  # drop the fence line itself too
        if not in_fence:
            out.append(line)
    return "".join(out)


def main():
    if len(sys.argv) < 3:
        return 0
    developer = sys.argv[1]
    out_file = sys.argv[2]

    raw = sys.stdin.read()
    text = strip_code_fences(raw)

    count = 0
    with open(out_file, "a") as f:
        for m in BLOCK_RE.finditer(text):
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
        print(
            f"📚 learn-capture: saved {count} correction(s) to "
            f".claude/memory/corrections/{developer}/corrections.jsonl"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
