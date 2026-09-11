"""Tests for lib/audio_mix._build_from_crop_config — legacy and new schema."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lib.audio_mix import (
    AUDIO_SELECTION_PATH,
    AUDIO_SELECTION_SCHEMA,
    SELECTED_REPAIR_AUDIO_PATH,
    _build_from_crop_config,
    _cache_matches,
    _generate_audio_mix_locked,
    _generate_camera_audio_mix,
    _mix_fingerprint,
    audio_processing_settings,
    audio_selection_settings,
    generate_audio_mix,
    logical_track_groups,
)

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

    def test_repeated_track_numbers_keep_all_recorder_segments(self):
        crop = {"speakers": [{"track": 1, "volume": 1.0}]}
        tracks = [
            _track(1, "session_a_Tr1.wav"),
            _track(1, "session_b_Tr1.wav"),
        ]

        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert result[0]["stem"] == "session_a_Tr1"
        assert result[0]["stems"] == ["session_a_Tr1", "session_b_Tr1"]

    def test_logical_track_groups_preserve_session_order_and_exclude_camera(
        self, tmp_path
    ):
        recorder_a = tmp_path / "session_a_Tr1.wav"
        recorder_b = tmp_path / "session_b_Tr1.wav"
        camera = tmp_path / "camera_Tr2.wav"
        for path in (recorder_a, recorder_b, camera):
            path.write_bytes(b"audio")
        episode = {
            "audio_tracks": [
                {
                    **_track(1, recorder_a.name, str(recorder_a)),
                    "track_type": "input",
                },
                {
                    **_track(1, recorder_b.name, str(recorder_b)),
                    "track_type": "input",
                },
                {
                    **_track(2, camera.name, str(camera)),
                    "track_type": "camera_channel",
                },
            ]
        }

        groups = logical_track_groups(tmp_path, episode, recorder_only=True)

        assert [track["filename"] for track in groups[1]] == [
            recorder_a.name,
            recorder_b.name,
        ]
        assert 2 not in groups

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

    def test_unassigned_new_speakers_do_not_duplicate_camera_tracks(self):
        crop = {
            "speakers": [
                {"label": "Host", "center_x": 400, "center_y": 500},
                {"label": "Guest", "center_x": 1400, "center_y": 500},
            ],
            # Compatibility fields generated by crop-config save must not turn
            # an unassigned layout into two independently summed camera tracks.
            "speaker_l_center_x": 400,
            "speaker_r_center_x": 1400,
        }
        tracks = [
            _track(1, "camera_Tr1.WAV"),
            _track(2, "camera_Tr2.WAV"),
        ]

        result = _build_from_crop_config(
            Path("/episodes/ep_test"), _episode_data(crop, tracks)
        )

        assert result == []

    def test_no_crop_config_returns_empty(self):
        result = _build_from_crop_config(Path("/episodes/ep_test"), {})
        assert result == []


class TestMixCache:
    def test_fingerprint_changes_with_source_or_settings(self, tmp_path):
        source = tmp_path / "Tr1.wav"
        source.write_bytes(b"audio")
        episode = {"audio_sync": {"offset_seconds": 0}}

        original = _mix_fingerprint(episode, {}, [source])
        changed_setting = _mix_fingerprint(
            {"audio_sync": {"offset_seconds": 0.25}}, {}, [source]
        )
        source.write_bytes(b"different audio")
        changed_source = _mix_fingerprint(episode, {}, [source])

        assert len({original, changed_setting, changed_source}) == 3

    def test_fingerprint_ignores_picture_and_video_only_settings(self, tmp_path):
        source = tmp_path / "Tr1.wav"
        source.write_bytes(b"audio")
        episode = {"crop_config": {"speakers": [{"track": 1, "volume": 1.0}]}}
        original = _mix_fingerprint(
            episode, {"processing": {"video_crf": 18}}, [source]
        )
        changed_picture = _mix_fingerprint(
            {
                "crop_config": {
                    "speakers": [{"track": 1, "volume": 1.0, "center_x": 900}]
                }
            },
            {"processing": {"video_crf": 30}},
            [source],
        )
        changed_audio = _mix_fingerprint(
            episode, {"processing": {"audio_target_lufs": -14}}, [source]
        )

        assert changed_picture == original
        assert changed_audio != original

    def test_cache_requires_nonempty_output_and_matching_fingerprint(self, tmp_path):
        output = tmp_path / "audio_mix.wav"
        fingerprint_file = tmp_path / "audio_mix.fingerprint"
        output.write_bytes(b"0" * 45)
        fingerprint_file.write_text("current")

        assert _cache_matches(output, "current")
        assert not _cache_matches(output, "stale")
        output.write_bytes(b"")
        assert not _cache_matches(output, "current")


class TestRepairSelection:
    @staticmethod
    def _write_selection(episode_dir: Path, episode: dict, config: dict) -> Path:
        work = episode_dir / "work"
        work.mkdir()
        source = episode_dir / "source_merged.mp4"
        source.write_bytes(b"camera source")
        selected = episode_dir / SELECTED_REPAIR_AUDIO_PATH
        selected.write_bytes(b"reviewed repair")
        source_stat, selected_stat = source.stat(), selected.stat()
        record = {
            "schema": AUDIO_SELECTION_SCHEMA,
            "source": {
                "path": str(source.resolve()),
                "fingerprint": {
                    "id": "sha256:source",
                    "size_bytes": source_stat.st_size,
                    "mtime_ns": source_stat.st_mtime_ns,
                },
            },
            "audio_selection_settings": audio_selection_settings(episode),
            "audio_processing_settings": audio_processing_settings(config),
            "selected_output": {
                "path": str(selected.resolve()),
                "fingerprint": {
                    "id": "sha256:selected",
                    "size_bytes": selected_stat.st_size,
                    "mtime_ns": selected_stat.st_mtime_ns,
                },
            },
        }
        (episode_dir / AUDIO_SELECTION_PATH).write_text(json.dumps(record))
        return selected

    def test_generate_returns_current_selection_without_regenerating(self, tmp_path):
        episode, config = {"audio_sync": {}}, {"processing": {"audio_enhance": False}}
        selected = self._write_selection(tmp_path, episode, config)

        with patch("lib.audio_mix._generate_audio_mix_locked") as generate_base:
            result = generate_audio_mix(tmp_path, episode, config)

        assert result == selected
        generate_base.assert_not_called()

    def test_stale_selection_blocks_instead_of_falling_back(self, tmp_path):
        episode, config = {"audio_sync": {}}, {"processing": {"audio_enhance": False}}
        selected = self._write_selection(tmp_path, episode, config)
        selected.write_bytes(b"changed reviewed repair")

        with (
            patch("lib.audio_mix._generate_audio_mix_locked") as generate_base,
            pytest.raises(ValueError, match="changed since review"),
        ):
            generate_audio_mix(tmp_path, episode, config)

        generate_base.assert_not_called()


class TestMixGraph:
    def test_unassigned_camera_speakers_use_centered_camera_fallback(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        merged = tmp_path / "source_merged.mp4"
        merged.write_bytes(b"camera")
        episode = {
            "crop_config": {
                "speakers": [
                    {"label": "Host", "center_x": 400, "center_y": 500},
                    {"label": "Guest", "center_x": 1400, "center_y": 500},
                ],
                "speaker_l_center_x": 400,
                "speaker_r_center_x": 1400,
            },
            "audio_tracks": [
                _track(1, "camera_Tr1.WAV", str(tmp_path / "missing_Tr1.WAV")),
                _track(2, "camera_Tr2.WAV", str(tmp_path / "missing_Tr2.WAV")),
            ],
        }
        output = work / "audio_mix.wav"

        with patch(
            "lib.audio_mix._generate_camera_audio_mix", return_value=output
        ) as camera_mix:
            result = _generate_audio_mix_locked(tmp_path, episode, {}, work, output)

        assert result == output
        camera_mix.assert_called_once()

    def test_assigned_camera_channel_wavs_still_decode_source_timeline(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        merged = tmp_path / "source_merged.mp4"
        merged.write_bytes(b"camera")
        tracks = []
        for number in (1, 2):
            path = tmp_path / f"camera_Tr{number}.WAV"
            path.write_bytes(b"legacy collapsed audio")
            tracks.append(
                {
                    **_track(number, path.name, str(path)),
                    "track_type": "camera_channel",
                }
            )
        episode = {
            "crop_config": {
                "speakers": [{"track": 1}, {"track": 2}],
            },
            "audio_tracks": tracks,
        }
        output = work / "audio_mix.wav"

        with patch(
            "lib.audio_mix._generate_camera_audio_mix", return_value=output
        ) as camera_mix:
            result = _generate_audio_mix_locked(tmp_path, episode, {}, work, output)

        assert result == output
        assert camera_mix.call_args.args[:2] == (merged, output)

    def test_default_mix_avoids_independent_speaker_gain_riding(self, tmp_path):
        audio_dir = tmp_path / "audio"
        work_dir = tmp_path / "work"
        audio_dir.mkdir()
        work_dir.mkdir()
        for name in ("Tr1.wav", "Tr2.wav"):
            (audio_dir / name).write_bytes(b"source")
        episode = {
            "audio_tracks": [
                {"filename": "Tr1.wav", "dest_path": str(audio_dir / "Tr1.wav")},
                {"filename": "Tr2.wav", "dest_path": str(audio_dir / "Tr2.wav")},
            ],
            "audio_mix": {
                "tracks": [
                    {"stem": "Tr1", "role": "speaker"},
                    {"stem": "Tr2", "role": "speaker"},
                ]
            },
        }

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"0" * 100)
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch("lib.audio_mix.subprocess.run", side_effect=fake_run) as run:
            result = _generate_audio_mix_locked(
                tmp_path, episode, {}, work_dir, work_dir / "audio_mix.wav"
            )

        filter_graph = run.call_args.args[0][
            run.call_args.args[0].index("-filter_complex") + 1
        ]
        assert result == work_dir / "audio_mix.wav"
        assert "dynaudnorm" not in filter_graph
        assert "loudnorm" not in filter_graph
        assert "amix=inputs=2:duration=longest:normalize=0" in filter_graph
        assert "alimiter=limit=0.95" in filter_graph

    def test_camera_audio_is_averaged_to_dual_mono(self, tmp_path):
        source = tmp_path / "source_merged.mp4"
        output = tmp_path / "audio_mix.wav"
        source.write_bytes(b"source")

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"0" * 100)
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch("lib.audio_mix.subprocess.run", side_effect=fake_run) as run:
            result = _generate_camera_audio_mix(
                source, output, {}, fingerprint="fingerprint"
            )

        cmd = run.call_args.args[0]
        audio_filter = cmd[cmd.index("-af") + 1]
        assert result == output
        assert audio_filter.startswith(
            "aresample=async=1000:min_hard_comp=0.001:first_pts=0,"
        )
        assert "pan=mono|c0=0.5*c0+0.5*c1" in audio_filter
        assert "pan=stereo|c0=c0|c1=c0" in audio_filter
        assert output.read_bytes() == b"0" * 100
        assert output.with_suffix(".fingerprint").read_text() == "fingerprint"

    def test_repeated_recorder_segments_are_concatenated_before_mix(self, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        tracks = []
        for name in ("session_a_Tr1.wav", "session_b_Tr1.wav"):
            path = tmp_path / name
            path.write_bytes(b"source")
            tracks.append(_track(1, name, str(path)))
        episode = {
            "audio_tracks": tracks,
            "crop_config": {"speakers": [{"track": 1}]},
            "audio_sync": {
                "offset_seconds": 5.062456,
                "tempo_factor": 0.99997493,
                "r_squared": 0.9992,
            },
        }

        def fake_run(cmd, **_kwargs):
            Path(cmd[-1]).write_bytes(b"0" * 100)
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        with patch("lib.audio_mix.subprocess.run", side_effect=fake_run) as run:
            _generate_audio_mix_locked(
                tmp_path, episode, {}, work_dir, work_dir / "audio_mix.wav"
            )

        cmd = run.call_args.args[0]
        filter_graph = cmd[cmd.index("-filter_complex") + 1]
        assert str(tmp_path / "session_a_Tr1.wav") in cmd
        assert str(tmp_path / "session_b_Tr1.wav") in cmd
        assert "[s0_0][s0_1]concat=n=2:v=0:a=1[c0]" in filter_graph
        processing = (
            "[c0]anull,atrim=start=5.062456,asetpts=PTS-STARTPTS,atempo=0.99997493"
        )
        assert processing in filter_graph
        assert filter_graph.index("concat=n=2") < filter_graph.index("atrim=start=")
