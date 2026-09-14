"""Prepare and optionally upload exact-episode watch pages.

Preparation reads episode metadata but writes only to the requested output
directory. Upload is a separate explicit command that verifies the prepared
manifest before replacing public R2 objects.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import tomllib

from agents.qa import current_funnel_urls_for_episode

WATCH_LINKS_SCHEMA = "cascade.episode-watch-links/v1"
SITE_MANIFEST_SCHEMA = "cascade.episode-watch-site/v1"
SHOW_LINKS = (
    ("spotify", "Spotify show"),
    ("apple_podcasts", "Apple Podcasts show"),
    ("youtube", "YouTube channel"),
    ("instagram", "Instagram"),
    ("x", "X"),
    ("tiktok", "TikTok"),
    ("iheartradio", "iHeartRadio"),
    ("github", "GitHub"),
)


def _https_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return value


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _site_identity(config: dict) -> dict:
    podcast = config.get("podcast", {})
    links = podcast.get("links", {})
    r2 = podcast.get("r2", {})
    public_url = _https_url(r2.get("public_url"))
    if not public_url:
        raise ValueError("podcast.r2.public_url must be an HTTPS URL")
    return {
        "name": str(links.get("display_name") or podcast.get("title") or "Podcast"),
        "tagline": str(links.get("tagline") or podcast.get("description") or ""),
        "artwork_url": _https_url(podcast.get("artwork_url")),
        "public_url": public_url.rstrip("/"),
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
    identity = _site_identity(config)
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
    relative_path = f"links/episodes/{episode_id}.html"
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
            "path": relative_path,
            "url": f"{identity['public_url']}/{relative_path}",
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
<style>body{{margin:0;background:#0a0a0a;color:#f4f4f5;font:16px system-ui,sans-serif}}main{{max-width:680px;margin:auto;padding:40px 20px 64px}}.brand{{display:flex;align-items:center;gap:16px;margin-bottom:32px}}.art{{width:76px;height:76px;border-radius:50%;object-fit:cover}}h1{{font-size:clamp(28px,7vw,48px);line-height:1.05}}h2{{font-size:20px}}p,.meta{{color:#a1a1aa;line-height:1.5}}.card,.button{{display:block;border:1px solid #303036;border-radius:14px;background:#18181b;padding:18px;margin:12px 0;color:#fafafa;text-decoration:none}}.button:hover,.card:hover{{border-color:#8b5cf6}}.scope,.back{{color:#a1a1aa;font-size:13px}}.scope{{display:block;margin-top:5px}}.back{{display:inline-block;margin-bottom:12px}}footer{{margin-top:36px;color:#71717a;font-size:13px}}</style></head>
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
    body = (
        '<a class="back" href="../index.html">All episodes</a>'
        f"<h1>{html.escape(document['title'])}</h1>"
        "<p>Choose where to watch or listen to the full episode.</p>" + "".join(buttons)
    )
    return _page_shell(
        document["title"],
        body,
        document["show"],
        f"Full episode links for {document['title']}",
    )


def render_index(
    documents: list[dict], config: dict, *, apple_catalog: dict | None = None
) -> str:
    identity = _site_identity(config)
    cards = []
    for document in documents:
        platforms = ", ".join(
            item["key"].replace("_", " ").title()
            for item in document["destinations"]
            if item["scope"] == "episode"
        )
        href = f"episodes/{quote(document['episode_id'])}.html"
        cards.append(
            f'<a class="card" href="{href}"><h2>{html.escape(document["title"])}</h2>'
            f'<span class="scope">Full episode: {html.escape(platforms)}</span></a>'
        )
    links = config.get("podcast", {}).get("links", {})
    show_urls = {key: _https_url(links.get(key)) for key, _label in SHOW_LINKS}
    if apple_catalog:
        show_urls["apple_podcasts"] = (
            _https_url(apple_catalog.get("show_url")) or show_urls["apple_podcasts"]
        )
    show_links = "".join(
        f'<a class="button" target="_blank" rel="noopener" href="{html.escape(show_urls[key], quote=True)}">{html.escape(label)}</a>'
        for key, label in SHOW_LINKS
        if show_urls[key]
    )
    body = (
        "<h1>Watch full episodes</h1>"
        "<p>Choose an episode, then open its matching full conversation.</p>"
        + "".join(cards)
        + (
            f"<h2>Follow {html.escape(identity['name'])}</h2>{show_links}"
            if show_links
            else ""
        )
    )
    return _page_shell(
        f"{identity['name']} — full episodes",
        body,
        identity,
        f"Full episode links for {identity['name']}",
    )


def render_legacy_root_redirect(config: dict) -> str:
    """Keep legacy /index.html links useful after the hub moved under /links."""
    identity = _site_identity(config)
    target = f"{identity['public_url']}/links/index.html"
    escaped_target = html.escape(target, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0; url={escaped_target}"><meta name="robots" content="noindex">
<link rel="canonical" href="{escaped_target}"><title>{html.escape(identity["name"])} — full episodes</title></head>
<body><p><a href="{escaped_target}">Open the full episode hub</a></p></body></html>"""


