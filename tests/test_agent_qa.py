"""Tests for the QA agent."""

import json
from unittest.mock import patch

import pytest

from agents.qa import QAAgent, longform_publication_snapshot
from lib.audio_qa import AUDIO_FINDING_REVIEWS_PATH, AUDIO_FINDING_REVIEWS_SCHEMA


@pytest.fixture(autouse=True)
def current_output_continuity(monkeypatch):
    monkeypatch.setattr(
        "agents.qa.analyze_release_audio_continuity",
        lambda *args, **kwargs: {
            "status": "pass",
            "safe": True,
            "artifacts": [],
            "findings": [],
        },
    )
    monkeypatch.setattr(
        "agents.qa.apply_output_finding_reviews",
        lambda report, *_args, **_kwargs: report,
    )


class TestQAAgent:
    def _setup_full_episode(self, episode_dir, sample_clips):
        """Create all files needed for a passing QA run."""
        # source_merged.mp4
        (episode_dir / "source_merged.mp4").write_bytes(b"\x00" * 100)

        # longform.mp4
        (episode_dir / "longform.mp4").write_bytes(b"\x00" * 100)

        # clips.json
        with open(episode_dir / "clips.json", "w") as f:
            json.dump({"clips": sample_clips}, f)

        # shorts
        for clip in sample_clips:
            (episode_dir / "shorts" / "{}.mp4".format(clip["id"])).write_bytes(b"\x00")

        # SRT
        (episode_dir / "subtitles" / "transcript.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nTest\n"
        )

        # Metadata
        metadata = {
            "longform": {"title": "Test", "description": "Test description"},
            "clips": [{"id": c["id"]} for c in sample_clips],
            "schedule": [{"clip_id": "clip_01", "platform": "youtube"}],
        }
        with open(episode_dir / "metadata" / "metadata.json", "w") as f:
            json.dump(metadata, f)

    def test_all_checks_pass(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "3600.0",
                },
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            result = agent.execute()

        assert result["overall"] == "pass"
        assert result["hard_checks_passed"] == result["hard_checks_total"]

    def test_missing_source_merged(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        (tmp_episode_dir / "source_merged.mp4").unlink()

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "3600.0",
                },
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            result = agent.execute()

        assert result["overall"] == "fail"
        failed = [c for c in result["checks"] if not c["pass"]]
        assert any("source_merged" in c["name"] for c in failed)

    def test_missing_shorts_detected(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        for clip in sample_clips:
            clip["selection_status"] = "selected"
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        # Remove one short
        (tmp_episode_dir / "shorts" / "clip_02.mp4").unlink()

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "3600.0",
                },
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            result = agent.execute()

        assert result["overall"] == "fail"
        shorts_check = next(
            c for c in result["checks"] if c["name"] == "all_shorts_rendered"
        )
        assert not shorts_check["pass"]
        assert "clip_02" in shorts_check["detail"]

    def test_duration_warnings(self, tmp_episode_dir, sample_config):
        """Clips outside configured duration range produce warnings."""
        clips = [
            {
                "id": "clip_01",
                "start_seconds": 0,
                "end_seconds": 10,
                "duration": 10.0,
                "status": "pending",
                "selection_status": "selected",
            },
        ]
        self._setup_full_episode(tmp_episode_dir, clips)

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "3600.0",
                },
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            result = agent.execute()

        assert result["warning_count"] > 0

    def test_rejected_clips_do_not_block_selected_render_or_metadata(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        sample_clips[0]["selection_status"] = "selected"
        sample_clips[1].update(selection_status="rejected", status="rejected")
        sample_config["platforms"] = {"youtube": {"enabled": True}}
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        (tmp_episode_dir / "shorts" / "clip_02.mp4").unlink()
        metadata = {
            "longform": {"title": "Test", "description": "Description"},
            "clips": [
                {
                    "id": "clip_01",
                    "youtube": {"title": "Selected", "description": "Copy"},
                }
            ],
        }
        (tmp_episode_dir / "metadata" / "metadata.json").write_text(
            json.dumps(metadata)
        )
        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "duration": "3600.0"},
                {"codec_type": "audio", "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            result = agent.execute()

        by_name = {check["name"]: check for check in result["checks"]}
        assert by_name["all_shorts_rendered"]["pass"] is True
        assert by_name["all_shorts_rendered"]["detail"].startswith("1/1 selected")
        assert by_name["metadata_valid"]["pass"] is True
        assert result["overall"] == "pass"

    def test_recorder_mix_camera_continuity_is_recorded_as_skipped(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "duration": "3600.0"},
                {"codec_type": "audio", "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={
                    "status": "not_applicable",
                    "safe": None,
                    "reason": "Selected recorder mix does not use camera audio",
                },
            ),
        ):
            result = agent.execute()

        assert result["overall"] == "pass"
        assert not any(c["name"] == "audio_continuity" for c in result["checks"])
        assert result["skipped_checks"] == [
            {
                "name": "audio_continuity",
                "status": "not_applicable",
                "pass": None,
                "detail": "Selected recorder mix does not use camera audio",
            }
        ]
        assert result["skipped_check_count"] == 1

    def test_qa_json_saved(self, tmp_episode_dir, sample_config, sample_clips):
        self._setup_full_episode(tmp_episode_dir, sample_clips)

        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "3600.0",
                },
                {"codec_type": "audio", "channels": 2, "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            agent.execute()

        assert (tmp_episode_dir / "qa" / "qa.json").exists()

    def test_qa_rerun_keeps_review_only_for_the_same_output(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        report = {
            "fingerprint": "report-v1",
            "analysis": {"status": "complete"},
            "scope": {"selected_mix_provenance": {}},
            "findings": [
                {
                    "id": "aq_one",
                    "fingerprint": "finding-v1",
                    "severity": "warning",
                    "edited_time": {"status": "retained"},
                    "resolution": {"status": "unresolved"},
                }
            ],
        }
        (tmp_episode_dir / AUDIO_FINDING_REVIEWS_PATH).write_text(
            json.dumps(
                {
                    "schema": AUDIO_FINDING_REVIEWS_SCHEMA,
                    "reviews": {
                        "aq_one": {
                            "decision": "accepted",
                            "source_report_fingerprint": "report-v1",
                            "finding_fingerprint": "finding-v1",
                            "output_revision": "render-v1",
                            "reviewed_by": "Editor",
                            "reviewed_at": "2026-09-11T00:00:00+00:00",
                            "evidence_note": "Reviewed current output.",
                        }
                    },
                }
            )
        )
        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "duration": "3600.0"},
                {"codec_type": "audio", "duration": "3600.0"},
            ],
        }
        agent = QAAgent(tmp_episode_dir, sample_config)

        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value=report),
            patch("agents.qa.review_output_revision", return_value="render-v1"),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            agent.execute()
        saved = json.loads((tmp_episode_dir / "qa" / "audio-quality.json").read_text())
        assert saved["findings"][0]["resolution"]["status"] == "accepted"

        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value=report),
            patch("agents.qa.review_output_revision", return_value="render-v2"),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            agent.execute()
        saved = json.loads((tmp_episode_dir / "qa" / "audio-quality.json").read_text())
        assert saved["findings"][0]["resolution"] == {"status": "unresolved"}

    def test_clip_boundary_words_are_nonblocking_revisioned_warnings(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        sample_clips[0]["selection_status"] = "selected"
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        transcript = {
            "clock": "source",
            "utterances": [
                {
                    "speaker": 0,
                    "words": [
                        {
                            "word": "partial",
                            "start": 59.9,
                            "end": 60.2,
                            "confidence": 0.96,
                        }
                    ],
                }
            ],
        }
        (tmp_episode_dir / "diarized_transcript.json").write_text(
            json.dumps(transcript)
        )
        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "duration": "3600.0"},
                {"codec_type": "audio", "duration": "3600.0"},
            ],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.current_diarized_transcript", return_value=transcript),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
        ):
            result = agent.execute()

        evidence = result["clip_boundary_evidence"]
        assert evidence["current"] is True
        assert evidence["status"] == "review_required"
        assert evidence["transcript_revision"].startswith("sha256:")
        warning = next(
            item
            for item in result["warnings"]
            if item["name"] == "clip_boundary_clip_01"
        )
        assert warning["evidence"]["findings"][0]["word"]["word"] == "partial"
        assert result["overall"] == "pass"

    def test_release_output_continuity_failure_is_a_hard_check(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "duration": "3600.0"},
                {"codec_type": "audio", "duration": "3600.0"},
            ],
        }
        continuity = {
            "status": "failed",
            "safe": False,
            "artifacts": [
                {
                    "role": "upload_video",
                    "required": True,
                    "status": "failed",
                    "detail": "1 speech-overlapping silence span detected.",
                }
            ],
            "findings": [{"id": "oc_dropout"}],
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
            patch(
                "agents.qa.analyze_release_audio_continuity",
                return_value=continuity,
            ),
        ):
            result = agent.execute()

        check = next(
            item
            for item in result["checks"]
            if item["name"] == "selected_master_output_continuity"
        )
        assert check["pass"] is False
        assert check["status"] == "failed"
        assert result["overall"] == "fail"
        assert result["selected_master_output_continuity"] == continuity

    def test_reviewed_output_continuity_controls_result_without_rewriting_evidence(
        self, tmp_episode_dir, sample_config, sample_clips
    ):
        self._setup_full_episode(tmp_episode_dir, sample_clips)
        mock_probe = {
            "format": {"duration": "3600.0"},
            "streams": [
                {"codec_type": "video", "duration": "3600.0"},
                {"codec_type": "audio", "duration": "3600.0"},
            ],
        }
        raw_continuity = {
            "status": "failed",
            "safe": False,
            "detail": "1 semantic finding requires review.",
            "artifacts": [{"role": "selected_audio_master", "status": "failed"}],
            "findings": [{"id": "oc_reviewed"}],
        }
        reviewed_continuity = {
            **raw_continuity,
            "status": "pass",
            "safe": True,
            "detail": "The current output finding was explicitly reviewed.",
        }

        agent = QAAgent(tmp_episode_dir, sample_config)
        with (
            patch("agents.qa.ffprobe", return_value=mock_probe),
            patch("agents.qa.analyze_episode_audio", return_value={"findings": []}),
            patch(
                "agents.qa.audio_release_gate",
                return_value={"status": "pass", "reason": "checked"},
            ),
            patch(
                "agents.qa.analyze_release_audio_continuity",
                return_value=raw_continuity,
            ),
            patch(
                "agents.qa.apply_output_finding_reviews",
                return_value=reviewed_continuity,
            ),
        ):
            result = agent.execute()

        check = next(
            item
            for item in result["checks"]
            if item["name"] == "selected_master_output_continuity"
        )
        assert check == {
            "name": "selected_master_output_continuity",
            "status": "pass",
            "pass": True,
            "detail": "The current output finding was explicitly reviewed.",
        }
        assert result["overall"] == "pass"
        assert result["selected_master_output_continuity"] == raw_continuity
        saved = json.loads((tmp_episode_dir / "qa" / "qa.json").read_text())
        assert saved["selected_master_output_continuity"] == raw_continuity


