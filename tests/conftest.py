"""Shared test fixtures for Cascade tests."""

import json
import os
import pytest
from pathlib import Path


@pytest.fixture
def tmp_episode_dir(tmp_path):
    """Create a temporary episode directory with standard structure."""
    ep_dir = tmp_path / "ep_2026-01-01_120000"
    ep_dir.mkdir()
    for sub in ["source", "shorts", "subtitles", "metadata", "qa", "work"]:
        (ep_dir / sub).mkdir()
    return ep_dir


@pytest.fixture
def sample_episode_json():
    """Return a sample episode.json dict."""
    return {
        "episode_id": "ep_2026-01-01_120000",
        "title": "Test Episode",
        "status": "processing",
        "source_path": "/tmp/test_source",
        "duration_seconds": 3600.0,
        "created_at": "2026-01-01T12:00:00+00:00",
        "clips": [],
        "pipeline": {
            "started_at": "2026-01-01T12:00:00+00:00",
            "completed_at": None,
            "agents_completed": [],
        },
        "crop_config": {
            "source_width": 1920,
            "source_height": 1080,
            "speaker_l_center_x": 480,
            "speaker_l_center_y": 540,
            "speaker_r_center_x": 1440,
            "speaker_r_center_y": 540,
        },
    }


@pytest.fixture
def sample_clips():
    """Return sample clips data."""
    return [
        {
            "id": "clip_01",
            "rank": 1,
            "start_seconds": 60.0,
            "end_seconds": 120.0,
            "start": 60.0,
            "end": 120.0,
            "duration": 60.0,
            "title": "Test Clip 1",
            "hook_text": "Amazing hook",
            "compelling_reason": "Very viral",
            "virality_score": 8,
            "speaker": "L",
            "status": "pending",
        },
        {
            "id": "clip_02",
            "rank": 2,
            "start_seconds": 300.0,
            "end_seconds": 360.0,
            "start": 300.0,
            "end": 360.0,
            "duration": 60.0,
            "title": "Test Clip 2",
            "hook_text": "Great start",
            "compelling_reason": "Insightful",
            "virality_score": 6,
            "speaker": "R",
            "status": "pending",
        },
    ]


@pytest.fixture
def sample_config():
    """Return a minimal config dict."""
    return {
        "paths": {
            "output_dir": "/tmp/cascade/episodes",
            "work_dir": "/tmp/cascade/work",
            "backup_dir": "/tmp/cascade/backup",
        },
        "processing": {
            "frame_seconds": 0.1,
            "speech_db_margin": 6,
            "min_segment_seconds": 0.8,
            "both_db_range": 6.0,
            "max_channel_correlation": 0.95,
            "max_channel_rms_ratio_delta": 3.0,
            "clip_min_seconds": 30,
            "clip_max_seconds": 90,
            "clip_count": 10,
            "video_crf": 18,
            "shorts_crf": 20,
            "audio_bitrate": "192k",
            "shorts_audio_bitrate": "128k",
        },
        "transcription": {
            "model": "nova-3",
            "language": "en",
            "diarize": True,
            "utterances": True,
            "smart_format": True,
        },
        "clip_mining": {
            "llm_model": "claude-sonnet-4-6",
            "llm_temperature": 0.3,
            "boundary_snap_tolerance_seconds": 3.0,
        },
        "podcast": {
            "title": "Test Podcast",
            "channel_handle": "@test",
        },
    }


@pytest.fixture
def mock_ffprobe_result():
    """Return a mock ffprobe JSON result."""
    return {
        "format": {
            "duration": "3600.123",
            "size": "5000000000",
            "tags": {
                "creation_time": "2026-01-01T10:00:00.000000Z",
            },
        },
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "codec_name": "h264",
                "duration": "3600.123",
            },
            {
                "codec_type": "audio",
                "channels": 2,
                "sample_rate": "48000",
                "codec_name": "aac",
                "duration": "3600.123",
            },
        ],
    }


@pytest.fixture
def episode_with_clips(tmp_episode_dir, sample_episode_json, sample_clips):
    """Create a tmp episode dir with episode.json and clips.json."""
    with open(tmp_episode_dir / "episode.json", "w") as f:
        json.dump(sample_episode_json, f)
    with open(tmp_episode_dir / "clips.json", "w") as f:
        json.dump({"clips": sample_clips}, f)
    return tmp_episode_dir
