"""Tests for the publish agent.

These tests pin the safety-critical behaviors:

1. The agent refuses to run unless the exact current release revision has
   explicit approval.
2. The agent surfaces per-clip API errors instead of marking everything
   submitted. Background: at one point the X (Twitter) integration silently
   failed because Upload-Post returned HTTP 200 with an error body and the
   agent only checked the curl exit code.
3. Schedule entries from metadata.json are honored when present and a
   fallback schedule is generated when missing.
4. The X-specific ``x_long_text_as_post=true`` flag is sent so long X posts
   don't silently fail.

The agent shells out to curl for the actual upload, so subprocess.run is
patched in every test. We never make a real network call.
"""

import json
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from agents.publish import LongformPublishAgent, PublishAgent
from agents.qa import (
    clip_review_revision,
    editorial_revision,
    publication_identity,
    quality_revision,
    release_revision,
)
from lib.delivery_video import (
    longform_render_fingerprint,
    read_render_manifest,
    record_longform_render,
    record_short_render,
    short_render_fingerprint,
)
from lib.ffprobe import file_fingerprint
from lib.timeline import Timeline

REAL_REMOTE_SCHEDULE = PublishAgent._remote_schedule

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test_key")
    monkeypatch.setenv("UPLOAD_POST_USER", "test_user")
    yield


@pytest.fixture(autouse=True)
def empty_upload_post_calendar(monkeypatch):
    monkeypatch.setattr(PublishAgent, "_remote_schedule", lambda *_args: [])


@pytest.fixture
def episode_dir(tmp_path):
    ed = tmp_path / "ep_test"
    ed.mkdir()
    (ed / "shorts").mkdir()
    (ed / "metadata").mkdir()
    return ed


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _multipart_values(command):
    return [
        value
        for index, value in enumerate(command)
        if index > 0 and command[index - 1] in {"-F", "--form-string"}
    ]


def _publish_config() -> dict:
    return {
        "platforms": {
            "youtube": {"enabled": True},
            "tiktok": {"enabled": True},
            "instagram": {"enabled": True},
            "x": {"enabled": True},
            "podcast_rss": {"enabled": False},
        },
        "schedule": {
            "timezone": "America/Los_Angeles",
            "shorts_per_day_weekday": 1,
            "shorts_per_day_weekend": 2,
        },
    }


def _seed_episode(
    episode_dir,
    *,
    clips=None,
    longform=True,
    config=None,
    youtube_url="https://youtube.com/watch?v=ready",
    schedule=None,
):
    """Write the minimum files publish agent needs to run successfully."""
    config = config or _publish_config()
    episode = {
        "episode_id": "ep_test",
        "title": "Longform",
        "description": "lf desc",
        "status": "ready_for_review",
        "crop_config": {"speakers": [{"label": "Host"}]},
        "longform_edits": [],
    }
    if schedule is not None:
        episode["publish_schedule"] = schedule
    if youtube_url:
        episode["youtube_longform_url"] = youtube_url
        episode["youtube_longform_url_source"] = "supplied"
    _write_json(episode_dir / "episode.json", episode)
    clips = clips or [
        {
            "id": "clip_0",
            "title": "Clip zero",
            "status": "approved",
            "start_seconds": 0,
            "end_seconds": 30,
            "metadata": {
                "youtube": {"title": "yt title", "description": "yt desc"},
                "tiktok": {"caption": "tt cap", "hashtags": ["#a", "#b"]},
                "instagram": {"caption": "ig cap", "hashtags": ["#a"]},
                "x": {"text": "x text"},
            },
        },
    ]
    _write_json(episode_dir / "clips.json", {"clips": clips})
    _write_json(
        episode_dir / "metadata" / "metadata.json",
        {
            "longform": {
                "title": "Longform",
                "description": "lf desc",
                "tags": ["x", "y"],
            },
            "clips": clips,
            "schedule": [],
        },
    )
    # Stub out the actual short video — must exist or the agent skips it.
    for c in clips:
        if c.get("status") != "rejected":
            (episode_dir / "shorts" / f"{c['id']}.mp4").write_bytes(b"fake mp4")
    (episode_dir / "source_merged.mp4").write_bytes(b"source")
    (episode_dir / "work").mkdir(exist_ok=True)
    (episode_dir / "work" / "audio_mix.wav").write_bytes(b"master")
    duration = max(
        60.0,
        *(float(clip.get("end_seconds", 0)) for clip in clips),
    )
    segments = [{"start": 0, "end": duration, "speaker": "BOTH"}]
    _write_json(episode_dir / "segments.json", {"segments": segments})
    audio = episode_dir / "work" / "audio_mix.wav"
    if longform:
        video = episode_dir / "upload_video.mp4"
        video.write_bytes(b"fake mp4")
        record_longform_render(
            episode_dir,
            fingerprint=longform_render_fingerprint(
                episode_dir, episode, config, audio, segments
            ),
            render_mode="speaker_cut",
            timeline=Timeline(duration, [(0, duration)]),
            media={"duration_seconds": duration},
        )
    metadata_by_id = {
        item["id"]: item
        for item in json.loads(
            (episode_dir / "metadata" / "metadata.json").read_text()
        )["clips"]
    }
    for clip in clips:
        if clip.get("status") != "approved":
            continue
        clip_id = clip["id"]
        record = record_short_render(
            episode_dir,
            clip_id,
            fingerprint=short_render_fingerprint(
                episode_dir, episode, config, audio, segments, clip
            ),
            timeline=Timeline(
                duration,
                [(float(clip["start_seconds"]), float(clip["end_seconds"]))],
            ),
            media={
                "duration_seconds": float(clip["end_seconds"])
                - float(clip["start_seconds"])
            },
        )
        clip["approved_render_fingerprint"] = record["fingerprint"]
        clip["approved_revision"] = clip_review_revision(
            clip, record, metadata_by_id.get(clip_id)
        )
    _write_json(episode_dir / "clips.json", {"clips": clips})
    current_editorial_revision = editorial_revision(episode_dir, episode)
    episode["editorial_approval"] = {
        "revision": current_editorial_revision,
        "approved_at": "2026-01-01T00:00:00+00:00",
    }
    if episode.get("youtube_longform_url_source") == "supplied":
        episode["youtube_longform_url_editorial_revision"] = current_editorial_revision
    _write_json(episode_dir / "episode.json", episode)
    episode["publish_approval"] = {
        "revision": release_revision(episode_dir, episode, config=config),
        "approved_at": "2026-01-01T00:01:00+00:00",
    }
    _write_json(episode_dir / "episode.json", episode)
    _write_json(
        episode_dir / "qa" / "qa.json",
        {
            "overall": "pass",
            "quality_revision": quality_revision(episode_dir, episode),
            "checks": [],
        },
    )


def _make_agent(episode_dir, config=None):
    """Construct an agent with a minimal config."""
    return PublishAgent(episode_dir, config or _publish_config())


def _mock_proc(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _write_video_feed_receipt(episode_dir, config):
    video = episode_dir / "upload_video.mp4"
    scan = file_fingerprint(video)
    digest = scan["id"].removeprefix("sha256:")
    render_fingerprint = read_render_manifest(episode_dir)["longform"]["fingerprint"]
    episode = json.loads((episode_dir / "episode.json").read_text())
    public_url = config["podcast"]["r2"]["public_url"].rstrip("/")
    object_key = f"video/{episode_dir.name}/{render_fingerprint}.mp4"
    remote = {
        "status": "ready",
        "size_bytes": scan["size_bytes"],
        "sha256": digest,
        "render_fingerprint": render_fingerprint,
    }
    _write_json(
        episode_dir / "video_feed.json",
        {
            "schema": "cascade.video-podcast-feed/v1",
            "status": "published",
            "episode_id": episode_dir.name,
            "release_revision": episode["publish_approval"]["revision"],
            "editorial_revision": episode["editorial_approval"]["revision"],
            "quality_revision": quality_revision(episode_dir, episode, config=config),
            "publish_approval_current": True,
            "video": {
                "path": str(video),
                "object_key": object_key,
                "url": f"{public_url}/{object_key}",
                "size_bytes": scan["size_bytes"],
                "render_fingerprint": render_fingerprint,
                "sha256": digest,
                "remote": remote,
            },
        },
    )


# ── safety gate ─────────────────────────────────────────────────────────────


class TestSafetyGate:
    """Only an approval for the exact current release may publish."""

    def test_legacy_boolean_does_not_replace_publish_approval(self, env, episode_dir):
        _seed_episode(episode_dir)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode.pop("publish_approval")
        episode["publish_approved"] = True
        _write_json(episode_path, episode)
        agent = _make_agent(episode_dir)
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="release gate blocked"),
        ):
            agent.execute()
        run.assert_not_called()

    def test_refuses_when_episode_json_missing(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="release gate blocked"),
        ):
            agent.execute()
        run.assert_not_called()

    def test_runs_with_current_publish_approval(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"request_id": "req_123"}),
            )
            result = agent.execute()
        assert result["shorts_submitted"] == 1
        assert result["shorts_failed"] == 0

    def test_required_destination_variant_blocks_legacy_short_publish(
        self, env, episode_dir
    ):
        config = _publish_config()
        config["platforms"]["x"]["required_short_variant_id"] = "satisfying_motion_v1"
        _seed_episode(episode_dir, config=config)

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(
                RuntimeError, match="Submit a separate x destination request"
            ),
        ):
            _make_agent(episode_dir, config).execute()

        run.assert_not_called()

    def test_destination_change_after_approval_is_blocked(self, env, episode_dir):
        config = _publish_config()
        _seed_episode(episode_dir, config=config)
        config["platforms"]["tiktok"]["enabled"] = False

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="release gate blocked"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()

    def test_account_change_after_approval_is_blocked(
        self, env, monkeypatch, episode_dir
    ):
        _seed_episode(episode_dir)
        monkeypatch.setenv("UPLOAD_POST_USER", "different-account")

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="release gate blocked"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    @pytest.mark.parametrize("report_state", ["missing", "failed", "stale"])
    def test_refuses_missing_failed_or_stale_qa(self, env, episode_dir, report_state):
        _seed_episode(episode_dir)
        qa_path = episode_dir / "qa" / "qa.json"
        if report_state == "missing":
            qa_path.unlink()
        elif report_state == "failed":
            report = json.loads(qa_path.read_text())
            report["overall"] = "fail"
            _write_json(qa_path, report)
        else:
            episode = json.loads((episode_dir / "episode.json").read_text())
            episode["title"] = "Changed after QA"
            _write_json(episode_dir / "episode.json", episode)

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="release gate blocked"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_pending_clip_is_never_submitted(self, env, episode_dir):
        clips = [
            {
                "id": "clip_0",
                "title": "Pending",
                "status": "pending",
                "start_seconds": 0,
                "end_seconds": 30,
                "metadata": {
                    "youtube": {"title": "Pending", "description": "Copy"},
                    "tiktok": {"caption": "Copy"},
                    "instagram": {"caption": "Copy"},
                    "x": {"text": "Copy"},
                },
            }
        ]
        _seed_episode(episode_dir, clips=clips)

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="release gate blocked"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_episode_editor_copy_is_the_publisher_payload(self, env, episode_dir):
        config = _publish_config()
        config["platforms"]["youtube"]["self_declared_made_for_kids"] = True
        _seed_episode(episode_dir, config=config, youtube_url=None)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode["title"] = "Title saved in the episode editor"
        episode["description"] = "Description saved in the episode editor"
        episode["tags"] = ["editor-copy"]
        episode["publish_approval"] = {
            "revision": release_revision(episode_dir, episode, config=config),
            "approved_at": "2026-01-01T00:02:00+00:00",
        }
        _write_json(episode_path, episode)
        qa_path = episode_dir / "qa" / "qa.json"
        report = json.loads(qa_path.read_text())
        report["quality_revision"] = quality_revision(episode_dir, episode)
        _write_json(qa_path, report)

        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda cmd, **_kwargs: (
                captured.append(cmd)
                or _mock_proc(stdout=json.dumps({"request_id": "request"}))
            )
            _make_agent(episode_dir, config).execute()

        longform = next(cmd for cmd in captured if "upload_video.mp4" in " ".join(cmd))
        fields = _multipart_values(longform)
        assert "youtube_title=Title saved in the episode editor" in fields
        assert "youtube_description=Description saved in the episode editor" in fields
        assert "selfDeclaredMadeForKids=true" in fields


class TestApiKeyGate:
    def test_missing_api_key_raises(self, monkeypatch, episode_dir):
        monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
        monkeypatch.setenv("UPLOAD_POST_USER", "test_user")
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with pytest.raises(RuntimeError, match="UPLOAD_POST_API_KEY"):
            agent.execute()

    def test_missing_user_raises(self, monkeypatch, episode_dir):
        monkeypatch.setenv("UPLOAD_POST_API_KEY", "k")
        monkeypatch.delenv("UPLOAD_POST_USER", raising=False)
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with pytest.raises(RuntimeError, match="UPLOAD_POST_USER"):
            agent.execute()


# ── per-platform error surfacing ────────────────────────────────────────────