def test_longform_gate_ignores_only_short_blockers(tmp_episode_dir):
    episode_path = tmp_episode_dir / "episode.json"
    episode = {
        "episode_id": tmp_episode_dir.name,
        "title": "Corrected episode",
        "description": "Corrected description",
    }
    episode_path.write_text(json.dumps(episode))
    (tmp_episode_dir / "upload_video.mp4").write_bytes(b"corrected video")
    aggregate = {
        "quality": {
            "status": "passed",
            "current_revision": "sha256:quality",
            "report_revision": "sha256:quality",
        },
        "release_gate": {
            "safe": False,
            "revision": "sha256:aggregate",
            "blockers": [
                {
                    "code": "approved_shorts_missing",
                    "message": "Ten approved shorts are stale.",
                },
                {
                    "code": "publish_approval_missing_or_stale",
                    "message": "Global approval required.",
                },
            ],
            "publish_plan": {
                "upload_post": {
                    "destinations": ["youtube", "tiktok"],
                    "account_identity": "sha256:user",
                    "youtube": {"self_declared_made_for_kids": False},
                },
                "video_podcast_rss": {"enabled": False, "format": "video"},
            },
        },
        "approvals": {"editorial": {"current": True, "revision": "sha256:editorial"}},
    }

    with patch("agents.qa.quality_snapshot", return_value=aggregate):
        awaiting = longform_publication_snapshot(tmp_episode_dir, config={})
        episode["longform_publish_approval"] = {
            "schema": "cascade.longform-publish-approval/v1",
            "revision": awaiting["revision"],
        }
        episode_path.write_text(json.dumps(episode))
        approved = longform_publication_snapshot(tmp_episode_dir, config={})

    assert awaiting["can_approve"] is True
    assert awaiting["safe"] is False
    assert approved["safe"] is True
    assert aggregate["release_gate"]["safe"] is False


def test_longform_gate_keeps_nonshort_delivery_blockers(tmp_episode_dir):
    (tmp_episode_dir / "episode.json").write_text(
        json.dumps(
            {
                "episode_id": tmp_episode_dir.name,
                "title": "Corrected episode",
                "description": "Corrected description",
            }
        )
    )
    aggregate = {
        "quality": {
            "status": "passed",
            "current_revision": "sha256:quality",
            "report_revision": "sha256:quality",
        },
        "release_gate": {
            "safe": False,
            "revision": "sha256:aggregate",
            "blockers": [
                {
                    "code": "release_video_invalid",
                    "message": "Canonical video is stale.",
                }
            ],
            "publish_plan": {
                "upload_post": {
                    "destinations": ["youtube"],
                    "account_identity": "sha256:user",
                },
                "video_podcast_rss": {"enabled": False},
            },
        },
        "approvals": {"editorial": {"current": True, "revision": "sha256:editorial"}},
    }

    with patch("agents.qa.quality_snapshot", return_value=aggregate):
        gate = longform_publication_snapshot(tmp_episode_dir, config={})

    assert gate["can_approve"] is False
    assert "release_video_invalid" in {item["code"] for item in gate["blockers"]}
