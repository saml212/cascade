"""Tests for the QA agent."""

import json
from unittest.mock import patch

import pytest

from agents.qa import QAAgent


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
