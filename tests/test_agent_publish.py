"""Tests for the publish agent.

These tests pin the safety-critical behaviors:

1. The agent refuses to run unless ``publish_approved`` is True. This is the
   destructive-action gate — bypassing it sends real videos to live social
   platforms.
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
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.publish import PublishAgent


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test_key")
    monkeypatch.setenv("UPLOAD_POST_USER", "test_user")
    yield


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


def _seed_episode(episode_dir, *, publish_approved=True, clips=None, longform=True):
    """Write the minimum files publish agent needs to run successfully."""
    _write_json(
        episode_dir / "episode.json",
        {
            "episode_id": "ep_test",
            "publish_approved": publish_approved,
            "status": "ready_for_review",
        },
    )
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
    if longform:
        (episode_dir / "longform.mp4").write_bytes(b"fake mp4")


def _make_agent(episode_dir, **platform_overrides):
    """Construct an agent with a minimal config."""
    cfg = {
        "platforms": {
            "youtube": {"enabled": True},
            "tiktok": {"enabled": True},
            "instagram": {"enabled": True},
            "x": {"enabled": True},
        },
        "schedule": {
            "timezone": "America/Los_Angeles",
            "shorts_per_day_weekday": 1,
            "shorts_per_day_weekend": 2,
        },
    }
    for k, v in platform_overrides.items():
        cfg["platforms"][k] = v
    return PublishAgent(episode_dir, cfg)


def _mock_proc(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ── safety gate ─────────────────────────────────────────────────────────────


class TestSafetyGate:
    """The agent must refuse to run unless publish_approved=True. This is the
    only thing standing between a misclick and live social posts."""

    def test_refuses_without_publish_approved(self, env, episode_dir):
        _seed_episode(episode_dir, publish_approved=False)
        agent = _make_agent(episode_dir)
        with pytest.raises(RuntimeError, match="not publish_approved"):
            agent.execute()

    def test_refuses_when_episode_json_missing(self, env, episode_dir):
        # No episode.json at all → load_json_safe returns {} → flag falsy.
        agent = _make_agent(episode_dir)
        with pytest.raises(RuntimeError, match="not publish_approved"):
            agent.execute()

    def test_runs_when_publish_approved(self, env, episode_dir):
        _seed_episode(episode_dir, publish_approved=True)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(
                stdout=json.dumps({"request_id": "req_123"}),
            )
            result = agent.execute()
        assert result["shorts_submitted"] == 1
        assert result["shorts_failed"] == 0


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

    def test_missing_request_id_reported_as_failed(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout=json.dumps({"unexpected": "shape"}))
            result = agent.execute()
        assert result["shorts_failed"] == 1
        assert result["shorts"][0]["status"] == "failed"

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
        assert result["shorts"][0]["status"] == "failed"
        assert result["shorts"][0]["stdout"] == "some body"
        assert "connection refused" in result["shorts"][0]["error"]

    def test_non_json_response_reported_as_failed(self, env, episode_dir):
        _seed_episode(episode_dir)
        agent = _make_agent(episode_dir)
        with patch("agents.publish.subprocess.run") as run:
            run.return_value = _mock_proc(stdout="<html>nginx error</html>")
            result = agent.execute()
        assert result["shorts"][0]["status"] == "failed"
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
        assert result["shorts"][0]["request_id"] == "req_xyz"
        # Full response body retained for debugging / per-platform polling later
        assert result["shorts"][0]["response"]["extra"] == "data"


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
        # First call is the short upload; longform is second
        short_cmd = captured[0]
        # Walk -F flags looking for x_long_text_as_post
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
        assert any("x_long_text_as_post=true" in f for f in flags), (
            "publish.py must send x_long_text_as_post=true defensively, "
            "or X posts > 280 chars silently fail."
        )

    def test_x_long_text_flag_not_sent_when_x_metadata_absent(self, env, episode_dir):
        clips = [
            {
                "id": "clip_0",
                "title": "no x",
                "status": "approved",
                "start_seconds": 0,
                "end_seconds": 30,
                "metadata": {"youtube": {"title": "yt", "description": ""}},
            }
        ]
        _seed_episode(episode_dir, clips=clips)
        agent = _make_agent(episode_dir)
        captured = []
        with patch("agents.publish.subprocess.run") as run:

            def _capture(cmd, **_kwargs):
                captured.append(cmd)
                return _mock_proc(stdout=json.dumps({"request_id": "r"}))

            run.side_effect = _capture
            agent.execute()
        flags = [
            a for i, a in enumerate(captured[0]) if i > 0 and captured[0][i - 1] == "-F"
        ]
        assert not any("x_long_text_as_post" in f for f in flags)


# ── YouTube longform link funnel ────────────────────────────────────────────


class TestYouTubeLongformFunnel:
    """The single highest-CTR funnel from a Short to the longform episode is
    the auto-pinned YouTube first comment with a direct link. Industry data
    puts this at 1-3% CTR vs <0.5% for "link in bio". These tests pin that
    publish.py sends the youtube_first_comment field on every short upload
    when a youtube_longform_url is configured on the episode."""

    def _seed_with_longform_url(
        self, episode_dir, *, youtube_url=None, spotify_url=None, channel_handle=None
    ):
        _seed_episode(episode_dir, publish_approved=True)
        # Inject the longform URLs into episode.json
        ep_path = episode_dir / "episode.json"
        ep = json.loads(ep_path.read_text())
        ep["publish_approved"] = True
        if youtube_url is not None:
            ep["youtube_longform_url"] = youtube_url
        if spotify_url is not None:
            ep["spotify_longform_url"] = spotify_url
        ep_path.write_text(json.dumps(ep))
        return channel_handle

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
        self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc123",
        )
        agent = _make_agent(episode_dir)
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
            "youtube_longform_url is set — this is the highest-CTR funnel."
        )
        assert "youtube.com/watch?v=abc123" in first_comment_flags[0]
        assert "Full episode" in first_comment_flags[0]

    def test_first_comment_includes_spotify_when_set(self, env, episode_dir):
        self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc",
            spotify_url="https://open.spotify.com/episode/xyz",
        )
        agent = _make_agent(episode_dir)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        short_cmd = captured[0]
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
        first_comment = next(f for f in flags if f.startswith("youtube_first_comment="))
        assert "spotify.com/episode/xyz" in first_comment
        assert "Listen on Spotify" in first_comment

    def test_first_comment_includes_channel_handle(self, env, episode_dir):
        self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc",
        )
        agent = _make_agent(episode_dir)
        agent.config["podcast"] = {"channel_handle": "@local-pod"}
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        short_cmd = captured[0]
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
        first_comment = next(f for f in flags if f.startswith("youtube_first_comment="))
        assert "@local-pod" in first_comment

    def test_first_comment_skipped_when_no_youtube_url(self, env, episode_dir):
        # If user hasn't filled in the URL yet, we must NOT send an empty
        # first comment — that would post a useless empty pinned comment.
        _seed_episode(episode_dir, publish_approved=True)
        agent = _make_agent(episode_dir)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        short_cmd = captured[0]
        flags = [
            a for i, a in enumerate(short_cmd) if i > 0 and short_cmd[i - 1] == "-F"
        ]
        first_comment_flags = [
            f for f in flags if f.startswith("youtube_first_comment=")
        ]
        assert len(first_comment_flags) == 0

    def test_longform_idempotent_when_url_already_set(self, env, episode_dir):
        # When youtube_longform_url is already recorded, publish skips the
        # longform re-upload. This is the two-phase flow: longform uploads on
        # the first publish run (URL not yet set), then on the SECOND run
        # (after URL is saved) only shorts upload.
        self._seed_with_longform_url(
            episode_dir,
            youtube_url="https://youtube.com/watch?v=abc",
        )
        agent = _make_agent(episode_dir)
        captured = self._capture_upload_cmds(env, episode_dir, agent)
        # Only 1 call: short upload. Longform upload is skipped by idempotency.
        assert len(captured) == 1
        assert "longform.mp4" not in " ".join(captured[0])


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
                "metadata": {"youtube": {"title": "yt", "description": ""}},
            },
            {
                "id": "clip_1",
                "title": "no",
                "status": "rejected",
                "start_seconds": 30,
                "end_seconds": 60,
                "metadata": {"youtube": {"title": "yt", "description": ""}},
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
        # 1 short + 1 longform = 2 calls total. clip_1 must NOT be uploaded.
        assert len(upload_calls) == 2
        # Make sure clip_1.mp4 doesn't appear in any upload command
        for cmd in upload_calls:
            assert "clip_1.mp4" not in " ".join(cmd)


# ── schedule conversion ─────────────────────────────────────────────────────


class TestScheduleConversion:
    def test_schedule_to_datetime_morning_slot(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        sched = {"day_offset": 1, "time_slot": "morning"}
        dt = agent._schedule_to_datetime(sched, "America/Los_Angeles")
        assert dt.hour == 9
        assert dt.minute == 0

    def test_schedule_to_datetime_evening_slot(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        sched = {"day_offset": 0, "time_slot": "evening"}
        dt = agent._schedule_to_datetime(sched, "America/Los_Angeles")
        assert dt.hour == 18

    def test_generate_schedule_distributes_clips_across_days(self, env, episode_dir):
        agent = _make_agent(episode_dir)
        clips = [{"id": f"clip_{i}"} for i in range(5)]
        sched = agent._generate_schedule(clips, weekday_per_day=1, weekend_per_day=2)
        assert len(sched) == 5
        # No clip should be scheduled with day_offset=0 (always tomorrow earliest)
        assert all(s["day_offset"] >= 1 for s in sched)
        # Every clip_id must appear exactly once
        assert sorted(s["clip_id"] for s in sched) == [f"clip_{i}" for i in range(5)]
