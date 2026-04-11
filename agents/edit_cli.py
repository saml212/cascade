"""Cascade edit CLI — transcript-driven editing.

Usage:
    python -m agents.edit_cli <episode_id> list
    python -m agents.edit_cli <episode_id> find "phrase to find"
    python -m agents.edit_cli <episode_id> cut <start> <end> [--reason "..."]
    python -m agents.edit_cli <episode_id> trim-start <seconds> [--reason "..."]
    python -m agents.edit_cli <episode_id> trim-end <seconds> [--reason "..."]
    python -m agents.edit_cli <episode_id> remove <index>
    python -m agents.edit_cli <episode_id> clear
    python -m agents.edit_cli <episode_id> apply

Episode IDs can be partial — the CLI will resolve them against
$CASCADE_OUTPUT_DIR. If multiple episodes match, you get an error.

Find example:
    python -m agents.edit_cli ep_2026-04-10 find "credit cards"
    → prints proposals; user copies start/end into a `cut` command.
"""

import argparse
import json
import sys
from pathlib import Path

# Load .env so CASCADE_OUTPUT_DIR is available
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from lib.editor import (
    add_cut,
    add_trim_start,
    add_trim_end,
    clear_edits,
    list_edits,
    remove_edit,
    total_time_removed,
    find_and_propose_cut,
)
from lib.paths import get_episodes_dir


def _resolve_episode(episode_arg: str) -> Path:
    """Resolve a possibly-partial episode ID to a directory."""
    episodes_dir = get_episodes_dir()
    if not episodes_dir.exists():
        print(f"Error: episodes dir not found: {episodes_dir}")
        sys.exit(1)

    # Exact match first
    exact = episodes_dir / episode_arg
    if exact.exists() and (exact / "episode.json").exists():
        return exact

    # Prefix match
    candidates = sorted(
        d for d in episodes_dir.iterdir()
        if d.is_dir() and (d / "episode.json").exists() and d.name.startswith(episode_arg)
    )
    if not candidates:
        print(f"Error: no episode matching '{episode_arg}' in {episodes_dir}")
        sys.exit(1)
    if len(candidates) > 1:
        print(f"Error: multiple episodes match '{episode_arg}':")
        for c in candidates:
            print(f"  {c.name}")
        sys.exit(1)
    return candidates[0]


def _print_edits(ep_dir: Path) -> None:
    edits = list_edits(ep_dir)
    if not edits:
        print(f"No edits for {ep_dir.name}")
        return
    print(f"Edits for {ep_dir.name}:")
    for i, e in enumerate(edits):
        if e["type"] == "cut":
            print(f"  [{i}] cut    {e['start_seconds']:>9.3f}s → {e['end_seconds']:>9.3f}s "
                  f"({e.get('duration_removed', 0):.1f}s)  {e.get('reason','')}")
        elif e["type"] == "trim_start":
            print(f"  [{i}] start  →  {e['seconds']:.3f}s   {e.get('reason','')}")
        elif e["type"] == "trim_end":
            print(f"  [{i}] end    →  {e['seconds']:.3f}s   {e.get('reason','')}")
    total = total_time_removed(edits)
    print(f"Total time removed by cuts: {total:.1f}s")


def cmd_list(args):
    ep_dir = _resolve_episode(args.episode)
    _print_edits(ep_dir)


def cmd_find(args):
    ep_dir = _resolve_episode(args.episode)
    try:
        proposals = find_and_propose_cut(ep_dir, args.query, max_results=args.max)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not proposals:
        print(f"No matches for '{args.query}'")
        return

    print(f"Found {len(proposals)} match(es) for '{args.query}':\n")
    for i, p in enumerate(proposals):
        mark = "★" if p["method"] == "exact" else " "
        print(f"  [{i}] {mark} score={p['score']:>5.1f} {p['method']:<5} "
              f"speaker_{p['speaker']}  "
              f"{p['start_seconds']:>8.2f}s → {p['end_seconds']:>8.2f}s  "
              f"({p['duration']:.1f}s)")
        print(f"        matched: \"{p['matched_text']}\"")
        print(f"        context: {p['context']}")
        print()
    print("To cut one, run:")
    print(f"  python -m agents.edit_cli {args.episode} cut <start> <end> --reason '...'")


