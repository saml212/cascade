"""Read-only exact-episode watch links and HTML preview."""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from agents.qa import current_funnel_urls_for_episode, episode_hub_url

WATCH_LINKS_SCHEMA = "cascade.episode-watch-links/v1"
MIGRATION = """Cascade's R2 watch-site generator is retired.
The canonical public episode hub is maintained in the thelocalpod.link site repository.
Use GET /api/episodes/{episode_id}/watch-links for current destination data or
GET /api/episodes/{episode_id}/watch-page for a read-only local HTML preview.
Existing R2 pages remain historical read-only assets.
"""


def _https_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.strip(".")
        or any(char.isspace() for char in parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        return ""
    return value


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def configured_apple_catalog(config: dict) -> dict | None:
    """Read the explicit-ID Apple catalog named by trusted local config."""
    value = config.get("podcast", {}).get("links", {}).get("apple_catalog_path")
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypeError("podcast.links.apple_catalog_path must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("podcast.links.apple_catalog_path must be an absolute path")
    return _read_json(path)


def _show_identity(config: dict) -> dict:
    podcast = config.get("podcast", {})
    links = podcast.get("links", {})
    return {
        "name": str(links.get("display_name") or podcast.get("title") or "Podcast"),
        "tagline": str(links.get("tagline") or podcast.get("description") or ""),
        "artwork_url": _https_url(podcast.get("artwork_url")),
        "apple_show_url": _https_url(links.get("apple_podcasts")),
    }


def _apple_destination(
    episode: dict, catalog: dict | None, fallback: str
) -> dict | None:
    exact_url = ""
    if catalog:
        catalog_episodes = catalog.get("episodes", [])
        if not isinstance(catalog_episodes, list):
            raise TypeError("Apple catalog episodes must be a list")
        matches = [
            item
            for item in catalog_episodes
            if isinstance(item, dict)
            and item.get("episode_id") == episode["episode_id"]
        ]
        if len(matches) == 1:
            exact_url = _https_url(matches[0].get("url"))
        fallback = _https_url(catalog.get("show_url")) or fallback
    if exact_url:
        return {
            "key": "apple_podcasts",
            "label": "Watch on Apple Podcasts",
            "url": exact_url,
            "scope": "episode",
        }
    if fallback:
        return {
            "key": "apple_podcasts",
            "label": "All episodes on Apple Podcasts",
            "url": fallback,
            "scope": "show",
            "note": "Show-level fallback — this is not an exact episode link.",
        }
    return None


def _landing_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    if parsed.fragment:
        path += f"#{parsed.fragment}"
    return path


def build_episode_watch_document(
    episode_dir: Path,
    config: dict,
    *,
    apple_catalog: dict | None = None,
    funnel_urls: dict[str, str] | None = None,
) -> dict:
    """Build one read-only document from current revision-bound episode URLs."""
    episode_dir = Path(episode_dir)
    episode = _read_json(episode_dir / "episode.json")
    episode_id = episode.get("episode_id") or episode_dir.name
    if episode_id != episode_dir.name:
        raise ValueError("episode.json identity does not match its directory")
    title = str(episode.get("title") or episode.get("episode_name") or episode_id)
    identity = _show_identity(config)
    landing_url = _https_url(episode_hub_url(config, episode_id))
    if not landing_url:
        raise ValueError("No safe episode landing URL is configured")
    resolved = (
        current_funnel_urls_for_episode(episode_dir, episode, config)
        if funnel_urls is None
        else funnel_urls
    )
    if not isinstance(resolved, dict):
        raise TypeError("Current funnel URLs must be a JSON object")
    destinations = []
    for key, label in (
        ("youtube", "Watch on YouTube"),
        ("spotify", "Watch or listen on Spotify"),
    ):
        url = _https_url(resolved.get(key))
        if url:
            destinations.append(
                {"key": key, "label": label, "url": url, "scope": "episode"}
            )
    apple = _apple_destination(
        {"episode_id": episode_id, "title": title},
        apple_catalog,
        identity["apple_show_url"],
    )
    if apple:
        destinations.append(apple)
    return {
        "schema": WATCH_LINKS_SCHEMA,
        "episode_id": episode_id,
        "title": title,
        "show": {
            "name": identity["name"],
            "tagline": identity["tagline"],
            "artwork_url": identity["artwork_url"],
        },
        "landing_page": {
            "path": _landing_path(landing_url),
            "url": landing_url,
            "remote_status": "not_checked",
        },
        "destinations": destinations,
        "exact_episode_destination_count": sum(
            item["scope"] == "episode" for item in destinations
        ),
    }


def _page_shell(title: str, body: str, identity: dict, description: str) -> str:
    artwork = html.escape(identity["artwork_url"], quote=True)
    image = f'<img class="art" src="{artwork}" alt="">' if artwork else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name="description" content="{html.escape(description, quote=True)}">
<style>body{{margin:0;background:#0a0a0a;color:#f4f4f5;font:16px system-ui,sans-serif}}main{{max-width:680px;margin:auto;padding:40px 20px 64px}}.brand{{display:flex;align-items:center;gap:16px;margin-bottom:32px}}.art{{width:76px;height:76px;border-radius:50%;object-fit:cover}}h1{{font-size:clamp(28px,7vw,48px);line-height:1.05}}p,.meta{{color:#a1a1aa;line-height:1.5}}.button{{display:block;border:1px solid #303036;border-radius:14px;background:#18181b;padding:18px;margin:12px 0;color:#fafafa;text-decoration:none}}.button:hover{{border-color:#8b5cf6}}.scope,.back{{color:#a1a1aa;font-size:13px}}.scope{{display:block;margin-top:5px}}.back{{display:inline-block;margin-bottom:12px}}footer{{margin-top:36px;color:#71717a;font-size:13px}}</style></head>
<body><main><header class="brand">{image}<div><strong>{html.escape(identity["name"])}</strong><div class="meta">{html.escape(identity["tagline"])}</div></div></header>{body}<footer>{html.escape(identity["name"])}</footer></main></body></html>"""


def render_episode_page(document: dict) -> str:
    buttons = []
    for item in document["destinations"]:
        scope = "Exact episode" if item["scope"] == "episode" else "Browse the show"
        buttons.append(
            '<a class="button" target="_blank" rel="noopener" '
            f'href="{html.escape(item["url"], quote=True)}">'
            f'{html.escape(item["label"])}<span class="scope">'
            f"{html.escape(scope)}</span></a>"
        )
    canonical = html.escape(document["landing_page"]["url"], quote=True)
    body = (
        f'<a class="back" href="{canonical}">Open the canonical episode page</a>'
        f"<h1>{html.escape(document['title'])}</h1>"
        "<p>Choose where to watch or listen to the full episode.</p>" + "".join(buttons)
    )
    return _page_shell(
        document["title"],
        body,
        document["show"],
        f"Full episode links for {document['title']}",
    )


def main() -> None:
    sys.stderr.write(MIGRATION)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