class TestErrorSurfacing:
    """Regression tests for the X bug: Upload-Post returning HTTP 200 with an
    error body must be reported as a failure, not a successful submission."""

    def test_api_error_in_200_response_reported_as_failed(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"error": "x_long_text_as_post required"}),
            )
            result = agent.execute()
        assert result["shorts_failed"] == 1
        assert result["shorts_submitted"] == 0
        assert result["shorts"][0]["status"] == "failed"
        assert "x_long_text_as_post" in result["shorts"][0]["error"]
        # The full response body must be persisted for debugging
        assert (
            result["shorts"][0]["response"]["error"] == "x_long_text_as_post required"
        )

    def test_missing_request_id_is_ambiguous(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"unexpected": "shape"}))
            result = agent.execute()
        assert result["shorts_unknown"] == 1
        assert result["shorts"][0]["status"] == "unknown"

    def test_curl_failure_persists_stdout(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout="some body",
                stderr="connection refused",
                returncode=7,
            )
            result = agent.execute()
        assert result["shorts"][0]["status"] == "unknown"
        assert result["shorts"][0]["stdout"] == "some body"
        assert "connection refused" in result["shorts"][0]["error"]

    def test_non_json_response_is_ambiguous(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout="<html>nginx error</html>")
            result = agent.execute()
        assert result["shorts"][0]["status"] == "unknown"
        assert "non-JSON" in result["shorts"][0]["error"]

    def test_successful_submission_persists_response(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"request_id": "req_xyz", "extra": "data"}),
            )
            result = agent.execute()
        assert result["shorts"][0]["status"] == "submitted"
        assert result["shorts"][0]["request_id"].startswith("cascade-short-")
        assert result["shorts"][0]["server_request_id"] == "req_xyz"
        assert result["shorts"][0]["response"]["extra"] == "data"

    def test_platform_failure_is_not_reported_as_success(self, env, episode_dir):
        _seed_episode(episode_dir)
        response = {
            "success": True,
            "results": {
                "youtube": {"success": True, "url": "https://youtu.be/ok"},
                "x": {"success": False, "error": "No access token"},
            },
        }
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps(response))
            agent = _make_agent(episode_dir)
            result = agent.run()
            repeated = agent.execute()

        assert result["shorts_failed"] == 1
        assert result["shorts"][0]["platform_failures"]["x"]["error"] == (
            "No access token"
        )
        assert run.call_count == 1
        assert repeated["shorts"][0]["reused_receipt"] is True
        assert "failed-platform retry" in repeated["next_action"]

    def test_top_level_failure_wins_over_empty_results(self, env, episode_dir):
        _seed_episode(episode_dir)
        response = {"success": False, "error": "rejected", "results": {}}
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps(response))
            result = _make_agent(episode_dir).execute()

        assert result["shorts_failed"] == 1
        assert result["shorts"][0]["status"] == "failed"


# ── X-specific defensive flag ───────────────────────────────────────────────


class TestXLongTextFlag:
    def test_x_long_text_flag_sent_when_x_metadata_present(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        captured = []
        with patch("agents.publish.subprocess.run") as run:

            def _capture(cmd, **_kwargs):
                captured.append(cmd)
                return _mock_proc(stdout=json.dumps({"request_id": "r"}))

            run.side_effect = _capture
            agent.execute()
        # The recorded longform URL makes this a shorts-only retry.
        short_cmd = captured[0]
        # Walk -F flags looking for x_long_text_as_post
        flags = _multipart_values(short_cmd)
        assert any("x_long_text_as_post=true" in f for f in flags), (
            "publish.py must send x_long_text_as_post=true defensively, "
            "or X posts > 280 chars silently fail."
        )

    def test_missing_x_copy_is_blocked_before_submission(self, env, episode_dir):
        clips = [
            {
                "id": "clip_0",
                "title": "no x",
                "status": "approved",
                "start_seconds": 0,
                "end_seconds": 30,
                "metadata": {
                    "youtube": {"title": "yt", "description": "description"},
                    "tiktok": {"caption": "caption"},
                    "instagram": {"caption": "caption"},
                },
            }
        ]
        _seed_episode(episode_dir, clips=clips)
        agent = _make_agent(episode_dir)
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="x copy is missing: text"),
        ):
            agent.execute()
        run.assert_not_called()


# ── YouTube longform link funnel ────────────────────────────────────────────


class TestYouTubeLongformFunnel:
    """Short submissions include a YouTube comment linking to longform."""

    def _seed_with_longform_url(
        self, episode_dir, *, youtube_url=None, spotify_url=None, channel_handle=None
    ):
        config = _publish_config()
        if channel_handle:
            config["podcast"] = {"channel_handle": channel_handle}
        _seed_episode(episode_dir, config=config, youtube_url=None)
        ep_path = episode_dir / "episode.json"
        ep = json.loads(ep_path.read_text())
        if youtube_url is not None:
            ep["youtube_longform_url"] = youtube_url
            ep["youtube_longform_url_source"] = "supplied"
            ep["youtube_longform_url_editorial_revision"] = editorial_revision(
                episode_dir, ep
            )
        if spotify_url is not None:
            ep["spotify_longform_url"] = spotify_url
            ep["spotify_longform_url_source"] = "supplied"
            ep["spotify_longform_url_editorial_revision"] = editorial_revision(
                episode_dir, ep
            )
        ep["publish_approval"] = {
            "revision": release_revision(episode_dir, ep, config=config),
            "approved_at": "2026-01-01T00:02:00+00:00",
        }
        _write_json(ep_path, ep)
        return config

    def _capture_upload_cmds(self, env, episode_dir, agent):
        captured = []
        with patch("agents.publish.subprocess.run") as run:

            def _capture(cmd, **_kwargs):
                captured.append(cmd)
                return _mock_proc(stdout=json.dumps({"request_id": "r"}))

            run.side_effect = _capture
            agent.execute()
        return captured

    def test_first_comment_sent_when_youtube_url_set(self, env, episode_dir):
        config = self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc123",
        )
        agent = _make_agent(episode_dir, config)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        # First call is the short upload
        short_cmd = captured[0]
        flags = _multipart_values(short_cmd)
        first_comment_flags = [
            f for f in flags if f.startswith("youtube_first_comment=")
        ]
        assert len(first_comment_flags) == 1, (
            "publish.py must send youtube_first_comment on every short when "
            "youtube_longform_url is set"
        )
        assert "youtube.com/watch?v=abc123" in first_comment_flags[0]
        assert "Full episode" in first_comment_flags[0]

    def test_first_comment_includes_spotify_when_set(self, env, episode_dir):
        config = self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc",
            spotify_url="https://open.spotify.com/episode/xyz",
        )
        agent = _make_agent(episode_dir, config)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        short_cmd = captured[0]
        flags = _multipart_values(short_cmd)
        first_comment = next(f for f in flags if f.startswith("youtube_first_comment="))
        assert "spotify.com/episode/xyz" in first_comment
        assert "Listen on Spotify" in first_comment

    def test_first_comment_includes_channel_handle(self, env, episode_dir):
        config = self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc",
            channel_handle="@local-pod",
        )
        agent = _make_agent(episode_dir, config)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        short_cmd = captured[0]
        flags = _multipart_values(short_cmd)
        first_comment = next(f for f in flags if f.startswith("youtube_first_comment="))
        assert "@local-pod" in first_comment

    def test_shorts_deferred_when_no_youtube_url(self, env, episode_dir):
        _seed_episode(episode_dir, youtube_url=None)
        agent = _make_agent(episode_dir)
        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda cmd, **_kwargs: (
                captured.append(cmd)
                or _mock_proc(stdout=json.dumps({"request_id": "longform"}))
            )
            result = agent.execute()

        assert result["shorts_deferred"] is True
        assert result["shorts"] == []
        assert len(captured) == 1
        assert "upload_video.mp4" in " ".join(captured[0])

    def test_longform_failure_requires_action(self, env, episode_dir):
        _seed_episode(episode_dir, youtube_url=None)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"success": False, "error": "youtube rejected"})
            )
            result = _make_agent(episode_dir).execute()

        assert result["publish_status"] == "longform_failed"
        assert result["action_required"] is True
        assert "youtube rejected" in result["longform"]["error"]
        assert "awaiting a public URL" not in result["shorts_deferred_reason"]

    def test_longform_idempotent_when_url_already_set(self, env, episode_dir):
        # When youtube_longform_url is already recorded, publish skips the
        # longform re-upload. This is the two-phase flow: longform uploads on
        # the first publish run (URL not yet set), then on the SECOND run
        # (after URL is saved) only shorts upload.
        config = self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc",
        )
        agent = _make_agent(episode_dir, config)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        # Only 1 call: short upload. Longform upload is skipped by idempotency.
        assert len(captured) == 1
        assert "upload_video.mp4" not in " ".join(captured[0])

    def test_receipt_url_keeps_the_approved_batch_current(self, env, episode_dir):
        _seed_episode(episode_dir, youtube_url=None)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        revision = episode["publish_approval"]["revision"]
        identity = _make_agent(episode_dir)._identity(revision, "longform")
        episode["youtube_longform_url"] = "https://youtube.com/watch?v=receipt"
        episode["youtube_longform_url_source"] = "upload_post_receipt"
        episode["youtube_longform_url_external_id"] = identity
        episode["youtube_longform_url_release_revision"] = revision
        _write_json(episode_path, episode)

        captured = self._capture_upload_cmds(env, episode_dir, _make_agent(episode_dir))

        assert len(captured) == 1
        assert "upload_video.mp4" not in " ".join(captured[0])

    def test_unbound_receipt_url_is_rejected(self, env, episode_dir):
        _seed_episode(episode_dir, youtube_url=None)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode["youtube_longform_url"] = "https://youtube.com/watch?v=stale"
        episode["youtube_longform_url_source"] = "upload_post_receipt"
        _write_json(episode_path, episode)

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="does not belong"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_stale_longform_receipt_does_not_skip_current_upload(
        self, env, episode_dir
    ):
        _seed_episode(episode_dir, youtube_url=None)
        _write_json(
            episode_dir / "publish.json",
            {
                "longform": {
                    "status": "submitted",
                    "external_id": "cascade-longform-stale",
                }
            },
        )
        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                captured.append(command)
                or _mock_proc(stdout=json.dumps({"request_id": "current"}))
            )
            result = _make_agent(episode_dir).execute()

        assert len(captured) == 1
        assert "upload_video.mp4" in " ".join(captured[0])
        assert result["longform"].get("reused_receipt") is not True


# ── rejected clips skipped ──────────────────────────────────────────────────


class TestRejectedClips:
    def test_rejected_clip_not_submitted(self, env, episode_dir):
        clips = [
            {
                "id": "clip_0",
                "title": "ok",
                "status": "approved",
                "start_seconds": 0,
                "end_seconds": 30,
                "metadata": {
                    "youtube": {"title": "yt", "description": "description"},
                    "tiktok": {"caption": "caption"},
                    "instagram": {"caption": "caption"},
                    "x": {"text": "post"},
                },
            },
            {
                "id": "clip_1",
                "title": "no",
                "status": "rejected",
                "start_seconds": 30,
                "end_seconds": 60,
                "metadata": {"youtube": {"title": "unused"}},
            },
        ]
        _seed_episode(episode_dir, clips=clips)
        agent = _make_agent(episode_dir)
        upload_calls = []
        with patch("agents.publish.subprocess.run") as run:

            def _capture(cmd, **_kwargs):
                upload_calls.append(cmd)
                return _mock_proc(stdout=json.dumps({"request_id": "r"}))

            run.side_effect = _capture
            agent.execute()
        assert len(upload_calls) == 1
        # Make sure clip_1.mp4 doesn't appear in any upload command
        for cmd in upload_calls:
            assert "clip_1.mp4" not in " ".join(cmd)