def cmd_cut(args):
    ep_dir = _resolve_episode(args.episode)
    try:
        edit = add_cut(ep_dir, args.start, args.end, reason=args.reason)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Added cut: {args.start:.3f}s → {args.end:.3f}s "
          f"({edit['duration_removed']:.1f}s)")
    _print_edits(ep_dir)


def cmd_trim_start(args):
    ep_dir = _resolve_episode(args.episode)
    add_trim_start(ep_dir, args.seconds, reason=args.reason)
    print(f"Set trim_start: {args.seconds:.3f}s")
    _print_edits(ep_dir)


def cmd_trim_end(args):
    ep_dir = _resolve_episode(args.episode)
    add_trim_end(ep_dir, args.seconds, reason=args.reason)
    print(f"Set trim_end: {args.seconds:.3f}s")
    _print_edits(ep_dir)


def cmd_remove(args):
    ep_dir = _resolve_episode(args.episode)
    removed = remove_edit(ep_dir, args.index)
    if removed is None:
        print(f"Error: no edit at index {args.index}")
        sys.exit(1)
    print(f"Removed: {removed}")
    _print_edits(ep_dir)


def cmd_clear(args):
    ep_dir = _resolve_episode(args.episode)
    n = clear_edits(ep_dir)
    print(f"Cleared {n} edit(s) from {ep_dir.name}")


def cmd_apply(args):
    """Trigger longform_render via the API."""
    import httpx
    ep_dir = _resolve_episode(args.episode)
    episode_id = ep_dir.name

    # Check server is running
    try:
        client = httpx.Client(timeout=10)
        resp = client.post(
            f"http://localhost:8420/api/episodes/{episode_id}/edits/apply",
            json={},
        )
        if resp.status_code != 200:
            print(f"Error: API returned {resp.status_code}: {resp.text}")
            sys.exit(1)
        print("Render started. Watch progress at:")
        print(f"  curl http://localhost:8420/api/episodes/{episode_id}/pipeline-status")
    except Exception as e:
        print(f"Error: could not reach API at localhost:8420 ({e})")
        print("Make sure the server is running: ./start.sh")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="cascade-edit",
        description="Transcript-driven editing for Cascade episodes",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p = sub.add_parser("list", help="Show current edits")
    p.add_argument("episode", help="Episode ID (or unique prefix)")
    p.set_defaults(func=cmd_list)

    # find
    p = sub.add_parser("find", help="Search transcript for a phrase")
    p.add_argument("episode")
    p.add_argument("query", help="Phrase to search for")
    p.add_argument("--max", type=int, default=5, help="Max results (default 5)")
    p.set_defaults(func=cmd_find)

    # cut
    p = sub.add_parser("cut", help="Add a cut (remove a time range)")
    p.add_argument("episode")
    p.add_argument("start", type=float, help="Cut start in seconds")
    p.add_argument("end", type=float, help="Cut end in seconds")
    p.add_argument("--reason", default="", help="Optional reason/note")
    p.set_defaults(func=cmd_cut)

    # trim-start
    p = sub.add_parser("trim-start", help="Set the longform start time")
    p.add_argument("episode")
    p.add_argument("seconds", type=float)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_trim_start)

    # trim-end
    p = sub.add_parser("trim-end", help="Set the longform end time")
    p.add_argument("episode")
    p.add_argument("seconds", type=float)
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_trim_end)

    # remove
    p = sub.add_parser("remove", help="Remove an edit by index")
    p.add_argument("episode")
    p.add_argument("index", type=int)
    p.set_defaults(func=cmd_remove)

    # clear
    p = sub.add_parser("clear", help="Remove all edits")
    p.add_argument("episode")
    p.set_defaults(func=cmd_clear)

    # apply
    p = sub.add_parser("apply", help="Render longform with current edits")
    p.add_argument("episode")
    p.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
