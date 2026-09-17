#!/usr/bin/env python3
"""Fail fast for the retired standalone show-page generator."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

MIGRATION = """The standalone show-level links page was retired.
Prepare the canonical thelocalpod.link episode hub instead:
  python -m links.episode_hub prepare --episodes-root EPISODES --output-dir SITE
After reviewing SITE/manifest.json, upload those exact bytes with:
  python -m links.episode_hub upload --site-dir SITE
"""


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Retired; use links.episode_hub for the canonical site"
    )
    parser.parse_known_args(argv)
    parser.exit(2, MIGRATION)


if __name__ == "__main__":
    main()
