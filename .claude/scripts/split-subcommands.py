#!/usr/bin/env python3
"""Split a shell command on operators that introduce independent sub-commands.

Usage: split-subcommands.py <command-string>

Prints one sub-command per line, trimmed.

Understands:
  Separators:   && || ; | ` $(  and newlines
  Quoting:      single-quoted 'foo && bar' and double-quoted "foo ; bar"
                are treated as atomic (no split inside).

Known limits:
  - Doesn't handle backslash-escaped quotes inside the same quote type.
  - Doesn't track shell heredocs.
  - Doesn't track nested $( ... ) depth beyond splitting at the opener.

For safety-check.sh and pre-commit-gate.sh: the point isn't perfect shell
parsing, it's "don't match operators inside quoted string arguments".
"""

import sys


def split_subcommands(cmd: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)

    def flush():
        piece = "".join(current).strip().rstrip(")")
        if piece:
            parts.append(piece)

    while i < n:
        c = cmd[i]

        # Quote state — flip on unescaped matching quote
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
            i += 1
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
            i += 1
            continue

        if in_single or in_double:
            current.append(c)
            i += 1
            continue

        # Outside quotes: recognise separators
        two = cmd[i : i + 2]
        if two == "&&" or two == "||":
            flush()
            current = []
            i += 2
            continue
        if two == "$(":
            flush()
            current = []
            i += 2
            continue
        if c in (";", "|", "`", "\n"):
            flush()
            current = []
            i += 1
            continue

        current.append(c)
        i += 1

    flush()
    return parts


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    for sub in split_subcommands(cmd):
        print(sub)
