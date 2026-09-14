"""Exact-episode landing page preparation and upload safety."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from links import episode_hub


def _config() -> dict:
    return {
        "podcast": {
            "title": "The Local Podcast",
            "artwork_url": "https://cdn.example/art.jpg",
            "links": {
                "display_name": "Local",
                "tagline": "A local Bay Area podcast",
                "apple_podcasts": "https://podcasts.apple.com/show/local",
                "spotify": "https://open.spotify.com/show/local",
            },
            "r2": {
                "bucket": "podcast",
                "public_url": "https://public.example",
            },
        }
    }


def _episode(root: Path, episode_id: str, title: str) -> Path:
    episode_dir = root / episode_id
    episode_dir.mkdir(parents=True)
    (episode_dir / "episode.json").write_text(
        json.dumps({"episode_id": episode_id, "title": title})
    )
    return episode_dir


def test_document_uses_exact_current_urls_and_labels_apple_fallback(tmp_path):
    episode_dir = _episode(tmp_path, "ep_001", "Guest & Host")

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        _config(),
        funnel_urls={
            "youtube": "https://youtube.example/watch?v=1&list=2",
            "spotify": "https://open.spotify.com/episode/one",
        },
    )
    rendered = episode_hub.render_episode_page(document)

    assert document["landing_page"] == {
        "path": "links/episodes/ep_001.html",
        "url": "https://public.example/links/episodes/ep_001.html",
        "remote_status": "not_checked",
    }
    assert [item["scope"] for item in document["destinations"]] == [
        "episode",
        "episode",
        "show",
    ]
    assert document["destinations"][1]["label"] == "Watch or listen on Spotify"
    assert document["destinations"][-1]["label"] == ("All episodes on Apple Podcasts")
    assert "this is not an exact episode link" in document["destinations"][-1]["note"]
    assert "Guest &amp; Host" in rendered
    assert "v=1&amp;list=2" in rendered
    assert "Browse the show" in rendered
    assert "this is not an exact episode link" not in rendered


def test_prepare_builds_branded_index_and_exact_apple_page(tmp_path, monkeypatch):
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    _episode(episodes, "ep_002", "Second")
    _episode(episodes, "ep_001", "First <Episode>")

    def current_urls(episode_dir, _episode_data, _config_data):
        if episode_dir.name == "ep_002":
            return {"youtube": "", "spotify": ""}
        return {
            "youtube": "https://youtube.example/first",
            "spotify": "https://open.spotify.com/episode/first",
        }

    monkeypatch.setattr(episode_hub, "current_funnel_urls_for_episode", current_urls)
    output = tmp_path / "prepared"
    manifest = episode_hub.prepare_site(
        episodes,
        _config(),
        output,
        apple_catalog={
            "show_url": "https://podcasts.apple.com/show/local",
            "episodes": [
                {
                    "episode_id": "ep_001",
                    "title": "First <Episode>",
                    "url": "https://podcasts.apple.com/episode/first",
                }
            ],
        },
    )

    assert manifest["episode_count"] == 1
    assert manifest["episodes"][0]["episode_id"] == "ep_001"
    assert manifest["episodes"][0]["destinations"][-1]["scope"] == "episode"
    legacy = (output / "index.html").read_text()
    index = (output / "links" / "index.html").read_text()
    page = (output / "links" / "episodes" / "ep_001.html").read_text()
    assert 'rel="canonical" href="https://public.example/links/index.html"' in legacy
    assert 'content="0; url=https://public.example/links/index.html"' in legacy
    assert "Open the full episode hub" in legacy
    assert "Local" in index
    assert "First &lt;Episode&gt;" in index
    assert "ep_002" not in index
    assert "Spotify show" in index
    assert "Apple Podcasts show" in index
    assert "https://podcasts.apple.com/show/local" in index
    assert "Watch on Apple Podcasts" in page
    assert 'href="../index.html"' in page
    for item in manifest["files"]:
        content = (output / item["path"]).read_bytes()
        assert item["size_bytes"] == len(content)
        assert item["sha256"] == hashlib.sha256(content).hexdigest()


def test_upload_verifies_every_file_then_replaces_indexes_last(tmp_path, monkeypatch):
    site = tmp_path / "site"
    episode_path = site / "links" / "episodes" / "ep_001.html"
    index_path = site / "links" / "index.html"
    legacy_path = site / "index.html"
    episode_path.parent.mkdir(parents=True)
    episode_path.write_bytes(b"episode")
    index_path.write_bytes(b"index")
    legacy_path.write_bytes(b"legacy")

    def entry(path: Path) -> dict:
        content = path.read_bytes()
        return {
            "path": str(path.relative_to(site)),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    (site / "manifest.json").write_text(
        json.dumps(
            {
                "schema": episode_hub.SITE_MANIFEST_SCHEMA,
                "files": [entry(legacy_path), entry(index_path), entry(episode_path)],
            }
        )
    )
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    put = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr("httpx.put", put)

    uploaded = episode_hub.upload_prepared_site(site, _config())

    assert uploaded == [
        "https://public.example/links/episodes/ep_001.html",
        "https://public.example/links/index.html",
        "https://public.example/index.html",
    ]
    assert put.call_count == 3
    assert put.call_args_list[0].kwargs["content"] == b"episode"
    assert put.call_args_list[1].kwargs["content"] == b"index"
    assert put.call_args_list[2].kwargs["content"] == b"legacy"
    assert all(
        call.kwargs["headers"]["Cache-Control"]
        == "no-cache, max-age=0, must-revalidate"
        for call in put.call_args_list
    )


@pytest.mark.parametrize("damage", ["bytes", "duplicate", "escape", "missing_legacy"])
def test_upload_rejects_unreviewed_or_unsafe_manifest_without_network(
    tmp_path, monkeypatch, damage
):
    site = tmp_path / "site"
    links = site / "links"
    pages = links / "episodes"
    pages.mkdir(parents=True)
    index = links / "index.html"
    page = pages / "ep.html"
    legacy = site / "index.html"
    index.write_bytes(b"index")
    page.write_bytes(b"episode")
    legacy.write_bytes(b"legacy")

    def entry(relative: str, content: bytes) -> dict:
        return {
            "path": relative,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    files = [
        entry("index.html", b"legacy"),
        entry("links/index.html", b"index"),
        entry("links/episodes/ep.html", b"episode"),
    ]
    if damage == "bytes":
        page.write_bytes(b"changed")
    elif damage == "duplicate":
        files.append(files[-1].copy())
    elif damage == "escape":
        outside = tmp_path / "outside.html"
        outside.write_bytes(b"outside")
        page.unlink()
        page.symlink_to(outside)
    else:
        files.pop(0)
    (site / "manifest.json").write_text(
        json.dumps({"schema": episode_hub.SITE_MANIFEST_SCHEMA, "files": files})
    )
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    put = Mock()
    monkeypatch.setattr("httpx.put", put)

    with pytest.raises(ValueError):
        episode_hub.upload_prepared_site(site, _config())

    put.assert_not_called()


def test_apple_title_match_without_episode_identity_stays_show_level(tmp_path):
    episode_dir = _episode(tmp_path, "ep_new", "Repeated Title")

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        _config(),
        funnel_urls={"youtube": "", "spotify": "https://spotify.example/episode"},
        apple_catalog={
            "show_url": "https://podcasts.apple.com/show/local",
            "episodes": [
                {
                    "title": "Repeated Title",
                    "url": "https://podcasts.apple.com/episode/old",
                }
            ],
        },
    )

    apple = document["destinations"][-1]
    assert apple["scope"] == "show"
    assert apple["url"] == "https://podcasts.apple.com/show/local"