class TestReceiptTerminalEvidence:
    @staticmethod
    def _receipt():
        return {
            "clip_id": "clip_0",
            "status": "submitted",
            "job_id": "job-1",
            "platforms": ["youtube", "x"],
        }

    @staticmethod
    def _response():
        return {
            "status": "completed",
            "job_id": "job-1",
            "results": [
                {
                    "platform": "youtube",
                    "success": True,
                    "post_url": "https://youtu.be/current",
                    "job_id": "job-1",
                    "profile_username": "account-a",
                },
                {
                    "platform": "x",
                    "success": False,
                    "job_id": "job-1",
                    "profile_username": "account-a",
                },
            ],
        }

    def test_status_requires_exact_identity_profile_and_all_destinations(self):
        from agents.publish import (
            status_identity_conflicts,
            terminal_destinations_from_status,
        )

        evidence = terminal_destinations_from_status(
            self._receipt(), self._response(), profile_username="account-a"
        )

        assert evidence == {
            "youtube": {
                "state": "published",
                "url": "https://youtu.be/current",
            },
            "x": {"state": "failed"},
        }
        wrong_identity = self._response()
        wrong_identity.pop("job_id")
        for item in wrong_identity["results"]:
            item["job_id"] = "other-job"
        assert (
            terminal_destinations_from_status(
                self._receipt(), wrong_identity, profile_username="account-a"
            )
            is None
        )
        wrong_profile = self._response()
        wrong_profile["results"][1]["profile_username"] = "account-b"
        assert status_identity_conflicts(
            self._receipt(), wrong_profile, profile_username="account-a"
        )
        assert (
            terminal_destinations_from_status(
                self._receipt(), wrong_profile, profile_username="account-a"
            )
            is None
        )
        assert not status_identity_conflicts(
            self._receipt(),
            {"status": "processing", "job_id": "job-1"},
            profile_username="account-a",
        )
        incomplete = self._response()
        incomplete["results"].pop()
        assert (
            terminal_destinations_from_status(
                self._receipt(), incomplete, profile_username="account-a"
            )
            is None
        )
        mixed_identity_receipt = {
            **self._receipt(),
            "request_id": "request-1",
        }
        mixed_identity = self._response()
        mixed_identity["request_id"] = "request-1"
        mixed_identity["job_id"] = "wrong-job"
        for item in mixed_identity["results"]:
            item["request_id"] = "request-1"
            item["job_id"] = "wrong-job"
        assert (
            terminal_destinations_from_status(
                mixed_identity_receipt,
                mixed_identity,
                profile_username="account-a",
            )
            is None
        )

        malformed = self._response()
        malformed["results"].append("not-a-provider-result")
        assert (
            terminal_destinations_from_status(
                self._receipt(), malformed, profile_username="account-a"
            )
            is None
        )

    @pytest.mark.parametrize(
        "change",
        (
            {"post_url": None},
            {"status": "Queued"},
            {"fallback_to_inbox": True},
            {"status": "failed"},
        ),
    )
    def test_nonpublic_success_is_not_terminal(self, change):
        from agents.publish import terminal_destinations_from_status

        response = self._response()
        response["results"][0].update(change)

        assert (
            terminal_destinations_from_status(
                self._receipt(), response, profile_username="account-a"
            )
            is None
        )

    @pytest.mark.parametrize("state", ("scheduled", "submitted", "unknown", "waiting"))
    def test_unresolved_failure_state_is_not_definitive(self, state):
        from agents.publish import terminal_destinations_from_status

        response = self._response()
        response["results"][1]["status"] = state

        assert (
            terminal_destinations_from_status(
                self._receipt(), response, profile_username="account-a"
            )
            is None
        )

    def test_failure_with_public_url_is_not_definitive(self):
        from agents.publish import terminal_destinations_from_status

        response = self._response()
        response["results"][1]["post_url"] = "https://x.com/account/status/1"

        assert (
            terminal_destinations_from_status(
                self._receipt(), response, profile_username="account-a"
            )
            is None
        )

    @pytest.mark.parametrize(
        "destinations",
        (
            {"youtube": {"state": "published"}},
            {
                "youtube": {
                    "state": "failed",
                    "url": "https://youtu.be/contradiction",
                }
            },
        ),
    )
    def test_persisted_terminal_proof_uses_same_url_invariants(self, destinations):
        from agents.publish import receipt_terminal_destinations

        receipt = {
            "clip_id": "clip_0",
            "status": "published",
            "request_id": "request-1",
            "platforms": ["youtube"],
            "terminal_destinations": destinations,
            "status_history": [
                {
                    "profile_username": "account-a",
                    "evidence_source": "status",
                    "terminal_destinations": destinations,
                }
            ],
        }

        assert (
            receipt_terminal_destinations(receipt, profile_username="account-a") is None
        )

    def test_history_filters_locally_and_exact_in_progress_blocks(self):
        from agents.publish import terminal_destinations_from_history

        exact = self._response()["results"]
        unrelated = {
            **exact[0],
            "platform": "instagram",
            "job_id": "other-job",
        }
        history = {"history": [unrelated, *exact], "in_progress": []}

        assert (
            terminal_destinations_from_history(
                self._receipt(), history, profile_username="account-a"
            )
            is not None
        )
        history["in_progress"] = [
            {
                "job_id": "job-1",
                "profile_username": "account-a",
            }
        ]
        assert (
            terminal_destinations_from_history(
                self._receipt(), history, profile_username="account-a"
            )
            is None
        )
        for unverified_profile in (None, "account-b"):
            history["in_progress"] = [
                {
                    "job_id": "job-1",
                    **(
                        {"profile_username": unverified_profile}
                        if unverified_profile is not None
                        else {}
                    ),
                }
            ]
            assert (
                terminal_destinations_from_history(
                    self._receipt(), history, profile_username="account-a"
                )
                is None
            )

        history["in_progress"] = []
        history["history"][1].pop("profile_username")
        assert (
            terminal_destinations_from_history(
                self._receipt(), history, profile_username="account-a"
            )
            is None
        )

    def test_authorization_requires_explicit_base_variant_identity(self):
        from agents.publish import (
            short_receipt_history_revision,
            valid_rerelease_authorization,
        )
        from lib.short_variants import distribution_release_revision

        publish = {"shorts": []}
        authorization = {
            "request_id": "323e4567-e89b-12d3-a456-426614174000",
            "actor": "Sam",
            "reason": "Re-release rebuilt Base clip",
            "target_revision": "sha256:target",
            "render_fingerprint": "sha256:render",
            "receipt_history_revision": short_receipt_history_revision(
                publish, "clip_0"
            ),
            "created_at": "2026-09-13T20:00:00+00:00",
        }
        authorization["revision"] = distribution_release_revision(
            request_id=authorization["request_id"],
            actor=authorization["actor"],
            reason=authorization["reason"],
            variant_id=None,
            target_revision=authorization["target_revision"],
            render_fingerprint=authorization["render_fingerprint"],
            receipt_history_revision=authorization["receipt_history_revision"],
        )

        assert not valid_rerelease_authorization(
            publish,
            {"id": "clip_0", "distribution_release": authorization},
            {
                "variant_id": None,
                "revision": "sha256:target",
                "render_fingerprint": "sha256:render",
            },
        )


class TestVersionedShorts:
    @staticmethod
    def _variant_snapshot(episode_dir, config):
        from agents.qa import quality_snapshot

        snapshot = quality_snapshot(episode_dir, config=config)
        snapshot["release_gate"]["revision"] = "sha256:release-with-variant"
        snapshot["release_gate"]["short_versions"]["clip_0"] = {
            "version": "background_motion_v1",
            "variant_id": "background_motion_v1",
            "current": True,
            "approval_current": True,
            "revision": "sha256:variant-review",
            "path": ("short_variants/background_motion_v1/clip_0.mp4"),
            "render_fingerprint": "sha256:variant-render",
        }
        return snapshot

    def test_selected_variant_path_and_identity_are_persisted_and_reused(
        self, env, episode_dir
    ):
        config = _publish_config()
        _seed_episode(episode_dir, config=config)
        variant = episode_dir / "short_variants" / "background_motion_v1" / "clip_0.mp4"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"approved variant")
        snapshot = self._variant_snapshot(episode_dir, config)
        commands = []
        with (
            patch("agents.publish.quality_snapshot", return_value=snapshot),
            patch("agents.publish.subprocess.run") as run,
        ):
            run.side_effect = lambda command, **_kwargs: (
                commands.append(command)
                or _mock_proc(stdout=json.dumps({"request_id": "variant-job"}))
            )
            agent = _make_agent(episode_dir, config)
            first = agent.run()
            second = agent.execute()

        assert len(commands) == 1
        assert f"video=@{variant}" in commands[0]
        receipt = first["shorts"][0]
        assert receipt["version"] == "background_motion_v1"
        assert receipt["variant_id"] == "background_motion_v1"
        assert receipt["render_fingerprint"] == "sha256:variant-render"
        assert receipt["approval_revision"] == "sha256:variant-review"
        assert second["shorts"][0]["reused_receipt"] is True
        editorial_revision_value = snapshot["approvals"]["editorial"]["revision"]
        assert first["longform"]["external_id"] == agent._identity(
            editorial_revision_value, "longform"
        )
        assert first["longform"]["external_id"] != agent._identity(
            snapshot["release_gate"]["revision"], "longform"
        )

    @pytest.mark.parametrize("legacy_receipt", [False, True])
    def test_short_release_change_reuses_current_longform_receipt(
        self, env, episode_dir, legacy_receipt
    ):
        config = _publish_config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        variant = episode_dir / "short_variants" / "background_motion_v1" / "clip_0.mp4"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"approved variant")
        snapshot = self._variant_snapshot(episode_dir, config)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        longform_revision = snapshot["approvals"]["editorial"]["revision"]
        prior_release = "sha256:prior-base-release"
        longform_identity = publication_identity(
            episode_dir.name,
            prior_release if legacy_receipt else longform_revision,
            "longform",
        )
        episode.update(
            youtube_longform_url="https://youtube.com/watch?v=current-longform",
            youtube_longform_url_source="upload_post_receipt",
            youtube_longform_url_external_id=longform_identity,
            youtube_longform_url_release_revision=prior_release,
            publish_approval={
                "revision": snapshot["release_gate"]["revision"],
                "approved_at": "2026-01-01T00:03:00+00:00",
            },
        )
        if not legacy_receipt:
            episode["youtube_longform_url_editorial_revision"] = longform_revision
        _write_json(episode_path, episode)
        if legacy_receipt:
            qa_report = json.loads((episode_dir / "qa" / "qa.json").read_text())
            qa_report.update(
                editorial_revision=longform_revision,
                release_revision=prior_release,
            )
            _write_json(episode_dir / "qa" / "qa.json", qa_report)
        longform_receipt = {
            "status": "submitted",
            "external_id": longform_identity,
        }
        if not legacy_receipt:
            longform_receipt["editorial_revision"] = longform_revision
        _write_json(
            episode_dir / "publish.json",
            {
                "release_revision": prior_release,
                "longform": longform_receipt,
                "shorts": [],
            },
        )
        commands = []
        with (
            patch("agents.publish.quality_snapshot", return_value=snapshot),
            patch("agents.publish.subprocess.run") as run,
        ):
            run.side_effect = lambda command, **_kwargs: (
                commands.append(command)
                or _mock_proc(stdout=json.dumps({"request_id": "variant-job"}))
            )
            result = _make_agent(episode_dir, config).execute()

        assert len(commands) == 1
        assert f"video=@{variant}" in commands[0]
        assert "upload_video.mp4" not in " ".join(commands[0])
        assert result["longform"]["status"] == "already_submitted"
        assert (
            result["longform"]["youtube_longform_url"]
            == "https://youtube.com/watch?v=current-longform"
        )
        stored = json.loads(episode_path.read_text())
        assert stored["youtube_longform_url_editorial_revision"] == longform_revision

    @pytest.mark.parametrize(
        "receipt_identity",
        (
            {"version": "base"},
            {
                "version": "background_motion_v1",
                "variant_id": "background_motion_v1",
                "render_fingerprint": "sha256:replaced-render",
                "approval_revision": "sha256:variant-review",
            },
        ),
    )
    def test_partial_or_mismatched_version_receipt_fails_closed(
        self, env, episode_dir, receipt_identity
    ):
        config = _publish_config()
        _seed_episode(episode_dir, config=config)
        variant = episode_dir / "short_variants" / "background_motion_v1" / "clip_0.mp4"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"approved variant")
        snapshot = self._variant_snapshot(episode_dir, config)
        revision = snapshot["release_gate"]["revision"]
        identity = _make_agent(episode_dir, config)._identity(
            revision, "short", "clip_0"
        )
        _write_json(
            episode_dir / "publish.json",
            {
                "release_revision": revision,
                "shorts": [
                    {
                        "clip_id": "clip_0",
                        "status": "submitted",
                        "external_id": identity,
                        **receipt_identity,
                    }
                ],
            },
        )

        with (
            patch("agents.publish.quality_snapshot", return_value=snapshot),
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="does not match its selected version"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()

    def test_fieldless_legacy_receipt_is_accepted_only_for_base(self, episode_dir):
        agent = _make_agent(episode_dir)
        legacy_receipt = {"clip_id": "clip_0", "status": "submitted"}
        base = {
            "version": "base",
            "variant_id": None,
            "render_fingerprint": "sha256:base-render",
            "revision": "sha256:base-review",
        }
        variant = {
            **base,
            "version": "background_motion_v1",
            "variant_id": "background_motion_v1",
        }

        assert agent._receipt_matches_version(legacy_receipt, base) is True
        assert agent._receipt_matches_version(legacy_receipt, variant) is False

    def test_deferred_longform_run_preserves_prior_short_receipts(
        self, env, episode_dir
    ):
        _seed_episode(episode_dir, youtube_url=None)
        prior = {
            "clip_id": "clip_0",
            "status": "submitted",
            "external_id": "cascade-short-prior-release",
            "scheduled": True,
        }
        _write_json(
            episode_dir / "publish.json",
            {"release_revision": "sha256:prior-release", "shorts": [prior]},
        )

        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"request_id": "new-longform"})
            )
            result = _make_agent(episode_dir).run()

        assert result["shorts_deferred"] is True
        stored = json.loads((episode_dir / "publish.json").read_text())
        assert stored["shorts"] == [{**prior, "historical_receipt": True}]

    def test_old_release_receipt_blocks_same_base_from_being_submitted_again(
        self, env, episode_dir
    ):
        _seed_episode(episode_dir)
        _write_json(
            episode_dir / "publish.json",
            {
                "release_revision": "sha256:prior-release",
                "shorts": [
                    {
                        "clip_id": "clip_0",
                        "status": "submitted",
                        "external_id": "cascade-short-prior-release",
                    }
                ],
            },
        )

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="explicit re-release identity"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_explicit_rerelease_uses_new_identity_and_preserves_history(
        self, env, episode_dir
    ):
        from agents.publish import short_receipt_history_revision
        from agents.qa import quality_snapshot
        from lib.short_variants import distribution_release_revision

        config = _publish_config()
        _seed_episode(episode_dir, config=config)
        old_receipt = {
            "clip_id": "clip_0",
            "status": "published",
            "request_id": "old-request",
            "platforms": ["youtube"],
            "response": {
                "status": "completed",
                "request_id": "old-request",
                "results": [
                    {
                        "platform": "youtube",
                        "success": True,
                        "post_url": "https://youtu.be/old-short",
                        "request_id": "old-request",
                    }
                ],
            },
        }
        publish = {"release_revision": "sha256:old-release", "shorts": [old_receipt]}
        _write_json(episode_dir / "publish.json", publish)
        clips_path = episode_dir / "clips.json"
        clips = json.loads(clips_path.read_text())
        clip = clips["clips"][0]
        version = quality_snapshot(episode_dir, config=config)["release_gate"][
            "short_versions"
        ]["clip_0"]
        request = {
            "request_id": "47db1913-4d32-4acf-bcfe-31763c50e9c2",
            "actor": "release-operator",
            "reason": "Rebuilt episode with current media",
            "variant_id": None,
            "target_revision": version["revision"],
            "render_fingerprint": version["render_fingerprint"],
            "receipt_history_revision": short_receipt_history_revision(
                publish, "clip_0"
            ),
            "created_at": "2026-01-01T00:02:00+00:00",
        }
        request["revision"] = distribution_release_revision(
            request_id=request["request_id"],
            actor=request["actor"],
            reason=request["reason"],
            variant_id=None,
            target_revision=request["target_revision"],
            render_fingerprint=request["render_fingerprint"],
            receipt_history_revision=request["receipt_history_revision"],
        )
        clip["distribution_release"] = request
        _write_json(clips_path, clips)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        current = quality_snapshot(episode_dir, config=config)["release_gate"]
        episode["publish_approval"] = {
            "revision": current["revision"],
            "approved_at": "2026-01-01T00:03:00+00:00",
        }
        _write_json(episode_path, episode)
        commands = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                commands.append(command)
                or _mock_proc(stdout=json.dumps({"request_id": "new-request"}))
            )
            agent = _make_agent(episode_dir, config)
            first = agent.run()
            second = agent.execute()

        assert len(commands) == 1
        current_receipt = next(
            receipt
            for receipt in first["shorts"]
            if not receipt.get("historical_receipt")
        )
        assert current_receipt["rerelease_request_id"] == request["request_id"]
        assert current_receipt["rerelease_actor"] == request["actor"]
        assert current_receipt["rerelease_reason"] == request["reason"]
        assert (
            current_receipt["rerelease_authorization_revision"] == request["revision"]
        )
        assert current_receipt["external_id"] != old_receipt["request_id"]
        assert any(
            receipt.get("request_id") == "old-request"
            and receipt.get("historical_receipt") is True
            for receipt in first["shorts"]
        )
        assert second["shorts"][0]["reused_receipt"] is True

    def test_changed_receipt_history_invalidates_prepared_rerelease(
        self, env, episode_dir
    ):
        from agents.publish import short_receipt_history_revision
        from agents.qa import quality_snapshot
        from lib.short_variants import distribution_release_revision

        config = _publish_config()
        _seed_episode(episode_dir, config=config)
        publish = {
            "shorts": [
                {
                    "clip_id": "clip_0",
                    "status": "published",
                    "request_id": "old-request",
                    "platforms": ["youtube"],
                    "response": {
                        "status": "completed",
                        "request_id": "old-request",
                        "results": [
                            {
                                "platform": "youtube",
                                "success": True,
                                "post_url": "https://youtu.be/old",
                                "request_id": "old-request",
                            }
                        ],
                    },
                }
            ]
        }
        _write_json(episode_dir / "publish.json", publish)
        clips_path = episode_dir / "clips.json"
        clips = json.loads(clips_path.read_text())
        version = quality_snapshot(episode_dir, config=config)["release_gate"][
            "short_versions"
        ]["clip_0"]
        authorization = {
            "request_id": "8e05ec16-3b85-4929-96ef-24d8e609576c",
            "actor": "release-operator",
            "reason": "Rebuilt episode with current media",
            "variant_id": None,
            "target_revision": version["revision"],
            "render_fingerprint": version["render_fingerprint"],
            "receipt_history_revision": short_receipt_history_revision(
                publish, "clip_0"
            ),
            "created_at": "2026-01-01T00:02:00+00:00",
        }
        authorization["revision"] = distribution_release_revision(
            **{
                key: value
                for key, value in authorization.items()
                if key != "created_at"
            }
        )
        clips["clips"][0]["distribution_release"] = authorization
        _write_json(clips_path, clips)
        publish["shorts"][0]["status_history"] = [{"status": "published"}]
        _write_json(episode_dir / "publish.json", publish)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        gate = quality_snapshot(episode_dir, config=config)["release_gate"]
        episode["publish_approval"] = {
            "revision": gate["revision"],
            "approved_at": "2026-01-01T00:03:00+00:00",
        }
        _write_json(episode_path, episode)

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="explicit re-release identity"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()

    @pytest.mark.parametrize(
        "invalid_receipts",
        (
            "not-a-list",
            [{"status": "submitted", "external_id": "unattributed"}],
        ),
    )
    def test_uninspectable_receipt_history_blocks_before_external_requests(
        self, env, episode_dir, invalid_receipts
    ):
        _seed_episode(episode_dir)
        _write_json(episode_dir / "publish.json", {"shorts": invalid_receipts})

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="Cannot inspect prior short"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_media_replacement_after_initial_gate_snapshot_blocks_submission(
        self, env, episode_dir
    ):
        from agents.qa import quality_snapshot

        config = _publish_config()
        _seed_episode(episode_dir, config=config)
        initial = quality_snapshot(episode_dir, config=config)
        short = episode_dir / "shorts" / "clip_0.mp4"
        calls = 0

        def snapshot(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return initial
            short.write_bytes(b"replacement render")
            return quality_snapshot(episode_dir, config=config)

        with (
            patch("agents.publish.quality_snapshot", side_effect=snapshot),
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="Release media"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()

    def test_longform_replacement_after_initial_gate_blocks_all_submission(
        self, env, episode_dir
    ):
        from agents.qa import quality_snapshot

        config = _publish_config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        initial = quality_snapshot(episode_dir, config=config)
        longform = episode_dir / "upload_video.mp4"
        calls = 0

        def snapshot(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return initial
            longform.write_bytes(b"replacement longform render")
            return quality_snapshot(episode_dir, config=config)

        with (
            patch("agents.publish.quality_snapshot", side_effect=snapshot),
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="Release media"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()


class TestIdempotency:
    @staticmethod
    def _values(command, flag):
        return [
            value
            for index, value in enumerate(command)
            if index > 0 and command[index - 1] == flag
        ]

    def test_short_timeout_is_not_automatically_resubmitted(self, env, episode_dir):
        _seed_episode(episode_dir)
        commands = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                commands.append(command)
                or (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 600))
            )
            agent = _make_agent(episode_dir)
            first = agent.run()
            second = agent.execute()

        assert len(commands) == 1
        headers = self._values(commands[0], "-H")
        fields = _multipart_values(commands[0])
        identity = first["shorts"][0]["external_id"]
        assert f"Idempotency-Key: {identity}" in headers
        assert f"request_id={identity}" in fields
        assert f"external_id={identity}" in fields
        assert first["shorts"][0]["status"] == "unknown"
        assert second["shorts"][0]["status"] == "unknown"
        assert second["shorts"][0]["reused_receipt"] is True

    def test_longform_upload_has_stable_identity(self, env, episode_dir):
        _seed_episode(episode_dir, youtube_url=None)
        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                captured.append(command)
                or _mock_proc(stdout=json.dumps({"request_id": "longform"}))
            )
            _make_agent(episode_dir).execute()

        headers = self._values(captured[0], "-H")
        fields = _multipart_values(captured[0])
        assert any(
            value.startswith("Idempotency-Key: cascade-longform-") for value in headers
        )
        assert any(value.startswith("request_id=cascade-longform-") for value in fields)
        assert any(
            value.startswith("external_id=cascade-longform-") for value in fields
        )

    def test_longform_timeout_is_not_automatically_resubmitted(self, env, episode_dir):
        _seed_episode(episode_dir, youtube_url=None)
        commands = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                commands.append(command)
                or (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 1200))
            )
            agent = _make_agent(episode_dir)
            first = agent.run()
            second = agent.execute()

        assert len(commands) == 1
        assert first["longform"]["status"] == "unknown"
        assert second["longform"]["status"] == "unknown"
        assert second["longform"]["reused_receipt"] is True


