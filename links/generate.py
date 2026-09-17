"""Fail fast for the retired static watch-site generators."""

from __future__ import annotations

import sys

from links.episode_hub import MIGRATION


def main() -> None:
    sys.stderr.write(MIGRATION)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