def prepare_site(
    episodes_root: Path,
    config: dict,
    output_dir: Path,
    *,
    apple_catalog: dict | None = None,
) -> dict:
    """Write a reviewable static site without changing episode or remote state."""
    output_dir = Path(output_dir)
    documents = []
    for episode_dir in sorted(Path(episodes_root).iterdir(), reverse=True):
        if not (episode_dir / "episode.json").is_file():
            continue
        document = build_episode_watch_document(
            episode_dir, config, apple_catalog=apple_catalog
        )
        if document["exact_episode_destination_count"]:
            documents.append(document)
    if not documents:
        raise ValueError("No current exact episode destinations are available")

    files: dict[str, bytes] = {
        "index.html": render_legacy_root_redirect(config).encode(),
        "links/index.html": render_index(
            documents, config, apple_catalog=apple_catalog
        ).encode(),
    }
    files.update(
        {
            document["landing_page"]["path"]: render_episode_page(document).encode()
            for document in documents
        }
    )
    for relative, content in files.items():
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "schema": SITE_MANIFEST_SCHEMA,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "episode_count": len(documents),
        "episodes": documents,
        "files": [
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in sorted(files.items())
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def upload_prepared_site(site_dir: Path, config: dict) -> list[str]:
    """Upload the exact manifest-bound bytes, replacing the index last."""
    import httpx

    site_dir = Path(site_dir).resolve()
    manifest = _read_json(site_dir / "manifest.json")
    if manifest.get("schema") != SITE_MANIFEST_SCHEMA:
        raise ValueError("Prepared watch-site manifest has the wrong schema")
    identity = _site_identity(config)
    r2 = config.get("podcast", {}).get("r2", {})
    bucket = str(r2.get("bucket", "")).strip()
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    token = os.getenv("CLOUDFLARE_API_TOKEN", "")
    if not bucket or not account or not token:
        raise RuntimeError("R2 bucket and Cloudflare credentials are required")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list) or not manifest_files:
        raise ValueError("Prepared watch-site manifest has no files")
    links_root = (site_dir / "links").resolve()
    if links_root.parent != site_dir:
        raise ValueError("Prepared watch-site links directory escapes its directory")
    prepared = []
    seen: set[str] = set()
    for item in manifest_files:
        relative = item.get("path") if isinstance(item, dict) else None
        if not isinstance(relative, str) or not (
            relative == "index.html" or relative.startswith("links/")
        ):
            raise ValueError("Prepared watch-site path is invalid")
        if relative in seen:
            raise ValueError(f"Prepared watch-site path is duplicated: {relative}")
        seen.add(relative)
        path = (site_dir / relative).resolve()
        if relative == "index.html":
            if path.parent != site_dir:
                raise ValueError("Prepared watch-site path escapes its directory")
        elif links_root not in path.parents:
            raise ValueError("Prepared watch-site path escapes its directory")
        content = path.read_bytes()
        if len(content) != item.get("size_bytes") or hashlib.sha256(
            content
        ).hexdigest() != item.get("sha256"):
            raise ValueError(f"Prepared watch-site file changed: {relative}")
        prepared.append((relative, content))
    if (
        "index.html" not in seen
        or "links/index.html" not in seen
        or not any(value.startswith("links/episodes/") for value in seen)
    ):
        raise ValueError("Prepared watch-site manifest is incomplete")
    prepared.sort(
        key=lambda item: (
            2 if item[0] == "index.html" else 1 if item[0] == "links/index.html" else 0
        )
    )
    uploaded = []
    for key, content in prepared:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{quote(account)}/r2/"
            f"buckets/{quote(bucket)}/objects/{quote(key, safe='/')}"
        )
        response = httpx.put(
            url,
            content=content,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-cache, max-age=0, must-revalidate",
            },
            timeout=60.0,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"R2 watch-site upload failed for {key}: HTTP {response.status_code}"
            )
        uploaded.append(f"{identity['public_url']}/{key}")
    return uploaded


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or upload episode watch pages"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--episodes-root", type=Path, required=True)
    prepare.add_argument("--config", type=Path, default=Path("config/config.toml"))
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--apple-catalog", type=Path)
    upload = subparsers.add_parser("upload")
    upload.add_argument("--site-dir", type=Path, required=True)
    upload.add_argument("--config", type=Path, default=Path("config/config.toml"))
    args = parser.parse_args()
    with args.config.open("rb") as handle:
        config = tomllib.load(handle)
    if args.command == "prepare":
        catalog = _read_json(args.apple_catalog) if args.apple_catalog else None
        manifest = prepare_site(
            args.episodes_root, config, args.output_dir, apple_catalog=catalog
        )
        print(
            json.dumps(
                {
                    "status": "prepared",
                    "episode_count": manifest["episode_count"],
                    "manifest": str(args.output_dir / "manifest.json"),
                }
            )
        )
    else:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        uploaded = upload_prepared_site(args.site_dir, config)
        print(json.dumps({"status": "uploaded", "urls": uploaded}))


if __name__ == "__main__":
    main()