class TestLongformTransport:
    @staticmethod
    def _config():
        config = _publish_config()
        config["platforms"]["video_podcast_rss"] = {"enabled": True}
        config["podcast"] = {
            "title": "Show",
            "description": "Description",
            "author": "Host",
            "artwork_url": "https://media.example.test/art.jpg",
            "link": "https://example.test",
            "owner_email": "host@example.test",
            "explicit": "false",
            "r2": {
                "bucket": "media",
                "public_url": "https://media.example.test",
            },
        }
        return config

    @pytest.fixture
    def remote_object(self):
        with (
            patch(
                "agents.video_feed.VideoFeedAgent._r2_client",
                return_value=object(),
            ),
            patch("agents.video_feed.VideoFeedAgent._head_video_object") as head,
        ):
            head.side_effect = lambda _client, inputs: {
                "status": "ready",
                "size_bytes": inputs["video_size"],
                "sha256": inputs["content_sha256"],
                "render_fingerprint": inputs["render_fingerprint"],
            }
            yield head

    def test_verified_video_feed_object_is_submitted_by_url(
        self, env, episode_dir, remote_object
    ):
        config = self._config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        _write_video_feed_receipt(episode_dir, config)
        commands = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                commands.append(command)
                or _mock_proc(stdout=json.dumps({"request_id": "remote-request"}))
            )
            result = _make_agent(episode_dir, config).execute()

        fields = _multipart_values(commands[0])
        transport = result["longform"]["transport"]
        assert f"video={transport['url']}" in fields
        assert not any(value.startswith("video=@") for value in fields)
        assert transport["kind"] == "verified_public_url"
        assert transport["sha256"].startswith("sha256:")

    def test_stale_video_feed_proof_blocks_before_submission(
        self, env, episode_dir, remote_object
    ):
        config = self._config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        _write_video_feed_receipt(episode_dir, config)
        path = episode_dir / "video_feed.json"
        receipt = json.loads(path.read_text())
        receipt["release_revision"] = "sha256:stale"
        _write_json(path, receipt)

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="transport proof is stale"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()

    def test_missing_remote_object_blocks_before_submission(
        self, env, episode_dir, remote_object
    ):
        config = self._config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        _write_video_feed_receipt(episode_dir, config)
        remote_object.side_effect = None
        remote_object.return_value = None

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="transport proof is stale"),
        ):
            _make_agent(episode_dir, config).execute()
        run.assert_not_called()

    def test_verified_url_retries_only_definitive_legacy_413(
        self, env, episode_dir, remote_object
    ):
        config = self._config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        _write_video_feed_receipt(episode_dir, config)
        episode = json.loads((episode_dir / "episode.json").read_text())
        identity = publication_identity(
            episode_dir.name,
            episode["editorial_approval"]["revision"],
            "longform",
        )
        _write_json(
            episode_dir / "publish.json",
            {
                "shorts": [],
                "longform": {
                    "external_id": identity,
                    "idempotency_key": identity,
                    "request_id": identity,
                    "status": "unknown",
                    "error": (
                        "non-JSON response from Upload-Post: "
                        "<h1>413 Request Entity Too Large</h1>"
                    ),
                },
            },
        )
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"request_id": "remote-request"})
            )
            result = _make_agent(episode_dir, config).execute()

        assert run.call_count == 1
        assert result["longform"]["external_id"] == identity
        assert result["longform"]["attempt_history"][0]["status"] == "unknown"
        assert (
            "413 Request Entity Too Large"
            in result["longform"]["attempt_history"][0]["error"]
        )

    def test_verified_url_does_not_retry_generic_unknown(
        self, env, episode_dir, remote_object
    ):
        config = self._config()
        _seed_episode(episode_dir, config=config, youtube_url=None)
        _write_video_feed_receipt(episode_dir, config)
        episode = json.loads((episode_dir / "episode.json").read_text())
        identity = publication_identity(
            episode_dir.name,
            episode["editorial_approval"]["revision"],
            "longform",
        )
        _write_json(
            episode_dir / "publish.json",
            {
                "shorts": [],
                "longform": {
                    "external_id": identity,
                    "request_id": identity,
                    "status": "unknown",
                    "error": "connection lost after request body was sent",
                },
            },
        )
        with patch("agents.publish.subprocess.run") as run:
            result = _make_agent(episode_dir, config).execute()

        run.assert_not_called()
        assert result["longform"]["status"] == "unknown"
        assert result["longform"]["reused_receipt"] is True

    def test_http_413_is_recorded_as_definitive_failure(self, env, episode_dir):
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout="<html><h1>413 Request Entity Too Large</h1></html>",
            )
            result = PublishAgent._submit(["curl"], 10, "identity")

        assert result["status"] == "failed"
        assert result["http_status"] == 413
        assert "HTTP 413" in result["error"]

    def test_structured_rejection_remains_definitive_failure(self):
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"success": False, "error": "bad metadata"})
            )
            result = PublishAgent._submit(["curl"], 10, "identity")

        assert result["status"] == "failed"
        assert result["error"] == "bad metadata"


# ── schedule conversion ─────────────────────────────────────────────────────


