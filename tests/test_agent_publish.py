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

from agents.publish import PublishAgent
from agents.qa import (
    clip_review_revision,
    editorial_revision,
    publication_identity,
    quality_revision,
    release_revision,
)
from lib.delivery_video import (
    longform_render_fingerprint,
    record_longform_render,
    record_short_render,
    short_render_fingerprint,
)
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
        fields = [
            value
            for index, value in enumerate(longform)
            if index > 0 and longform[index - 1] == "-F"
        ]
        assert "youtube_title=Title saved in the episode editor" in fields
        assert "youtube_description=Description saved in the episode editor" in fields


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
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
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
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
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
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
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
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
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
        fields = self._values(commands[0], "-F")
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
        fields = self._values(captured[0], "-F")
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
        fields = [
            value
            for index, value in enumerate(command)
            if index > 0 and command[index - 1] == "-F"
        ]
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
