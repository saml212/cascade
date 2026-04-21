#!/usr/bin/env python3
"""Split a shell command on operators that introduce independent sub-commands.

Usage: split-subcommands.py <command-string>

Prints one sub-command per line, trimmed.

Splits on: && || ; | ` $(
Also strips trailing ) from sub-commands (residue from $(...) splits).

This is a helper for safety-check.sh and pre-commit-gate.sh so they can
inspect each sub-command independently rather than the raw string (which
would false-positive on quoted content like echo "rm -rf /protected").
"""
import re
import sys


def split_subcommands(cmd: str) -> list[str]:
    parts = re.split(r"&&|\|\||;|\||`|\$\(", cmd)
    out = []
    for p in parts:
        p = p.strip().rstrip(")")
        if p:
            out.append(p)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    for sub in split_subcommands(cmd):
        print(sub)