class TestScheduleConversion:
    def test_spring_dst_uses_new_local_offset(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        sched = {"day_offset": 1, "time_slot": "morning"}
        reference = datetime(2026, 3, 7, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        dt = agent._schedule_to_datetime(
            sched, "America/Los_Angeles", reference=reference
        )
        assert dt.isoformat() == "2026-03-08T09:00:00-07:00"
        assert dt.utcoffset() == timedelta(hours=-7)

    def test_fall_dst_uses_new_local_offset(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        sched = {"day_offset": 1, "time_slot": "evening"}
        reference = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
        dt = agent._schedule_to_datetime(
            sched, "America/Los_Angeles", reference=reference
        )
        assert dt.isoformat() == "2026-11-01T18:00:00-08:00"
        assert dt.utcoffset() == timedelta(hours=-8)

    def test_absolute_date_preserves_approved_local_time(self, env, episode_dir):
        dt = _make_agent(episode_dir)._schedule_to_datetime(
            {"scheduled_date": "2026-11-01T18:00:00-08:00"},
            "America/Los_Angeles",
        )
        assert dt.isoformat() == "2026-11-01T18:00:00-08:00"

    def test_absolute_date_still_requires_an_explicit_offset(self, env, episode_dir):
        with pytest.raises(ValueError, match="no UTC offset"):
            _make_agent(episode_dir)._schedule_to_datetime(
                {"scheduled_date": "2026-09-19T09:00:00"},
                "America/Los_Angeles",
            )

    def test_absolute_date_requires_matching_timezone_offset(self, env, episode_dir):
        with pytest.raises(ValueError, match="offset does not match"):
            _make_agent(episode_dir)._schedule_to_datetime(
                {"scheduled_date": "2026-07-01T09:00:00-08:00"},
                "America/Los_Angeles",
            )

    @pytest.mark.parametrize("offset", ["-07:00", "-08:00"])
    def test_absolute_date_rejects_ambiguous_dst_fold(self, env, episode_dir, offset):
        with pytest.raises(ValueError, match="ambiguous or nonexistent"):
            _make_agent(episode_dir)._schedule_to_datetime(
                {"scheduled_date": f"2026-11-01T01:30:00{offset}"},
                "America/Los_Angeles",
            )

    @pytest.mark.parametrize(
        "schedule,error",
        [
            ({"day_offset": -1, "time_slot": "morning"}, "cannot be negative"),
            ({"day_offset": 1, "time_slot": "midnight"}, "unknown time_slot"),
        ],
    )
    def test_relative_schedule_rejects_invalid_values(
        self, env, episode_dir, schedule, error
    ):
        reference = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
        with pytest.raises(ValueError, match=error):
            _make_agent(episode_dir)._schedule_to_datetime(
                schedule, "America/Los_Angeles", reference=reference
            )

    def test_generate_schedule_distributes_clips_across_days(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        clips = [{"id": f"clip_{i}"} for i in range(5)]
        reference = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Los_Angeles"))
        sched = agent._generate_schedule(
            clips,
            weekday_per_day=1,
            weekend_per_day=2,
            reference=reference,
        )
        assert len(sched) == 5
        assert all(s["day_offset"] >= 1 for s in sched)
        assert sorted(s["clip_id"] for s in sched) == [f"clip_{i}" for i in range(5)]

    def test_stale_approval_uses_current_time_as_schedule_reference(
        self, env, episode_dir
    ):
        zone = ZoneInfo("America/Los_Angeles")
        before = datetime.now(zone)
        reference = _make_agent(episode_dir)._schedule_reference(
            {"publish_approval": {"approved_at": "2020-01-01T00:00:00-08:00"}},
            "America/Los_Angeles",
        )
        after = datetime.now(zone)
        assert before <= reference <= after


class TestScheduleReservation:
    reference = datetime(2099, 1, 4, 12, tzinfo=ZoneInfo("America/Los_Angeles"))

    @pytest.fixture(autouse=True)
    def fixed_schedule_reference(self, monkeypatch):
        monkeypatch.setattr(
            PublishAgent,
            "_schedule_reference",
            staticmethod(lambda *_args: self.reference),
        )

    @staticmethod
    def _scheduled_field(command):
        fields = _multipart_values(command)
        return next(value for value in fields if value.startswith("scheduled_date="))

    def test_remote_job_reserves_global_daily_slot(self, env, monkeypatch, episode_dir):
        _seed_episode(episode_dir)
        monkeypatch.setattr(
            PublishAgent,
            "_remote_schedule",
            lambda *_args: [
                {
                    "job_id": "other-job",
                    "external_id": "another-episode",
                    "scheduled_date": "2099-01-05T18:00:00Z",
                }
            ],
        )
        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                captured.append(command)
                or _mock_proc(stdout=json.dumps({"job_id": "new-job"}))
            )
            _make_agent(episode_dir).execute()

        assert self._scheduled_field(captured[0]) == (
            "scheduled_date=2099-01-06T09:00:00"
        )

    def test_local_episode_receipt_reserves_global_daily_slot(self, env, episode_dir):
        _seed_episode(episode_dir)
        other = episode_dir.parent / "ep_other"
        other.mkdir()
        _write_json(
            other / "publish.json",
            {
                "profile_username": "test_user",
                "shorts": [
                    {
                        "status": "submitted",
                        "scheduled": True,
                        "scheduled_date": "2099-01-05T09:00:00-08:00",
                        "request_id": "other-job",
                    }
                ],
            },
        )
        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                captured.append(command)
                or _mock_proc(stdout=json.dumps({"job_id": "new-job"}))
            )
            _make_agent(episode_dir).execute()

        assert self._scheduled_field(captured[0]) == (
            "scheduled_date=2099-01-06T09:00:00"
        )

    def test_explicit_collision_blocks_before_upload(
        self, env, monkeypatch, episode_dir
    ):
        _seed_episode(
            episode_dir,
            schedule=[{"clip_id": "clip_0", "day_offset": 1, "time_slot": "morning"}],
        )
        monkeypatch.setattr(
            PublishAgent,
            "_remote_schedule",
            lambda *_args: [
                {
                    "job_id": "other-job",
                    "scheduled_date": "2099-01-05T17:00:00Z",
                }
            ],
        )
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="Schedule collision"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_absolute_schedule_is_sent_as_local_wall_time(self, env, episode_dir):
        _seed_episode(
            episode_dir,
            schedule=[
                {
                    "clip_id": "clip_0",
                    "scheduled_date": "2099-07-01T09:00:00-07:00",
                }
            ],
        )
        captured = []
        with patch("agents.publish.subprocess.run") as run:
            run.side_effect = lambda command, **_kwargs: (
                captured.append(command)
                or _mock_proc(stdout=json.dumps({"job_id": "scheduled-job"}))
            )
            result = _make_agent(episode_dir).execute()

        assert self._scheduled_field(captured[0]) == (
            "scheduled_date=2099-07-01T09:00:00"
        )
        assert result["shorts"][0]["job_id"] == "scheduled-job"
        assert result["shorts"][0]["request_id"].startswith("cascade-short-")

    def test_stale_absolute_schedule_blocks_before_upload(self, env, episode_dir):
        _seed_episode(
            episode_dir,
            schedule=[
                {
                    "clip_id": "clip_0",
                    "scheduled_date": "2025-01-01T09:00:00-08:00",
                }
            ],
        )
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="not in the future"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_duplicate_external_labels_do_not_collapse_remote_jobs(
        self, env, monkeypatch, episode_dir
    ):
        monkeypatch.setattr(
            PublishAgent,
            "_remote_schedule",
            lambda *_args: [
                {
                    "job_id": "job-one",
                    "external_id": "reused-label",
                    "scheduled_date": "2099-01-05T17:00:00Z",
                },
                {
                    "job_id": "job-two",
                    "external_id": "reused-label",
                    "scheduled_date": "2099-01-05T18:00:00Z",
                },
            ],
        )

        occupied = _make_agent(episode_dir)._occupied_schedule("key", "test_user")

        assert {item["job_id"] for item in occupied} == {"job-one", "job-two"}

    def test_offsetless_remote_calendar_date_is_utc(
        self, env, monkeypatch, episode_dir
    ):
        monkeypatch.setattr(
            PublishAgent,
            "_remote_schedule",
            lambda *_args: [
                {
                    "job_id": "remote-job",
                    "scheduled_date": "2026-09-19T16:00:00",
                }
            ],
        )

        occupied = _make_agent(episode_dir)._occupied_schedule("key", "test_user")

        assert occupied[0]["scheduled_at"].isoformat() == "2026-09-19T16:00:00+00:00"

    def test_malformed_sibling_receipt_blocks_before_upload(self, env, episode_dir):
        _seed_episode(episode_dir)
        other = episode_dir.parent / "ep_other"
        other.mkdir()
        (other / "publish.json").write_text("not json")

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="Cannot inspect local publish receipt"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_malformed_current_receipt_blocks_before_upload(self, env, episode_dir):
        _seed_episode(episode_dir)
        (episode_dir / "publish.json").write_text("not json")

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="current publish receipt"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_unknown_scheduled_receipt_recovers_remote_job_id(
        self, env, monkeypatch, episode_dir
    ):
        _seed_episode(episode_dir)
        episode = json.loads((episode_dir / "episode.json").read_text())
        identity = _make_agent(episode_dir)._identity(
            episode["publish_approval"]["revision"], "short", "clip_0"
        )
        scheduled_date = "2099-01-05T09:00:00-08:00"
        _write_json(
            episode_dir / "publish.json",
            {
                "profile_username": "test_user",
                "shorts": [
                    {
                        "clip_id": "clip_0",
                        "status": "unknown",
                        "scheduled": True,
                        "scheduled_date": scheduled_date,
                        "request_id": identity,
                        "external_id": identity,
                        "idempotency_key": identity,
                    }
                ],
            },
        )
        monkeypatch.setattr(
            PublishAgent,
            "_remote_schedule",
            lambda *_args: [
                {
                    "job_id": "remote-job",
                    "external_id": identity,
                    "scheduled_date": "2099-01-05T17:00:00Z",
                }
            ],
        )

        with patch("agents.publish.subprocess.run") as run:
            result = _make_agent(episode_dir).execute()

        run.assert_not_called()
        assert result["shorts"][0]["status"] == "submitted"
        assert result["shorts"][0]["job_id"] == "remote-job"

    @pytest.mark.parametrize(
        "response",
        [
            _mock_proc(stdout="not json"),
            _mock_proc(stdout=json.dumps({"unexpected": []})),
            _mock_proc(stdout="upstream error", stderr="HTTP 503", returncode=22),
        ],
    )
    def test_remote_calendar_response_must_be_valid(self, env, episode_dir, response):
        with (
            patch("agents.publish.subprocess.run", return_value=response),
            pytest.raises(RuntimeError, match="Cannot inspect Upload-Post schedule"),
        ):
            REAL_REMOTE_SCHEDULE(_make_agent(episode_dir), "key", "test_user")

    def test_calendar_inspection_failure_blocks_before_upload(
        self, env, monkeypatch, episode_dir
    ):
        _seed_episode(episode_dir)

        def unavailable(*_args):
            raise RuntimeError("Cannot inspect Upload-Post schedule")

        monkeypatch.setattr(PublishAgent, "_remote_schedule", unavailable)
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="Cannot inspect"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_partial_explicit_schedule_does_not_publish_immediately(
        self, env, episode_dir
    ):
        _seed_episode(
            episode_dir,
            schedule=[
                {"clip_id": "another_clip", "day_offset": 1, "time_slot": "morning"}
            ],
        )
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="missing schedule entries"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()

    def test_duplicate_explicit_schedule_blocks_before_upload(self, env, episode_dir):
        entry = {
            "clip_id": "clip_0",
            "scheduled_date": "2099-07-01T09:00:00-07:00",
        }
        _seed_episode(episode_dir, schedule=[entry, entry])

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="duplicate entries"),
        ):
            _make_agent(episode_dir).execute()
        run.assert_not_called()


def _destination_config():
    config = _publish_config()
    config["podcast"] = {
        "channel_handle": "@local-pod",
        "r2": {"public_url": "https://media.example"},
    }
    return config


def _destination_request(
    episode_dir, config, *, request_id, destinations, clip_ids=None
):
    from agents.qa import quality_snapshot

    request = {
        "destinations": destinations,
        "request_id": request_id,
        "actor": "release-operator",
        "reason": "Publish the approved motion cut",
        "expected_release_revision": quality_snapshot(episode_dir, config=config)[
            "release_gate"
        ]["revision"],
    }
    if clip_ids is not None:
        request["clip_ids"] = clip_ids
    return request


