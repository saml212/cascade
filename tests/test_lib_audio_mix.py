"""Tests for lib/audio_mix._build_from_crop_config — legacy and new schema."""

from pathlib import Path

import pytest

from lib.audio_mix import _build_from_crop_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _episode_data(crop_config: dict, audio_tracks: list[dict] | None = None) -> dict:
    data: dict = {"crop_config": crop_config}
    if audio_tracks is not None:
        data["audio_tracks"] = audio_tracks
    return data


def _track(number: int, filename: str, dest: str | None = None) -> dict:
    return {
        "track_number": number,
        "filename": filename,
        "dest_path": dest or f"/episodes/ep_test/audio/{filename}",
    }


# ---------------------------------------------------------------------------
# 1. Legacy crop_config (speaker_l_/speaker_r_, no speakers array)
# ---------------------------------------------------------------------------


class TestLegacyCropConfig:
    """Legacy 2-speaker crop_config synthesizes a 2-entry speakers list."""

    def test_returns_two_entries_mapped_to_track1_and_track2(self):
        crop = {
            "source_width": 1920,
            "source_height": 1080,
            "speaker_l_center_x": 658,
            "speaker_l_center_y": 465,
            "speaker_r_center_x": 1242,
            "speaker_r_center_y": 462,
        }
        tracks = [
            _track(1, "H6E_Tr1.wav"),
            _track(2, "H6E_Tr2.wav"),
        ]
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert len(result) == 2
        assert result[0]["stem"] == "H6E_Tr1"
        assert result[1]["stem"] == "H6E_Tr2"
        assert result[0]["volume"] == 1.0
        assert result[1]["volume"] == 1.0
        assert result[0]["role"] == "speaker"
        assert result[1]["role"] == "speaker"

    def test_legacy_with_zoom_fields_also_synthesizes(self):
        """Laura/Todd variant also has speaker_l_zoom etc. — still triggers fallback."""
        crop = {
            "source_width": 3840,
            "source_height": 2160,
            "speaker_l_center_x": 700,
            "speaker_l_center_y": 500,
            "speaker_l_zoom": 2.0,
            "speaker_r_center_x": 1200,
            "speaker_r_center_y": 500,
            "speaker_r_zoom": 2.0,
            "zoom": 1.5,
        }
        tracks = [
            _track(1, "Tr1.wav"),
            _track(2, "Tr2.wav"),
        ]
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert len(result) == 2
        assert result[0]["stem"] == "Tr1"
        assert result[1]["stem"] == "Tr2"


# ---------------------------------------------------------------------------
# 2. Legacy crop_config but no audio_tracks → graceful (empty stems → filter)
# ---------------------------------------------------------------------------


class TestLegacyCropConfigNoAudioTracks:
    """When audio_tracks is absent the synthesized entries have stem=None.

    These entries are filtered out by the caller (stem not in stem_to_path),
    which means the mix falls through to camera-audio mode — graceful, no crash.
    """

    def test_returns_two_entries_with_none_stems(self):
        crop = {
            "source_width": 1920,
            "source_height": 1080,
            "speaker_l_center_x": 658,
            "speaker_l_center_y": 465,
            "speaker_r_center_x": 1242,
            "speaker_r_center_y": 462,
        }
        # No audio_tracks key in episode_data
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, audio_tracks=None)
        )

        assert len(result) == 2
        assert result[0]["stem"] is None
        assert result[1]["stem"] is None
        # Roles and volumes still set correctly
        assert all(e["role"] == "speaker" for e in result)
        assert all(e["volume"] == 1.0 for e in result)

    def test_empty_audio_tracks_list_also_produces_none_stems(self):
        crop = {
            "speaker_l_center_x": 100,
            "speaker_l_center_y": 200,
            "speaker_r_center_x": 300,
            "speaker_r_center_y": 200,
        }
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, audio_tracks=[])
        )

        assert len(result) == 2
        assert all(e["stem"] is None for e in result)


# ---------------------------------------------------------------------------
# 3. New schema with speakers array → unchanged behavior (regression)
# ---------------------------------------------------------------------------


class TestNewSchemaSpeakersArray:
    """N-speaker crop_config (speakers array) is unaffected by the legacy path."""

    def test_new_schema_returns_speakers_from_array(self):
        crop = {
            "source_width": 3840,
            "source_height": 2160,
            "speakers": [
                {"id": "spk_0", "track": 1, "volume": 1.0},
                {"id": "spk_1", "track": 2, "volume": 0.9},
                {"id": "spk_2", "track": 3, "volume": 1.1},
            ],
        }
        tracks = [
            _track(1, "Tr1.wav"),
            _track(2, "Tr2.wav"),
            _track(3, "Tr3.wav"),
        ]
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert len(result) == 3
        assert result[0]["stem"] == "Tr1"
        assert result[1]["stem"] == "Tr2"
        assert result[2]["stem"] == "Tr3"
        assert result[1]["volume"] == pytest.approx(0.9)
        assert result[2]["volume"] == pytest.approx(1.1)
        assert all(e["role"] == "speaker" for e in result)

    def test_new_schema_with_ambient_tracks(self):
        crop = {
            "speakers": [
                {"id": "spk_0", "track": 1, "volume": 1.0},
            ],
            "ambient_tracks": [
                {"track_number": 5, "volume": 0.2},
            ],
        }
        tracks = [
            _track(1, "Tr1.wav"),
            _track(5, "TrLR.wav"),
        ]
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert len(result) == 2
        speaker_entries = [e for e in result if e["role"] == "speaker"]
        ambient_entries = [e for e in result if e["role"] == "ambient"]
        assert len(speaker_entries) == 1
        assert len(ambient_entries) == 1
        assert ambient_entries[0]["stem"] == "TrLR"
        assert ambient_entries[0]["volume"] == pytest.approx(0.2)

    def test_new_schema_does_not_trigger_legacy_fallback_even_with_l_fields(self):
        """If speakers array is present AND populated, legacy path must NOT activate
        even if speaker_l_center_x also happens to be in the dict (defensive)."""
        crop = {
            "speakers": [
                {"id": "spk_0", "track": 1, "volume": 1.0},
            ],
            # Hypothetical stale field — must not cause double-synthesis
            "speaker_l_center_x": 658,
        }
        tracks = [_track(1, "Tr1.wav")]
        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert len(result) == 1
        assert result[0]["stem"] == "Tr1"

    def test_no_crop_config_returns_empty(self):
        result = _build_from_crop_config(Path("/episodes/ep_test"), {})
        assert result == []
