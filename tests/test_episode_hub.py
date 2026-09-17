"""Read-only exact-episode watch-link resolution and preview safety."""

import json
from pathlib import Path

import pytest

from links import episode_hub


def _config(*, canonical=True) -> dict:
    links = {
        "display_name": "Local",
        "tagline": "A local Bay Area podcast",
        "apple_podcasts": "https://podcasts.apple.com/show/local",
    }
    if canonical:
        links["episode_url_template"] = "https://thelocalpod.link/#{episode_id}"
    config = {
        "podcast": {
            "title": "The Local Podcast",
            "artwork_url": "https://cdn.example/art.jpg",
            "links": links,
        }
    }
    if not canonical:
        config["podcast"]["r2"] = {"public_url": "https://public.example"}
    return config


def _episode(root: Path, episode_id: str, title: str) -> Path:
    episode_dir = root / episode_id
    episode_dir.mkdir(parents=True)
    (episode_dir / "episode.json").write_text(
        json.dumps({"episode_id": episode_id, "title": title})
    )
    return episode_dir


def test_document_uses_canonical_page_current_urls_and_apple_fallback(tmp_path):
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
        "path": "/#ep_001",
        "url": "https://thelocalpod.link/#ep_001",
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
    assert 'href="https://thelocalpod.link/#ep_001"' in rendered
    assert "Browse the show" in rendered
    assert "this is not an exact episode link" not in rendered


def test_document_keeps_read_only_legacy_r2_fallback(tmp_path):
    episode_dir = _episode(tmp_path, "ep_legacy", "Legacy")

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        _config(canonical=False),
        funnel_urls={"youtube": "", "spotify": ""},
    )

    assert document["landing_page"] == {
        "path": "/links/episodes/ep_legacy.html",
        "url": "https://public.example/links/episodes/ep_legacy.html",
        "remote_status": "not_checked",
    }


def test_landing_path_preserves_query_fragment_and_encoded_identity(tmp_path):
    episode_dir = _episode(tmp_path, "ep one", "One")
    config = _config()
    config["podcast"]["links"]["episode_url_template"] = (
        "https://thelocalpod.link/episode?view=watch#{episode_id}"
    )

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        config,
        funnel_urls={},
    )

    assert document["landing_page"] == {
        "path": "/episode?view=watch#ep%20one",
        "url": "https://thelocalpod.link/episode?view=watch#ep%20one",
        "remote_status": "not_checked",
    }
    assert episode_hub._landing_path("https://thelocalpod.link/#ep%2Fa%20b") == (
        "/#ep%2Fa%20b"
    )


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


def test_duplicate_exact_apple_identity_falls_back_to_show(tmp_path):
    episode_dir = _episode(tmp_path, "ep_new", "Repeated Title")
    catalog = {
        "show_url": "https://podcasts.apple.com/show/local",
        "episodes": [
            {
                "episode_id": "ep_new",
                "url": "https://podcasts.apple.com/episode/one",
            },
            {
                "episode_id": "ep_new",
                "url": "https://podcasts.apple.com/episode/two",
            },
        ],
    }

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        _config(),
        funnel_urls={},
        apple_catalog=catalog,
    )

    assert document["destinations"] == [
        {
            "key": "apple_podcasts",
            "label": "All episodes on Apple Podcasts",
            "url": "https://podcasts.apple.com/show/local",
            "scope": "show",
            "note": "Show-level fallback — this is not an exact episode link.",
        }
    ]


def test_unsafe_exact_apple_url_falls_back_to_safe_show(tmp_path):
    episode_dir = _episode(tmp_path, "ep_new", "Title")

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        _config(),
        funnel_urls={},
        apple_catalog={
            "show_url": "https://podcasts.apple.com/show/local",
            "episodes": [
                {
                    "episode_id": "ep_new",
                    "url": "https://user@podcasts.apple.com/episode/one",
                }
            ],
        },
    )

    assert document["destinations"] == [
        {
            "key": "apple_podcasts",
            "label": "All episodes on Apple Podcasts",
            "url": "https://podcasts.apple.com/show/local",
            "scope": "show",
            "note": "Show-level fallback — this is not an exact episode link.",
        }
    ]


def test_unsafe_destination_urls_are_omitted(tmp_path):
    episode_dir = _episode(tmp_path, "ep_new", "Title")
    config = _config()
    config["podcast"]["links"]["apple_podcasts"] = "https://user@example.com/show"

    document = episode_hub.build_episode_watch_document(
        episode_dir,
        config,
        funnel_urls={
            "youtube": "https://bad host/watch",
            "spotify": "https://user:secret@example.com/episode",
        },
        apple_catalog={
            "show_url": "https://podcasts.apple.com:99999/show/local",
            "episodes": [
                {
                    "episode_id": "ep_new",
                    "url": "https://user@podcasts.apple.com/episode/one",
                }
            ],
        },
    )

    assert document["destinations"] == []


def test_non_list_apple_catalog_episodes_is_rejected(tmp_path):
    episode_dir = _episode(tmp_path, "ep_new", "Title")

    with pytest.raises(TypeError, match="episodes must be a list"):
        episode_hub.build_episode_watch_document(
            episode_dir,
            _config(),
            funnel_urls={},
            apple_catalog={"episodes": {}},
        )


def test_preview_escapes_all_document_values(tmp_path):
    episode_dir = _episode(tmp_path, "ep_new", '<script>alert("title")</script>')
    config = _config()
    config["podcast"]["links"].update(
        {
            "display_name": "Local <&>",
            "tagline": 'Tagline "quoted" <unsafe>',
        }
    )
    config["podcast"]["artwork_url"] = "https://cdn.example/art.jpg?x=1&y=2"
    document = episode_hub.build_episode_watch_document(
        episode_dir,
        config,
        funnel_urls={"youtube": 'https://youtube.example/watch?x="&y=2'},
    )

    rendered = episode_hub.render_episode_page(document)

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "Local &lt;&amp;&gt;" in rendered
    assert "Tagline &quot;quoted&quot; &lt;unsafe&gt;" in rendered
    assert "art.jpg?x=1&amp;y=2" in rendered
    assert "watch?x=&quot;&amp;y=2" in rendered