def _refresh_copy_approvals(episode_dir, config):
    """Model the exact QA/editorial refresh after reviewed copy changes."""
    clips_path = episode_dir / "clips.json"
    clips_document = json.loads(clips_path.read_text())
    metadata = json.loads((episode_dir / "metadata" / "metadata.json").read_text())
    metadata_by_id = {str(item["id"]): item for item in metadata["clips"]}
    records = read_render_manifest(episode_dir)["shorts"]
    for clip in clips_document["clips"]:
        if clip.get("status") != "approved":
            continue
        record = records[str(clip["id"])]
        clip["approved_render_fingerprint"] = record["fingerprint"]
        clip["approved_revision"] = clip_review_revision(
            clip, record, metadata_by_id.get(str(clip["id"]))
        )
    _write_json(clips_path, clips_document)

    episode_path = episode_dir / "episode.json"
    episode = json.loads(episode_path.read_text())
    qa_path = episode_dir / "qa" / "qa.json"
    qa = json.loads(qa_path.read_text())
    qa["quality_revision"] = quality_revision(episode_dir, episode, config=config)
    _write_json(qa_path, qa)
    episode["publish_approval"] = {
        "revision": release_revision(episode_dir, episode, config=config),
        "approved_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }
    _write_json(episode_path, episode)


class TestShortDestinationRequests:
    REQUEST_A = "b8c0b129-599c-48cb-b363-60a5fe4dc46c"
    REQUEST_B = "ecf17f34-b5a5-4a03-a62d-49fca57b85df"

    @staticmethod
    def _seed(episode_dir, config, *, clip_count=1, extra_metadata=None):
        scheduled = (
            datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=14)
        ).replace(hour=9, minute=0, second=0, microsecond=0)
        clips = []
        for index in range(clip_count):
            clips.append(
                {
                    "id": f"clip_{index}",
                    "title": "A real story",
                    "status": "approved",
                    "start_seconds": index * 30,
                    "end_seconds": (index + 1) * 30,
                    "metadata": {
                        "youtube": {
                            "title": "YT title",
                            "description": "A moment.\nFull episode: link in bio",
                        },
                        "tiktok": {
                            "caption": "A moment #Local\nLink in bio",
                            "hashtags": ["local", "#LoveStory", "Love Story"],
                        },
                        "instagram": {"caption": "IG", "hashtags": ["local"]},
                        "x": {"text": "X copy"},
                        **(extra_metadata or {}),
                    },
                }
            )
        _seed_episode(
            episode_dir,
            config=config,
            clips=clips,
            schedule=[
                {
                    "clip_id": f"clip_{index}",
                    "scheduled_date": (scheduled + timedelta(days=index)).isoformat(),
                }
                for index in range(clip_count)
            ],
        )

    def test_required_variant_override_is_allowed_for_x_preview(
        self, env, episode_dir, monkeypatch
    ):
        config = _destination_config()
        required_variant = "satisfying_motion_v1"
        config["platforms"]["x"]["required_short_variant_id"] = required_variant
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        data = agent._inputs()
        effective = {
            **data["short_versions"]["clip_0"],
            "version": required_variant,
            "variant_id": required_variant,
        }
        monkeypatch.setattr(
            agent,
            "_destination_versions",
            lambda _data, _overrides: {"clip_0": effective},
        )
        request = _destination_request(
            episode_dir,
            config,
            request_id=self.REQUEST_A,
            destinations=["x"],
            clip_ids=["clip_0"],
        )
        request["variant_overrides"] = {"clip_0": required_variant}

        plan = agent._destination_plan(data, request)

        assert plan["targets"][0]["variant_id"] == required_variant
        assert plan["variant_overrides"] == {"clip_0": required_variant}

    @pytest.mark.parametrize("destinations", (["x"], ["youtube", "x"]))
    def test_wrong_required_variant_is_rejected_before_destination_preflight(
        self, env, episode_dir, monkeypatch, destinations
    ):
        config = _destination_config()
        config["platforms"]["x"]["required_short_variant_id"] = "satisfying_motion_v1"
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        data = agent._inputs()
        wrong = {
            **data["short_versions"]["clip_0"],
            "version": "minecraft_parkour_v1",
            "variant_id": "minecraft_parkour_v1",
        }
        monkeypatch.setattr(
            agent,
            "_destination_versions",
            lambda _data, _overrides: {"clip_0": wrong},
        )
        monkeypatch.setattr(
            agent,
            "_occupied_schedule",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("provider preflight must not run")
            ),
        )
        request = _destination_request(
            episode_dir,
            config,
            request_id=self.REQUEST_A,
            destinations=destinations,
            clip_ids=["clip_0"],
        )
        request["variant_overrides"] = {"clip_0": "minecraft_parkour_v1"}

        with pytest.raises(
            RuntimeError, match="Submit a separate x destination request"
        ):
            agent._destination_plan(data, request)

    def test_legacy_history_acknowledgement_requires_explicit_subset(
        self, episode_dir, monkeypatch
    ):
        acknowledgement = {
            "receipt_history_revision": "sha256:legacy-history",
            "obligations": [
                {
                    "receipt_revision": "sha256:legacy-receipt",
                    "artifact_identity": "unknown",
                }
            ],
        }
        snapshot = {
            "release_gate": {"safe": True, "revision": "sha256:release"},
            "approvals": {"editorial": {"revision": "sha256:longform"}},
        }
        data = {
            "episode": {},
            "snapshot": snapshot,
            "gate": snapshot["release_gate"],
            "api_key": "test-key",
            "user": "test-profile",
            "approved": [],
            "metadata": {"longform": {}},
            "platforms": ["youtube", "tiktok", "instagram", "x"],
            "previous": {"shorts": []},
            "previous_shorts": [],
            "funnel_urls": {"youtube": "", "spotify": ""},
            "longform_revision": "sha256:longform",
            "short_metadata": {},
            "short_versions": {
                "clip_0": {
                    "re_release_request": {
                        "unresolved_history_acknowledgement": acknowledgement
                    }
                }
            },
        }
        agent = _make_agent(episode_dir)
        monkeypatch.setattr(agent, "_inputs", lambda **_kwargs: data)
        monkeypatch.setattr(
            "agents.publish.quality_snapshot", lambda *_args, **_kwargs: snapshot
        )

        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="reviewed explicit destination subset"),
        ):
            agent.execute()

        run.assert_not_called()

    def test_acknowledgement_is_copied_into_destination_receipt_fields(self):
        from agents.publish import _rerelease_receipt_fields

        acknowledgement = {
            "receipt_history_revision": "sha256:legacy-history",
            "obligations": [{"receipt_revision": "sha256:legacy-receipt"}],
        }
        assert _rerelease_receipt_fields(
            {
                "request_id": "b8c0b129-599c-48cb-b363-60a5fe4dc46c",
                "actor": "release-operator",
                "reason": "Publish approved Motion replacement",
                "revision": "sha256:authorization",
                "receipt_history_revision": "sha256:legacy-history",
                "unresolved_history_acknowledgement": acknowledgement,
            }
        ) == {
            "rerelease_request_id": "b8c0b129-599c-48cb-b363-60a5fe4dc46c",
            "rerelease_actor": "release-operator",
            "rerelease_reason": "Publish approved Motion replacement",
            "rerelease_authorization_revision": "sha256:authorization",
            "parent_receipt_history_revision": "sha256:legacy-history",
            "unresolved_history_acknowledgement": acknowledgement,
        }

    def test_preview_execute_persists_intent_before_send_and_normalizes_copy(
        self, env, episode_dir
    ):
        config = _destination_config()
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        preview = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube", "tiktok"],
            )
        )

        assert preview["requested_destinations"] == ["tiktok", "youtube"]
        assert preview["deferred_destinations"] == ["instagram", "x"]
        assert preview["selected_clip_ids"] == ["clip_0"]
        copy = preview["targets"][0]["destination_copy"]
        assert "link in bio" not in json.dumps(copy).lower()
        assert "https://media.example/links/episodes/ep_test.html" in json.dumps(copy)
        assert copy["tiktok"]["text"].count("#Local") == 1
        assert copy["tiktok"]["text"].count("#LoveStory") == 1
        assert "instagram" not in copy and "x" not in copy

        def submitted(command, **_kwargs):
            stored = json.loads((episode_dir / "publish.json").read_text())
            assert stored["shorts"][0]["status"] == "intent_recorded"
            assert [
                value.removeprefix("platform[]=")
                for value in command
                if value.startswith("platform[]=")
            ] == ["tiktok", "youtube"]
            assert "selfDeclaredMadeForKids=false" in command
            return _mock_proc(stdout=json.dumps({"request_id": "provider-job"}))

        agent.short_destination_request = preview["execute"]
        with patch("agents.publish.subprocess.run", side_effect=submitted) as run:
            result = agent.run()
        assert run.call_count == 1
        receipt = result["shorts"][0]
        assert receipt["status"] == "submitted"
        assert receipt["destination_request_id"] == self.REQUEST_A
        assert receipt["deferred_platforms"] == ["instagram", "x"]

        with patch("agents.publish.subprocess.run") as repeated:
            again = agent.run()
        repeated.assert_not_called()
        assert again["shorts"][0]["reused_receipt"] is True

    def test_disjoint_wave_can_share_date_but_overlap_and_default_are_blocked(
        self, env, episode_dir
    ):
        config = _destination_config()
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        first = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube", "tiktok"],
            )
        )
        agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "yt-job"}))
            agent.run()

        second_request = _destination_request(
            episode_dir,
            config,
            request_id=self.REQUEST_B,
            destinations=["instagram"],
        )
        second = agent.preview_short_destinations(second_request)
        assert second["deferred_destinations"] == ["x"]
        assert second["deferred_destinations_by_clip"] == {"clip_0": ["x"]}
        assert (
            second["targets"][0]["scheduled_date"]
            == first["targets"][0]["scheduled_date"]
        )
        agent.short_destination_request = second["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "tt-job"}))
            result = agent.run()
        assert run.call_count == 1
        assert {tuple(receipt["platforms"]) for receipt in result["shorts"]} == {
            ("instagram",),
            ("tiktok", "youtube"),
        }

        overlap = {**second_request, "destinations": ["youtube"]}
        with pytest.raises(RuntimeError, match="already has a destination request"):
            agent.preview_short_destinations(overlap)

        from agents.publish import short_receipt_history_revision
        from agents.qa import quality_snapshot
        from lib.short_variants import distribution_release_revision

        publish = json.loads((episode_dir / "publish.json").read_text())
        clips_path = episode_dir / "clips.json"
        clips = json.loads(clips_path.read_text())
        clip = clips["clips"][0]
        version = quality_snapshot(episode_dir, config=config)["release_gate"][
            "short_versions"
        ]["clip_0"]
        rerelease = {
            "request_id": "811e01ca-6c83-4b52-9d88-3341c56560ec",
            "actor": "release-operator",
            "reason": "Release a new exact destination wave",
            "variant_id": version["variant_id"],
            "target_revision": version["revision"],
            "render_fingerprint": version["render_fingerprint"],
            "receipt_history_revision": short_receipt_history_revision(
                publish, "clip_0"
            ),
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        rerelease["revision"] = distribution_release_revision(
            request_id=rerelease["request_id"],
            actor=rerelease["actor"],
            reason=rerelease["reason"],
            variant_id=rerelease["variant_id"],
            target_revision=rerelease["target_revision"],
            render_fingerprint=rerelease["render_fingerprint"],
            receipt_history_revision=rerelease["receipt_history_revision"],
        )
        clip["distribution_release"] = rerelease
        _write_json(clips_path, clips)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode["publish_approval"] = {
            "revision": quality_snapshot(episode_dir, config=config)["release_gate"][
                "revision"
            ],
            "approved_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        _write_json(episode_path, episode)

        del agent.short_destination_request
        with (
            patch("agents.publish.subprocess.run") as run,
            pytest.raises(RuntimeError, match="explicit destination subset"),
        ):
            agent.execute()
        run.assert_not_called()

    def test_disjoint_sibling_waves_share_one_global_capacity_slot(
        self, env, monkeypatch, episode_dir
    ):
        config = _destination_config()
        sibling = episode_dir.parent / "ep_sibling"
        sibling.mkdir()
        (sibling / "shorts").mkdir()
        self._seed(sibling, config)
        sibling_agent = _make_agent(sibling, config)

        first = sibling_agent.preview_short_destinations(
            _destination_request(
                sibling,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube", "tiktok"],
            )
        )
        sibling_agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"job_id": "yt-job"}))
            sibling_agent.run()

        second = sibling_agent.preview_short_destinations(
            _destination_request(
                sibling,
                config,
                request_id=self.REQUEST_B,
                destinations=["x"],
            )
        )
        sibling_agent.short_destination_request = second["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"job_id": "x-job"}))
            sibling_agent.run()

        receipts = [
            receipt
            for receipt in json.loads((sibling / "publish.json").read_text())["shorts"]
            if receipt.get("destination_request_id")
        ]
        from agents.publish import _schedule_content_identity

        assert (
            _schedule_content_identity(
                {**receipts[0], "status": "unknown"}, "test_user", sibling.name
            )
            is None
        )
        assert (
            _schedule_content_identity(receipts[0], "test_user", "wrong_episode")
            is None
        )
        remote = [
            {
                "job_id": receipt["job_id"],
                "external_id": receipt["external_id"],
                "scheduled_date": receipt["scheduled_date"],
                "profile_username": "test_user",
                "platforms": receipt["platforms"],
                "source_filename": "clip_0.mp4",
                "fields": {"external_id": receipt["external_id"]},
            }
            for receipt in receipts
        ]
        current_agent = _make_agent(episode_dir, config)
        monkeypatch.setattr(current_agent, "_remote_schedule", lambda *_args: remote)

        occupied = current_agent._occupied_schedule("key", "test_user")
        assert len(occupied) == 2  # retain both jobs for exact recovery
        scheduled = datetime.fromisoformat(receipts[0]["scheduled_date"])
        with pytest.raises(RuntimeError, match="Schedule collision"):
            current_agent._reserve(
                list(occupied),
                scheduled,
                "another-exact-artifact",
                "clip_other",
                "America/Los_Angeles",
                2,
                2,
            )
        current_agent._reserve(
            occupied,
            scheduled.replace(hour=18),
            "another-exact-artifact",
            "clip_other",
            "America/Los_Angeles",
            2,
            2,
        )

        conflicting = json.loads(json.dumps(remote))
        conflicting[1]["source_filename"] = "different-clip.mp4"
        monkeypatch.setattr(
            current_agent, "_remote_schedule", lambda *_args: conflicting
        )
        with pytest.raises(RuntimeError, match="Schedule collision"):
            current_agent._reserve(
                current_agent._occupied_schedule("key", "test_user"),
                scheduled.replace(hour=18),
                "another-exact-artifact",
                "clip_other",
                "America/Los_Angeles",
                2,
                2,
            )

    def test_overlapping_destination_jobs_remain_distinct_capacity_slots(self):
        from agents.publish import _schedule_capacity_count

        scheduled = datetime.fromisoformat("2099-01-05T09:00:00-08:00")
        records = [
            {
                "scheduled_at": scheduled,
                "_schedule_content_identity": "sha256:exact-motion-artifact",
                "_schedule_platforms": platforms,
            }
            for platforms in (["tiktok", "youtube"], ["x"], ["x"])
        ]

        assert _schedule_capacity_count(records[:2]) == 1
        assert _schedule_capacity_count(records) == 2

    def test_request_uuid_cannot_change_destinations_or_selected_clips(
        self, env, episode_dir
    ):
        config = _destination_config()
        self._seed(episode_dir, config, clip_count=2)
        agent = _make_agent(episode_dir, config)
        first = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube"],
                clip_ids=["clip_0"],
            )
        )
        agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "yt-job"}))
            agent.run()

        for changed in (
            {"destinations": ["tiktok"], "clip_ids": ["clip_0"]},
            {"destinations": ["youtube"], "clip_ids": None},
        ):
            with pytest.raises(RuntimeError, match="request_id cannot be reused"):
                agent.preview_short_destinations(
                    _destination_request(
                        episode_dir,
                        config,
                        request_id=self.REQUEST_A,
                        **changed,
                    )
                )

    def test_deferred_wave_keeps_existing_future_slot(self, env, episode_dir):
        config = _destination_config()
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        first = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube"],
            )
        )
        agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "yt-job"}))
            agent.run()

        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        scheduled = datetime.fromisoformat(first["targets"][0]["scheduled_date"])
        episode["publish_schedule"][0]["scheduled_date"] = (
            scheduled + timedelta(days=1)
        ).isoformat()
        qa_path = episode_dir / "qa" / "qa.json"
        qa = json.loads(qa_path.read_text())
        qa["quality_revision"] = quality_revision(episode_dir, episode, config=config)
        _write_json(qa_path, qa)
        episode["publish_approval"] = {
            "revision": release_revision(episode_dir, episode, config=config),
            "approved_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        _write_json(episode_path, episode)

        with pytest.raises(RuntimeError, match="existing future schedule"):
            agent.preview_short_destinations(
                _destination_request(
                    episode_dir,
                    config,
                    request_id=self.REQUEST_B,
                    destinations=["tiktok"],
                )
            )

    def test_deferred_wave_can_use_fresh_slot_after_original_passes(
        self, env, monkeypatch, episode_dir
    ):
        config = _destination_config()
        self._seed(episode_dir, config)
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        original = datetime.fromisoformat(
            episode["publish_schedule"][0]["scheduled_date"]
        )
        monkeypatch.setattr(
            PublishAgent,
            "_schedule_reference",
            staticmethod(lambda *_args: original - timedelta(days=1)),
        )
        agent = _make_agent(episode_dir, config)
        first = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube"],
            )
        )
        agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "yt-job"}))
            agent.run()

        episode = json.loads(episode_path.read_text())
        episode["publish_schedule"][0]["scheduled_date"] = (
            original + timedelta(days=2)
        ).isoformat()
        qa_path = episode_dir / "qa" / "qa.json"
        qa = json.loads(qa_path.read_text())
        qa["quality_revision"] = quality_revision(episode_dir, episode, config=config)
        _write_json(qa_path, qa)
        episode["publish_approval"] = {
            "revision": release_revision(episode_dir, episode, config=config),
            "approved_at": (original + timedelta(days=1)).isoformat(),
        }
        _write_json(episode_path, episode)
        monkeypatch.setattr(
            PublishAgent,
            "_schedule_reference",
            staticmethod(lambda *_args: original + timedelta(days=1)),
        )

        second = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_B,
                destinations=["tiktok"],
            )
        )
        assert datetime.fromisoformat(second["targets"][0]["scheduled_date"]) == (
            original + timedelta(days=2)
        )

    @pytest.mark.parametrize(
        ("changed_field", "changed_value"),
        [
            (None, None),
            ("platforms", ["youtube", "instagram"]),
            ("profile_username", "wrong-profile"),
            ("source_filename", "wrong.mp4"),
        ],
    )
    def test_crash_recovery_rejects_conflicting_provider_row(
        self, env, monkeypatch, episode_dir, changed_field, changed_value
    ):
        config = _destination_config()
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        preview = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube"],
            )
        )
        agent.short_destination_request = preview["execute"]
        with (
            patch.object(agent, "_submit_short", side_effect=RuntimeError("crash")),
            pytest.raises(RuntimeError, match="crash"),
        ):
            agent.run()
        target = preview["targets"][0]
        remote = {
            "job_id": "remote-job",
            "external_id": target["external_id"],
            "profile_username": "test_user",
            "platforms": ["youtube"],
            "source_filename": "clip_0.mp4",
            "scheduled_date": target["scheduled_date"],
            "fields": {"external_id": target["external_id"]},
        }
        if changed_field:
            remote[changed_field] = changed_value
        monkeypatch.setattr(PublishAgent, "_remote_schedule", lambda *_args: [remote])

        with patch("agents.publish.subprocess.run") as run:
            if changed_field:
                with pytest.raises(RuntimeError, match="provider schedule conflicts"):
                    agent.run()
            else:
                result = agent.run()
                assert result["shorts"][0]["job_id"] == "remote-job"
        run.assert_not_called()

    def test_changed_release_revision_cannot_bypass_stable_target_overlap(
        self, env, episode_dir
    ):
        config = _destination_config()
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        first = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube"],
            )
        )
        agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "yt-job"}))
            agent.run()

        changed = _destination_config()
        changed["podcast"]["channel_handle"] = "@changed"
        episode_path = episode_dir / "episode.json"
        episode = json.loads(episode_path.read_text())
        episode["publish_approval"] = {
            "revision": release_revision(episode_dir, episode, config=changed),
            "approved_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }
        _write_json(episode_path, episode)
        changed_agent = _make_agent(episode_dir, changed)
        with pytest.raises(RuntimeError, match="already has a destination request"):
            changed_agent.preview_short_destinations(
                _destination_request(
                    episode_dir,
                    changed,
                    request_id=self.REQUEST_B,
                    destinations=["youtube"],
                )
            )

    def test_intent_survives_failure_and_malformed_intent_fails_closed(
        self, env, episode_dir
    ):
        config = _destination_config()
        self._seed(episode_dir, config)
        agent = _make_agent(episode_dir, config)
        preview = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=self.REQUEST_A,
                destinations=["youtube"],
            )
        )
        agent.short_destination_request = preview["execute"]
        with (
            patch.object(agent, "_submit_short", side_effect=RuntimeError("crash")),
            pytest.raises(RuntimeError, match="crash"),
        ):
            agent.run()
        stored_path = episode_dir / "publish.json"
        stored = json.loads(stored_path.read_text())
        assert stored["shorts"][0]["status"] == "intent_recorded"

        stored["shorts"][0]["destination_copy"]["youtube"]["title"] = "tampered"
        _write_json(stored_path, stored)
        with pytest.raises(RuntimeError, match="prior short publication receipts"):
            agent.execute()


class TestExpandedShortDestinations:
    def test_copy_only_continuation_preserves_real_rerelease_lineage(self):
        from agents.publish import (
            SHORT_COPY_SCHEMA,
            SHORT_DESTINATION_SCHEMA,
            _destination_external_id,
            _destination_request_fields,
            _document_revision,
            _short_target_fields,
            short_receipt_history_revision,
            unresolved_receipt_obligations,
            valid_rerelease_copy_continuation,
        )
        from lib.short_variants import distribution_release_revision

        legacy = {
            "clip_id": "clip_0",
            "status": "failed",
            "error": "Legacy transport response was not JSON",
        }
        before = {"shorts": [legacy]}
        history_revision = short_receipt_history_revision(before, "clip_0")
        obligations, allowed = unresolved_receipt_obligations(before, "clip_0")
        assert allowed and obligations
        acknowledgement = {
            "receipt_history_revision": history_revision,
            "obligations": obligations,
        }
        authorization = {
            "request_id": "811e01ca-6c83-4b52-9d88-3341c56560ec",
            "actor": "release-operator",
            "reason": "Release the approved Motion replacement",
            "variant_id": "background_motion_v1",
            "target_revision": "sha256:original-copy-approval",
            "render_fingerprint": "sha256:motion-pixels",
            "receipt_history_revision": history_revision,
            "unresolved_history_acknowledgement": acknowledgement,
            "created_at": "2026-09-14T00:00:00+00:00",
        }
        authorization["revision"] = distribution_release_revision(
            request_id=authorization["request_id"],
            actor=authorization["actor"],
            reason=authorization["reason"],
            variant_id=authorization["variant_id"],
            target_revision=authorization["target_revision"],
            render_fingerprint=authorization["render_fingerprint"],
            receipt_history_revision=history_revision,
            unresolved_history_acknowledgement=acknowledgement,
        )
        clip = {"id": "clip_0", "distribution_release": authorization}
        version = {
            "version": "background_motion_v1",
            "variant_id": "background_motion_v1",
            "render_fingerprint": "sha256:motion-pixels",
            "revision": "sha256:fresh-copy-approval",
            "re_release_request": authorization,
        }
        original_version = {
            **version,
            "revision": authorization["target_revision"],
        }
        target_revision = _document_revision(
            _short_target_fields("clip_0", original_version)
        )
        destinations = ["youtube"]
        destination_request_id = "ecf17f34-b5a5-4a03-a62d-49fca57b85df"
        external_id = _destination_external_id(
            "ep_test",
            destination_request_id,
            destinations,
            target_revision,
            "clip_0",
        )
        copy = {"youtube": {"title": "Title", "description": "Body"}}
        wave = {
            "clip_id": "clip_0",
            "status": "submitted",
            "platforms": destinations,
            "request_id": external_id,
            "external_id": external_id,
            "idempotency_key": external_id,
            "scheduled": True,
            "scheduled_date": "2026-10-01T09:00:00-07:00",
            "timezone": "America/Los_Angeles",
            "version": version["version"],
            "variant_id": version["variant_id"],
            "render_fingerprint": version["render_fingerprint"],
            "approval_revision": authorization["target_revision"],
            "destination_schema": SHORT_DESTINATION_SCHEMA,
            "destination_episode_id": "ep_test",
            "destination_profile_username": "test_user",
            "destination_request_id": destination_request_id,
            "destination_actor": "release-operator",
            "destination_reason": "Publish original destination wave",
            "deferred_platforms": ["tiktok"],
            "target_revision": target_revision,
            "destination_copy": copy,
            "copy_schema": SHORT_COPY_SCHEMA,
            "copy_revision": _document_revision(copy),
            "rerelease_request_id": authorization["request_id"],
            "rerelease_actor": authorization["actor"],
            "rerelease_reason": authorization["reason"],
            "rerelease_authorization_revision": authorization["revision"],
            "parent_receipt_history_revision": history_revision,
            "unresolved_history_acknowledgement": acknowledgement,
        }
        wave["destination_request_revision"] = _document_revision(
            _destination_request_fields(wave)
        )
        publish = {"shorts": [legacy, wave]}
        assert valid_rerelease_copy_continuation(publish, clip, version, [wave])

        later_unknown = {
            "clip_id": "clip_0",
            "status": "unknown",
            "error": "New unresolved provider outcome",
        }
        assert not valid_rerelease_copy_continuation(
            {"shorts": [legacy, later_unknown, wave]}, clip, version, [wave]
        )

    def test_copy_only_reapproval_allows_disjoint_expansion_wave(
        self, env, episode_dir, monkeypatch
    ):
        original_config = _destination_config()
        TestShortDestinationRequests._seed(episode_dir, original_config)
        original_agent = _make_agent(episode_dir, original_config)
        first = original_agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                original_config,
                request_id=TestShortDestinationRequests.REQUEST_A,
                destinations=["youtube", "tiktok"],
            )
        )
        original_agent.short_destination_request = first["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "old-job"}))
            original_agent.run()
        original_receipt = json.loads((episode_dir / "publish.json").read_text())[
            "shorts"
        ][0]
        config = _destination_config()
        config["podcast"]["links"] = {
            "episode_url_template": "https://thelocalpod.link/#{episode_id}"
        }
        config["platforms"]["facebook"] = {
            "enabled": True,
            "account_username": "facebook-account-id",
            "page_id": "facebook-page-id",
        }
        clips_path = episode_dir / "clips.json"
        clips = json.loads(clips_path.read_text())
        clips["clips"][0]["metadata"]["facebook"] = {
            "title": "A reviewed Reel title",
            "description": "A reviewed Reel description",
        }
        _write_json(clips_path, clips)
        _refresh_copy_approvals(episode_dir, config)
        episode_before = (episode_dir / "episode.json").read_bytes()

        agent = _make_agent(episode_dir, config)
        binding_checks = []
        monkeypatch.setattr(
            agent,
            "_verify_destination_bindings",
            lambda *_args: binding_checks.append(True),
        )
        monkeypatch.setattr(
            "agents.publish.validate_destination_media", lambda *_args: []
        )
        second = agent.preview_short_destinations(
            _destination_request(
                episode_dir,
                config,
                request_id=TestShortDestinationRequests.REQUEST_B,
                destinations=["facebook"],
            )
        )
        assert second["schema"] == "cascade.short-destination/v2"
        assert second["targets"][0]["destination_bindings"] == {
            "facebook": {
                "account_id": "facebook-account-id",
                "target_kind": "page",
                "target_id": "facebook-page-id",
            }
        }
        assert (
            second["targets"][0]["scheduled_date"]
            == first["targets"][0]["scheduled_date"]
        )
        copy = second["targets"][0]["destination_copy"]["facebook"]
        assert copy["description"].endswith(
            "Full episode: https://thelocalpod.link/#ep_test"
        )

        agent.short_destination_request = second["execute"]
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"request_id": "new-job"}))
            result = agent.run()
        assert run.call_count == 1
        command = run.call_args.args[0]
        assert "platform[]=facebook" in command
        assert not any("platform[]=youtube" == item for item in command)
        assert "facebook_page_id=facebook-page-id" in command
        assert "facebook_media_type=REELS" in command
        assert len(binding_checks) == 2
        stored = json.loads((episode_dir / "publish.json").read_text())["shorts"]
        assert {**original_receipt, "historical_receipt": True} in stored
        assert (episode_dir / "episode.json").read_bytes() == episode_before
        assert {tuple(item["platforms"]) for item in result["shorts"]} == {
            ("facebook",),
            ("tiktok", "youtube"),
        }
        tampered = {"shorts": json.loads(json.dumps(stored))}
        tampered["shorts"][0]["destination_bindings"]["facebook"]["target_id"] = (
            "different-page"
        )
        from agents.publish import validated_short_receipts

        with pytest.raises(ValueError, match="cannot be verified"):
            validated_short_receipts(tampered)

    def test_provider_binding_preflight_checks_accounts_targets_and_pins(
        self, episode_dir
    ):
        agent = _make_agent(episode_dir)
        bindings = {
            "facebook": {
                "account_id": "fb-account",
                "target_kind": "page",
                "target_id": "fb-page",
            },
            "threads": {
                "account_id": "threads-account",
                "target_kind": "account",
                "target_id": "threads-account",
            },
            "bluesky": {
                "account_id": "bluesky-account",
                "target_kind": "account",
                "target_id": "bluesky-account",
            },
            "linkedin": {
                "account_id": "li-account",
                "target_kind": "page",
                "target_id": "urn:li:organization:123",
            },
            "pinterest": {
                "account_id": "pin-account",
                "target_kind": "board",
                "target_id": "pin-board",
            },
        }
        profile = {
            "success": True,
            "profile": {
                "username": "test_user",
                "social_accounts": {
                    platform: {
                        "username": binding["account_id"],
                        "reauth_required": False,
                    }
                    for platform, binding in bindings.items()
                },
            },
        }
        facebook_pages = {
            "success": True,
            "pages": [{"id": "fb-page", "name": "Podcast page"}],
        }
        facebook_pin = {**facebook_pages, "selected_page_id": "fb-page"}
        linkedin_pages = {
            "success": True,
            "pages": [{"id": "urn:li:organization:123", "name": "Podcast company"}],
        }
        linkedin_pin = {**linkedin_pages, "selected_page_id": None}
        pinterest = {
            "success": True,
            "boards": [{"id": "pin-board", "name": "Podcast board"}],
            "pinterest_account_used": "public-pin-handle",
        }
        with patch.object(
            agent,
            "_provider_json",
            side_effect=[
                profile,
                facebook_pages,
                facebook_pin,
                linkedin_pages,
                linkedin_pin,
                pinterest,
            ],
        ) as provider:
            agent._verify_destination_bindings("secret", "test_user", bindings)
        assert provider.call_count == 6

        duplicate = {
            "success": True,
            "pages": [
                {"id": "fb-page", "name": "One"},
                {"id": "fb-page", "name": "Duplicate"},
            ],
        }
        with (
            patch.object(
                agent,
                "_provider_json",
                side_effect=[profile, duplicate],
            ),
            pytest.raises(RuntimeError, match="ambiguous"),
        ):
            agent._verify_destination_bindings(
                "secret", "test_user", {"facebook": bindings["facebook"]}
            )

    @pytest.mark.parametrize(
        "account",
        [
            None,
            "",
            {},
            [],
            {"username": ""},
            {"username": 123},
            {"username": "fb-account", "reauth_required": "false"},
            {"username": "fb-account", "reauth_required": True},
        ],
    )
    def test_provider_binding_preflight_rejects_unusable_account(
        self, episode_dir, account
    ):
        agent = _make_agent(episode_dir)
        response = {
            "success": True,
            "profile": {
                "username": "test_user",
                "social_accounts": {"facebook": account},
            },
        }
        binding = {
            "facebook": {
                "account_id": "fb-account",
                "target_kind": "page",
                "target_id": "fb-page",
            }
        }
        with (
            patch.object(agent, "_provider_json", return_value=response),
            pytest.raises(RuntimeError, match="unavailable or stale"),
        ):
            agent._verify_destination_bindings("secret", "test_user", binding)

    @pytest.mark.parametrize(
        ("platform", "binding", "responses", "message"),
        [
            (
                "facebook",
                {"account_id": "account", "target_kind": "page", "target_id": "a"},
                [
                    {"success": True, "pages": [{"id": "a", "name": "A"}]},
                    {
                        "success": True,
                        "pages": [{"id": "a", "name": "A"}],
                        "selected_page_id": "b",
                    },
                ],
                "Page target cannot be verified",
            ),
            (
                "linkedin",
                {
                    "account_id": "account",
                    "target_kind": "personal",
                    "target_id": "account",
                },
                [
                    {"success": True, "pages": [{"id": "a", "name": "A"}]},
                    {
                        "success": True,
                        "pages": [{"id": "a", "name": "A"}],
                        "selected_page_id": "a",
                    },
                ],
                "pinned to a Page",
            ),
            (
                "pinterest",
                {"account_id": "account", "target_kind": "board", "target_id": "a"},
                [
                    {
                        "success": True,
                        "boards": [{"id": "b", "name": "B"}],
                        "pinterest_account_used": "public-handle",
                    }
                ],
                "board target cannot be verified",
            ),
        ],
    )
    def test_provider_binding_preflight_rejects_target_or_pin_mismatch(
        self, episode_dir, platform, binding, responses, message
    ):
        agent = _make_agent(episode_dir)
        profile = {
            "success": True,
            "profile": {
                "username": "test_user",
                "social_accounts": {
                    platform: {
                        "username": binding["account_id"],
                        "reauth_required": False,
                    }
                },
            },
        }
        with (
            patch.object(agent, "_provider_json", side_effect=[profile, *responses]),
            pytest.raises(RuntimeError, match=message),
        ):
            agent._verify_destination_bindings(
                "secret", "test_user", {platform: binding}
            )

    @pytest.mark.parametrize("platform", ["facebook", "linkedin"])
    def test_provider_binding_preflight_rejects_missing_pin_identity(
        self, episode_dir, platform
    ):
        agent = _make_agent(episode_dir)
        target_kind = "page" if platform == "facebook" else "personal"
        target_id = "page-id" if platform == "facebook" else "account"
        profile = {
            "success": True,
            "profile": {
                "username": "test_user",
                "social_accounts": {
                    platform: {
                        "username": "account",
                        "reauth_required": False,
                    }
                },
            },
        }
        page_inventory = {
            "success": True,
            "pages": [{"id": "page-id", "name": "Page"}],
        }
        malformed_pin = {
            "success": True,
            "pages": [{"id": "page-id", "name": "Page"}],
        }
        with (
            patch.object(
                agent,
                "_provider_json",
                side_effect=[profile, page_inventory, malformed_pin],
            ),
            pytest.raises(RuntimeError, match="pinned Page is malformed"),
        ):
            agent._verify_destination_bindings(
                "secret",
                "test_user",
                {
                    platform: {
                        "account_id": "account",
                        "target_kind": target_kind,
                        "target_id": target_id,
                    }
                },
            )

    def test_provider_timeout_never_exposes_api_key(self):
        secret = "DO-NOT-LEAK-THIS-KEY"
        with (
            patch(
                "agents.publish.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["curl", f"Authorization: Apikey {secret}"], 35
                ),
            ),
            pytest.raises(RuntimeError) as raised,
        ):
            PublishAgent._provider_json(secret, "https://provider.invalid")
        assert secret not in str(raised.value)
        assert raised.value.__cause__ is None

    def test_all_expansion_fields_and_targets_reach_upload_post(self, episode_dir):
        agent = _make_agent(episode_dir)
        path = episode_dir / "shorts" / "clip_0.mp4"
        path.write_bytes(b"video")
        copy = {
            "facebook": {"title": "FB title", "description": "FB body"},
            "threads": {"text": "Threads post"},
            "bluesky": {"text": "Bluesky post"},
            "linkedin": {"title": "LI title", "description": "LI body"},
            "pinterest": {
                "title": "Pin title",
                "description": "Pin body",
                "link": "https://media.example/episode",
            },
        }
        bindings = {
            "facebook": {
                "account_id": "fb",
                "target_kind": "page",
                "target_id": "fb-page",
            },
            "threads": {
                "account_id": "th",
                "target_kind": "account",
                "target_id": "th",
            },
            "bluesky": {
                "account_id": "bs",
                "target_kind": "account",
                "target_id": "bs",
            },
            "linkedin": {
                "account_id": "li",
                "target_kind": "page",
                "target_id": "li-page",
            },
            "pinterest": {
                "account_id": "pin",
                "target_kind": "board",
                "target_id": "pin-board",
            },
        }
        target = {"destination_copy": copy, "destination_bindings": bindings}
        platforms = sorted(copy)
        with patch.object(
            agent, "_submit", return_value={"status": "submitted"}
        ) as submit:
            agent._submit_short(
                {"id": "clip_0", "title": "Clip"},
                {},
                platforms,
                None,
                "https://youtube.example/full",
                "https://spotify.example/full",
                {
                    "path": "shorts/clip_0.mp4",
                    "version": "base",
                    "variant_id": None,
                    "render_fingerprint": "sha256:render",
                    "revision": "sha256:approval",
                },
                "cascade-short-id",
                "secret",
                "test_user",
                destination_target=target,
            )
        command = submit.call_args.args[0]
        for field in (
            "facebook_title=FB title",
            "facebook_description=FB body",
            "facebook_media_type=REELS",
            "facebook_page_id=fb-page",
            "threads_title=Threads post",
            "bluesky_title=Bluesky post",
            "linkedin_title=LI title",
            "linkedin_description=LI body",
            "target_linkedin_page_id=li-page",
            "pinterest_title=Pin title",
            "pinterest_description=Pin body",
            "pinterest_link=https://media.example/episode",
            "pinterest_board_id=pin-board",
        ):
            assert field in command

    @pytest.mark.parametrize(
        ("platforms", "copy", "expected_title", "specific_field"),
        (
            (
                ["instagram"],
                {"instagram": {"text": "Full Instagram copy"}},
                "Full Instagram copy",
                "instagram_title=Full Instagram copy",
            ),
            (
                ["youtube"],
                {"youtube": {"title": "YT", "description": "Body"}},
                "Generic clip title",
                "youtube_title=YT",
            ),
            (
                ["x"],
                {"x": {"text": "X copy"}},
                "Generic clip title",
                "x_title=X copy",
            ),
            (
                ["instagram", "youtube"],
                {
                    "instagram": {"text": "Full Instagram copy"},
                    "youtube": {"title": "YT", "description": "Body"},
                },
                "Generic clip title",
                "instagram_title=Full Instagram copy",
            ),
        ),
    )
    def test_only_single_instagram_replaces_generic_fallback_title(
        self, episode_dir, platforms, copy, expected_title, specific_field
    ):
        agent = _make_agent(episode_dir)
        (episode_dir / "shorts" / "clip_0.mp4").write_bytes(b"video")
        with patch.object(
            agent, "_submit", return_value={"status": "submitted"}
        ) as submit:
            agent._submit_short(
                {"id": "clip_0", "title": "Generic clip title"},
                {},
                platforms,
                None,
                "https://youtube.example/full",
                "https://spotify.example/full",
                {
                    "path": "shorts/clip_0.mp4",
                    "version": "base",
                    "variant_id": None,
                    "render_fingerprint": "sha256:render",
                    "revision": "sha256:approval",
                },
                "cascade-short-id",
                "secret",
                "test_user",
                destination_target={"destination_copy": copy},
            )

        fields = _multipart_values(submit.call_args.args[0])
        assert f"title={expected_title}" in fields
        assert specific_field in fields

    def test_plaintext_multipart_fields_never_use_curl_file_syntax(self, episode_dir):
        agent = _make_agent(episode_dir)
        path = episode_dir / "shorts" / "clip_0.mp4"
        path.write_bytes(b"video")
        target = {
            "destination_copy": {"threads": {"text": "</private/not-a-file"}},
            "destination_bindings": {
                "threads": {
                    "account_id": "threads-account",
                    "target_kind": "account",
                    "target_id": "threads-account",
                }
            },
        }
        with patch.object(
            agent, "_submit", return_value={"status": "submitted"}
        ) as submit:
            agent._submit_short(
                {"id": "clip_0", "title": "@/private/not-a-file"},
                {},
                ["threads"],
                None,
                "https://youtube.example/full",
                "https://spotify.example/full",
                {
                    "path": "shorts/clip_0.mp4",
                    "version": "base",
                    "variant_id": None,
                    "render_fingerprint": "sha256:render",
                    "revision": "sha256:approval",
                },
                "cascade-short-id",
                "secret",
                "test_user",
                destination_target=target,
            )

        command = submit.call_args.args[0]
        file_forms = [
            value
            for index, value in enumerate(command)
            if index > 0 and command[index - 1] == "-F"
        ]
        text_forms = [
            value
            for index, value in enumerate(command)
            if index > 0 and command[index - 1] == "--form-string"
        ]
        assert file_forms == [f"video=@{path}"]
        assert "title=@/private/not-a-file" in text_forms
        assert "threads_title=</private/not-a-file" in text_forms


def _scoped_longform_gate():
    return {
        "safe": True,
        "revision": "sha256:scoped",
        "source_release_revision": "sha256:aggregate",
        "editorial_revision": "sha256:new-editorial",
        "quality_revision": "sha256:new-quality",
        "publish_plan": {"youtube": {"enabled": True, "destination_configured": True}},
        "blockers": [],
    }


def test_longform_only_agent_never_publishes_shorts_and_keeps_history(episode_dir, env):
    episode = {
        "episode_id": "ep_test",
        "title": "Corrected Arnold episode",
        "description": "Corrected full episode",
        "youtube_longform_url": "https://youtu.be/old-arnold",
        "youtube_longform_url_source": "upload_post_receipt",
        "youtube_longform_url_editorial_revision": "sha256:old-editorial",
        "youtube_longform_url_external_id": publication_identity(
            "ep_test", "sha256:old-editorial", "longform"
        ),
    }
    _write_json(episode_dir / "episode.json", episode)
    (episode_dir / "upload_video.mp4").write_bytes(b"corrected video")
    old_longform = {
        "status": "published",
        "external_id": publication_identity(
            "ep_test", "sha256:old-editorial", "longform"
        ),
        "job_id": "old-job",
        "youtube_longform_url": "https://youtu.be/old-arnold",
    }
    old_shorts = [
        {
            "clip_id": f"clip_{index:02d}",
            "status": "submitted",
            "destination_request_id": f"request-{index}",
        }
        for index in range(30)
    ]
    _write_json(
        episode_dir / "publish.json",
        {"longform": old_longform, "shorts": old_shorts},
    )
    agent = LongformPublishAgent(episode_dir, _publish_config())
    new_receipt = {
        "status": "submitted",
        "external_id": publication_identity(
            "ep_test", "sha256:new-editorial", "longform"
        ),
        "job_id": "new-job",
    }

    with (
        patch(
            "agents.publish.longform_publication_snapshot",
            side_effect=[_scoped_longform_gate(), _scoped_longform_gate()],
        ),
        patch(
            "agents.publish.current_funnel_urls",
            return_value={"youtube": "", "spotify": ""},
        ),
        patch.object(agent, "_publish_longform", return_value=new_receipt) as publish,
        patch.object(agent, "_publish_shorts") as publish_shorts,
    ):
        result = agent.execute()

    publish_shorts.assert_not_called()
    assert publish.call_args.args[2] == old_longform
    assert publish.call_args.args[6] == ""
    assert publish.call_args.kwargs == {"approval_scope": "longform"}
    assert result["shorts"] == old_shorts
    assert result["longform_history"] == [{**old_longform, "historical_receipt": True}]
    assert result["longform"] == new_receipt


def test_corrected_identity_does_not_reuse_old_longform_receipt(episode_dir, env):
    (episode_dir / "upload_video.mp4").write_bytes(b"corrected video")
    agent = LongformPublishAgent(episode_dir, _publish_config())
    old = {
        "status": "unknown",
        "external_id": publication_identity(
            "ep_test", "sha256:old-editorial", "longform"
        ),
    }
    submitted = {
        "status": "submitted",
        "external_id": publication_identity(
            "ep_test", "sha256:new-editorial", "longform"
        ),
    }
    with (
        patch.object(agent, "_verified_longform_transport", return_value=None),
        patch.object(agent, "_submit", return_value=submitted) as submit,
    ):
        result = agent._publish_longform(
            {},
            {"title": "Corrected", "description": "Corrected episode"},
            old,
            "sha256:aggregate",
            "sha256:new-editorial",
            "sha256:new-quality",
            "",
            ["youtube"],
            "key",
            "user",
            approval_scope="longform",
        )

    submit.assert_called_once()
    assert result["external_id"] == submitted["external_id"]


def test_scoped_unknown_receipt_is_reused_without_resubmission(episode_dir, env):
    (episode_dir / "upload_video.mp4").write_bytes(b"corrected video")
    agent = LongformPublishAgent(episode_dir, _publish_config())
    identity = publication_identity("ep_test", "sha256:new-editorial", "longform")
    previous = {"status": "unknown", "external_id": identity, "job_id": "job"}
    with (
        patch.object(agent, "_verified_longform_transport") as transport,
        patch.object(agent, "_submit") as submit,
    ):
        result = agent._publish_longform(
            {},
            {"title": "Corrected", "description": "Corrected episode"},
            previous,
            "sha256:aggregate",
            "sha256:new-editorial",
            "sha256:new-quality",
            "",
            ["youtube"],
            "key",
            "user",
            approval_scope="longform",
        )

    transport.assert_not_called()
    submit.assert_not_called()
    assert result["status"] == "unknown"
    assert result["reused_receipt"] is True
