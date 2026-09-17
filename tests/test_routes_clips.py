"""Tests for clip API routes."""

# ruff: noqa: F811 - imported pytest fixtures are intentionally shadowed by arguments

import asyncio
import json
import threading
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from tests.test_routes_episodes import _create_episode, test_client  # noqa: F401


def _add_clips(episodes_dir, episode_id, clips):
    ep_dir = episodes_dir / episode_id
    with open(ep_dir / "clips.json", "w") as f:
        json.dump({"clips": clips}, f)


def _release_request(request_id="47db1913-4d32-4acf-bcfe-31763c50e9c2"):
    return {
        "request_id": request_id,
        "actor": "release-operator",
        "reason": "Rebuilt episode with current media",
        "variant_id": None,
        "target_revision": "sha256:target",
        "render_fingerprint": "sha256:render",
        "receipt_history_revision": "sha256:history",
        "revision": "sha256:authorization",
        "created_at": "2026-09-13T20:00:00+00:00",
    }


SAMPLE_CLIPS = [
    {
        "id": "clip_01",
        "start_seconds": 60,
        "end_seconds": 120,
        "start": 60,
        "end": 120,
        "duration": 60,
        "title": "Clip 1",
        "virality_score": 8,
        "status": "pending",
        "speaker": "L",
    },
    {
        "id": "clip_02",
        "start_seconds": 300,
        "end_seconds": 360,
        "start": 300,
        "end": 360,
        "duration": 60,
        "title": "Clip 2",
        "virality_score": 5,
        "status": "pending",
        "speaker": "R",
    },
]

ACTIVE_VARIANT_IDS = ("gameplay_surround_v1", "speaker_panels_v1")
RETIRED_VARIANT_IDS = (
    "background_motion_v1",
    "satisfying_motion_v1",
    "minecraft_parkour_v1",
    "subway_surfers_v1",
    "gta_driving_v1",
)


class TestListClips:
    def test_list_clips(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.get("/api/episodes/ep_001/clips")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_empty(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.get("/api/episodes/ep_001/clips")
        assert resp.status_code == 200
        assert resp.json() == []


class TestGetClip:
    def test_get_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.get("/api/episodes/ep_001/clips/clip_01")
        assert resp.status_code == 200
        assert resp.json()["id"] == "clip_01"

    def test_get_clip_not_found(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.get("/api/episodes/ep_001/clips/clip_99")
        assert resp.status_code == 404


class TestCaptionSpeakerOverrides:
    @staticmethod
    def _prepare(test_client, monkeypatch):
        client, episodes_dir = test_client
        ep_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {"crop_config": {"speakers": [{}, {}, {}]}},
        )
        clip = {
            "id": "clip_04",
            "start_seconds": 100.0,
            "end_seconds": 110.0,
            "start": 100.0,
            "end": 110.0,
            "duration": 10.0,
            "title": "Reviewed words",
            "status": "approved",
        }
        _add_clips(episodes_dir, "ep_001", [clip])
        diarized = {
            "clock": "source",
            "speaker_map": [
                {"index": 1, "target_speaker": "speaker_1"},
                {"index": 2, "target_speaker": "speaker_2"},
            ],
            "utterances": [
                {
                    "speaker": 1,
                    "words": [
                        {
                            "word": "sure",
                            "punctuated_word": "Sure.",
                            "start": 103.0,
                            "end": 103.5,
                            "speaker": 1,
                        },
                        {
                            "word": "next",
                            "punctuated_word": "Next.",
                            "start": 103.5,
                            "end": 104.0,
                            "speaker": 1,
                        },
                    ],
                }
            ],
        }
        segments = {"clock": "source", "track_mapping": []}
        (ep_dir / "diarized_transcript.json").write_text(json.dumps(diarized))
        (ep_dir / "segments.json").write_text(json.dumps(segments))

        import agents.pipeline as pipeline_mod
        import agents.speaker_cut as speaker_cut_mod
        import agents.transcribe as transcribe_mod

        monkeypatch.setattr(pipeline_mod, "load_config", dict)
        monkeypatch.setattr(
            transcribe_mod,
            "current_diarized_transcript",
            lambda *_args, **_kwargs: diarized,
        )
        monkeypatch.setattr(
            speaker_cut_mod,
            "current_speaker_segments",
            lambda *_args, **_kwargs: segments,
        )
        return client, ep_dir

    @staticmethod
    def _request(state, *, word="sure"):
        return {
            "expected_document_revision": state["document_revision"],
            "expected_transcript_revision": state["transcript_revision"],
            "actor": "caption-reviewer",
            "reason": "Reviewed source picture and close microphones.",
            "overrides": [
                {
                    "id": "review_sure",
                    "start": 103.0,
                    "end": 103.5,
                    "from_asr_speaker": 1,
                    "to_asr_speaker": 2,
                    "source_speaker": "speaker_1",
                    "target_crop": "speaker_2",
                    "reason": "Reviewed source picture and close microphones.",
                    "expected_words": [
                        {
                            "word": word,
                            "punctuated_word": "Sure.",
                            "start": 103.0,
                            "end": 103.5,
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _text_request(state):
        return {
            "expected_document_revision": state["document_revision"],
            "expected_transcript_revision": state["transcript_revision"],
            "actor": "caption-reviewer",
            "reason": "Reviewed overlapping source picture and close microphones.",
            "overrides": [],
            "text_replacements": [
                {
                    "id": "review_parallel_sure",
                    "start": 103.0,
                    "end": 103.5,
                    "from_asr_speaker": 1,
                    "source_speaker": "speaker_1",
                    "reason": "Reviewed source picture and both close microphones.",
                    "expected_words": [
                        {
                            "word": "sure",
                            "punctuated_word": "Sure.",
                            "start": 103.0,
                            "end": 103.5,
                        }
                    ],
                    "display_phrases": [
                        {
                            "id": "speaker_one",
                            "to_asr_speaker": 1,
                            "target_crop": "speaker_1",
                            "words": [
                                {
                                    "word": "sure",
                                    "punctuated_word": "Sure—",
                                    "start": 103.0,
                                    "end": 103.35,
                                }
                            ],
                        },
                        {
                            "id": "speaker_two",
                            "to_asr_speaker": 2,
                            "target_crop": "speaker_2",
                            "words": [
                                {
                                    "word": "yes",
                                    "punctuated_word": "Yes.",
                                    "start": 103.2,
                                    "end": 103.5,
                                }
                            ],
                        },
                    ],
                }
            ],
        }

    def test_put_is_cas_guarded_and_changes_only_caption_sidecar(
        self, test_client, monkeypatch
    ):
        client, ep_dir = self._prepare(test_client, monkeypatch)
        endpoint = "/api/episodes/ep_001/clips/clip_04/caption-speaker-overrides"
        inspected = client.get(endpoint)
        assert inspected.status_code == 200
        initial = inspected.json()
        assert initial["current"] is True
        assert initial["override_count"] == 0
        canonical_paths = [
            ep_dir / "episode.json",
            ep_dir / "clips.json",
            ep_dir / "diarized_transcript.json",
            ep_dir / "segments.json",
        ]
        before = {path: path.read_bytes() for path in canonical_paths}

        saved = client.put(endpoint, json=self._request(initial))

        assert saved.status_code == 200
        state = saved.json()
        assert state["current"] is True
        assert state["override_count"] == 1
        assert state["applied_word_count"] == 1
        assert state["affected_variants"] == [
            "gameplay_surround_v1",
            "speaker_panels_v1",
        ]
        assert all(path.read_bytes() == before[path] for path in canonical_paths)
        assert (ep_dir / "caption_speaker_overrides" / "clip_04.json").is_file()

        stale = client.put(endpoint, json=self._request(initial))
        assert stale.status_code == 409
        assert "changed" in stale.json()["detail"]

        clear = {
            "expected_document_revision": state["document_revision"],
            "expected_transcript_revision": state["transcript_revision"],
            "actor": "caption-reviewer",
            "reason": "Remove the reviewed caption attribution.",
            "overrides": [],
        }
        cleared = client.put(endpoint, json=clear)
        assert cleared.status_code == 200
        assert cleared.json()["override_count"] == 0
        assert not (ep_dir / "caption_speaker_overrides" / "clip_04.json").exists()
        assert all(path.read_bytes() == before[path] for path in canonical_paths)

    def test_word_mismatch_writes_nothing(self, test_client, monkeypatch):
        client, ep_dir = self._prepare(test_client, monkeypatch)
        endpoint = "/api/episodes/ep_001/clips/clip_04/caption-speaker-overrides"
        state = client.get(endpoint).json()

        response = client.put(endpoint, json=self._request(state, word="wrong"))

        assert response.status_code == 409
        assert "no longer matches" in response.json()["detail"]
        assert not (ep_dir / "caption_speaker_overrides" / "clip_04.json").exists()

    def test_put_text_replacement_is_v2_cas_guarded_and_canonical_is_unchanged(
        self, test_client, monkeypatch
    ):
        client, ep_dir = self._prepare(test_client, monkeypatch)
        endpoint = "/api/episodes/ep_001/clips/clip_04/caption-speaker-overrides"
        initial = client.get(endpoint).json()
        canonical_paths = [
            ep_dir / "episode.json",
            ep_dir / "clips.json",
            ep_dir / "diarized_transcript.json",
            ep_dir / "segments.json",
        ]
        before = {path: path.read_bytes() for path in canonical_paths}

        saved = client.put(endpoint, json=self._text_request(initial))

        assert saved.status_code == 200
        state = saved.json()
        assert state["schema"] == "cascade.short-caption-speaker-overrides/v2"
        assert state["override_count"] == 0
        assert state["applied_word_count"] == 1
        assert len(state["text_replacements"][0]["display_phrases"]) == 2
        sidecar = json.loads(
            (ep_dir / "caption_speaker_overrides" / "clip_04.json").read_text()
        )
        assert sidecar["schema"] == "cascade.short-caption-speaker-overrides/v2"
        assert all(path.read_bytes() == before[path] for path in canonical_paths)

        stale = client.put(endpoint, json=self._text_request(initial))
        assert stale.status_code == 409
        assert "changed" in stale.json()["detail"]

    def test_clip_change_during_validation_conflicts_before_write(
        self, test_client, monkeypatch
    ):
        client, ep_dir = self._prepare(test_client, monkeypatch)
        endpoint = "/api/episodes/ep_001/clips/clip_04/caption-speaker-overrides"
        state = client.get(endpoint).json()

        from server.routes import clips as clips_mod

        original_validate = clips_mod.validate_caption_speaker_override_document

        def validate_then_move_clip(*args, **kwargs):
            result = original_validate(*args, **kwargs)
            clips = json.loads((ep_dir / "clips.json").read_text())
            clips["clips"][0]["start_seconds"] = 105.0
            clips["clips"][0]["start"] = 105.0
            (ep_dir / "clips.json").write_text(json.dumps(clips))
            return result

        monkeypatch.setattr(
            clips_mod,
            "validate_caption_speaker_override_document",
            validate_then_move_clip,
        )

        response = client.put(endpoint, json=self._request(state))

        assert response.status_code == 409
        assert "changed while" in response.json()["detail"]
        assert not (ep_dir / "caption_speaker_overrides" / "clip_04.json").exists()

    @pytest.mark.parametrize(
        ("field", "value"),
        (("start", False), ("start", "103.0"), ("from_asr_speaker", True)),
    )
    def test_numeric_fields_are_strict(self, test_client, monkeypatch, field, value):
        client, _ep_dir = self._prepare(test_client, monkeypatch)
        endpoint = "/api/episodes/ep_001/clips/clip_04/caption-speaker-overrides"
        state = client.get(endpoint).json()
        request = self._request(state)
        request["overrides"][0][field] = value

        response = client.put(endpoint, json=request)

        assert response.status_code == 422


class TestApproveReject:
    def test_approve_clip_binds_current_render(self, test_client, monkeypatch):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        import server.routes.clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_current_render",
            lambda _episode_dir, _clip: {"fingerprint": "sha256:current"},
        )
        resp = client.post("/api/episodes/ep_001/clips/clip_01/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ][0]
        assert stored["approved_render_fingerprint"] == "sha256:current"
        assert stored["approved_revision"].startswith("sha256:")

    def test_approve_clip_rejects_unrendered_candidate(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        resp = client.post("/api/episodes/ep_001/clips/clip_01/approve")

        assert resp.status_code == 409
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ][0]
        assert stored["status"] == "pending"

    def test_reject_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post("/api/episodes/ep_001/clips/clip_01/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ][0]
        assert stored["selection_status"] == "rejected"

    def test_select_does_not_grant_final_approval(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        resp = client.post("/api/episodes/ep_001/clips/clip_01/select")

        assert resp.status_code == 200
        assert resp.json()["selection_status"] == "selected"
        assert resp.json()["status"] == "pending"

    def test_alternative_uses_configured_generation_path(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        generated = {
            "rejected_clip_id": "clip_01",
            "alternative": {"id": "clip_03", "title": "Fresh choice"},
            "generation": {"provider": "openai", "model": "test-model"},
        }

        from agents.clip_miner import ClipMinerAgent

        monkeypatch.setattr(
            ClipMinerAgent,
            "generate_alternative",
            lambda _self, _clip_id: generated,
        )
        response = client.post("/api/episodes/ep_001/clips/clip_01/alternative")

        assert response.status_code == 200
        assert response.json() == generated

    def test_bulk_approval_is_atomic_when_one_render_is_stale(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        import server.routes.clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_current_render",
            lambda _episode_dir, clip: (
                {"fingerprint": "sha256:current"} if clip["id"] == "clip_01" else None
            ),
        )
        resp = client.post(
            "/api/episodes/ep_001/clips/bulk/approve",
            json={"clip_ids": ["clip_01", "clip_02"]},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["clip_ids"] == ["clip_02"]
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ]
        assert [clip["status"] for clip in stored] == ["pending", "pending"]

    def test_bulk_reject_preserves_final_approval(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        clips = [dict(SAMPLE_CLIPS[0], status="approved"), SAMPLE_CLIPS[1]]
        _add_clips(episodes_dir, "ep_001", clips)

        resp = client.post(
            "/api/episodes/ep_001/clips/bulk/reject", json={"max_score": 10}
        )

        assert resp.status_code == 200
        assert resp.json()["rejected"] == ["clip_02"]
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ]
        assert [clip["status"] for clip in stored] == ["approved", "rejected"]


class TestDistributionSelection:
    @staticmethod
    def _state(candidate, *, current=True, approval_current=True):
        variant_id = candidate.get("distribution_variant_id")
        labels = {
            "gameplay_surround_v1": "Gameplay surround",
            "speaker_panels_v1": "Clean speaker panels",
        }
        return {
            "version": variant_id or "base",
            "variant_id": variant_id,
            "label": labels.get(variant_id, "Base"),
            "current": current,
            "approval_current": approval_current,
            "active_for_new_writes": (
                variant_id is None or variant_id in ACTIVE_VARIANT_IDS
            ),
            "revision": "sha256:selected-review",
            "render_fingerprint": "sha256:selected-render",
            "re_release_request": None,
        }

    @staticmethod
    def _destination_state(candidate):
        variant_id = candidate.get("distribution_variant_id")
        identities = {
            "gameplay_surround_v1": ("1", "2"),
            "speaker_panels_v1": ("3", "4"),
        }
        revision, fingerprint = identities[variant_id]
        return {
            "version": variant_id,
            "variant_id": variant_id,
            "label": variant_id,
            "current": True,
            "approval_current": True,
            "active_for_new_writes": True,
            "revision": "sha256:" + revision * 64,
            "render_fingerprint": "sha256:" + fingerprint * 64,
            "re_release_request": None,
        }

    def test_selects_exact_approved_variant_and_can_restore_base(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(SAMPLE_CLIPS[0], status="approved")
        _add_clips(episodes_dir, "ep_001", [clip])

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "publication_change_lock",
            lambda *_: {"change_locked": False, "change_lock_reason": None},
        )
        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )

        selected = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "gameplay_surround_v1",
                "expected_revision": "sha256:selected-review",
            },
        )
        restored = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": None,
                "expected_revision": "sha256:selected-review",
            },
        )

        assert selected.status_code == 200
        assert selected.json()["distribution"] == {
            "version": "gameplay_surround_v1",
            "variant_id": "gameplay_surround_v1",
            "label": "Gameplay surround",
            "current": True,
            "approval_current": True,
            "active_for_new_writes": True,
            "revision": "sha256:selected-review",
            "re_release_request": None,
            "change_locked": False,
            "change_lock_reason": None,
        }
        assert restored.status_code == 200
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    @pytest.mark.parametrize(
        ("current", "approval_current", "revision", "detail"),
        (
            (True, True, "sha256:changed", "changed; refresh"),
            (False, True, "sha256:selected-review", "current render"),
            (True, False, "sha256:selected-review", "Approve this exact"),
        ),
    )
    def test_selection_fails_closed_for_stale_missing_or_unapproved_version(
        self,
        test_client,
        monkeypatch,
        current,
        approval_current,
        revision,
        detail,
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "publication_change_lock",
            lambda *_: {"change_locked": False, "change_lock_reason": None},
        )
        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: (
                self._state(
                    candidate,
                    current=current,
                    approval_current=approval_current,
                )
                | {"revision": revision}
            ),
        )

        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "gameplay_surround_v1",
                "expected_revision": "sha256:selected-review",
            },
        )

        assert response.status_code == 409
        assert detail in response.json()["detail"]
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    def test_unknown_variant_is_rejected_without_persisting(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={"variant_id": "unknown", "expected_revision": "sha256:any"},
        )

        assert response.status_code == 404
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    def test_any_recorded_receipt_locks_legacy_base_selection(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "release_revision": "sha256:older-release",
                    "shorts": [
                        {
                            "clip_id": "clip_01",
                            "status": "submitted",
                            "request_id": "legacy-job",
                        }
                    ],
                }
            )
        )

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "gameplay_surround_v1",
                "expected_revision": "sha256:selected-review",
            },
        )

        assert response.status_code == 409
        assert "historical or current publication receipt" in response.json()["detail"]
        assert "explicit re-release identity" in response.json()["detail"]

    def test_pre_schema_receipt_without_release_or_version_still_locks_base(
        self, test_client
    ):
        _client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "shorts": [
                        {"clip_id": "clip_01", "status": "unknown"},
                    ]
                }
            )
        )

        from server.routes import clips as clips_mod

        state = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert state["change_locked"] is True
        assert "explicit re-release identity" in state["change_lock_reason"]

    def test_failed_or_malformed_receipt_history_fails_closed(self, test_client):
        _client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        receipt_path = episode_dir / "publish.json"
        receipt_path.write_text(
            json.dumps({"shorts": [{"clip_id": "clip_01", "status": "failed"}]})
        )

        from server.routes import clips as clips_mod

        failed = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert failed["change_locked"] is True
        assert failed["re_release_allowed"] is False
        assert "unresolved remote destinations" in failed["re_release_reason"]
        receipt_path.write_text(json.dumps({"shorts": ["not-a-receipt"]}))
        malformed = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert malformed["change_locked"] is True
        assert "cannot be verified" in malformed["change_lock_reason"]
        receipt_path.write_text(json.dumps({"shorts": [{"status": "submitted"}]}))
        unattributed = clips_mod.publication_change_lock(episode_dir, "clip_01")
        assert unattributed["change_locked"] is True
        assert "cannot be verified" in unattributed["change_lock_reason"]

        malformed_request = clips_mod.publication_change_lock(
            episode_dir, "clip_01", None
        )
        assert malformed_request["change_locked"] is True
        assert malformed_request["re_release_allowed"] is False
        assert "cannot be verified" in malformed_request["change_lock_reason"]

    @pytest.mark.parametrize("publish_state", (None, {"shorts": []}))
    def test_stored_rerelease_without_its_receipt_history_blocks_selection(
        self, test_client, monkeypatch, publish_state
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        clip = {
            **SAMPLE_CLIPS[0],
            "distribution_release": _release_request(),
        }
        _add_clips(episodes_dir, "ep_001", [clip])
        if publish_state is not None:
            (episode_dir / "publish.json").write_text(json.dumps(publish_state))

        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        response = client.put(
            "/api/episodes/ep_001/clips/clip_01/distribution",
            json={
                "variant_id": "gameplay_surround_v1",
                "expected_revision": "sha256:selected-review",
            },
        )

        assert response.status_code == 409
        assert "cannot be verified" in response.json()["detail"]

    def test_selection_waits_until_inflight_publish_receipt_is_saved(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        from agents import publish as publish_mod
        from server.routes import clips as clips_mod

        shared_lock = threading.Lock()

        @contextmanager
        def guarded_publication(_episodes_dir):
            with shared_lock:
                yield

        monkeypatch.setattr(publish_mod, "publication_lock", guarded_publication)
        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        remote_submission_finished = threading.Event()
        allow_receipt_write = threading.Event()
        agent = publish_mod.PublishAgent(episode_dir, {})

        def execute():
            remote_submission_finished.set()
            assert allow_receipt_write.wait(timeout=2)
            return {
                "release_revision": "sha256:old-release",
                "shorts": [
                    {
                        "clip_id": "clip_01",
                        "status": "submitted",
                        "external_id": "legacy-base-job",
                    }
                ],
            }

        monkeypatch.setattr(agent, "execute", execute)
        publish_thread = threading.Thread(target=agent.run)
        publish_thread.start()
        assert remote_submission_finished.wait(timeout=2)

        outcome = {}

        def select():
            try:
                outcome["result"] = clips_mod._select_clip_distribution_locked(
                    "ep_001",
                    "clip_01",
                    clips_mod.DistributionSelectionRequest(
                        variant_id="gameplay_surround_v1",
                        expected_revision="sha256:selected-review",
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome["error"] = exc

        selection_thread = threading.Thread(target=select)
        selection_thread.start()
        selection_thread.join(timeout=0.05)
        assert selection_thread.is_alive()

        allow_receipt_write.set()
        publish_thread.join(timeout=2)
        selection_thread.join(timeout=2)

        assert not publish_thread.is_alive()
        assert not selection_thread.is_alive()
        assert isinstance(outcome.get("error"), HTTPException)
        assert outcome["error"].status_code == 409
        assert "explicit re-release identity" in outcome["error"].detail
        assert (episode_dir / "publish.json").is_file()
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "distribution_variant_id" not in stored

    def test_prepares_idempotent_rerelease_without_rewriting_receipts(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        receipt = {
            "clip_id": "clip_01",
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
                        "profile_username": "up",
                    }
                ],
            },
        }
        publish_path = episode_dir / "publish.json"
        publish_path.write_text(json.dumps({"shorts": [receipt]}))
        publish_before = publish_path.read_bytes()

        from agents.qa import quality_revision, release_revision
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        episode = json.loads((episode_dir / "episode.json").read_text())
        quality_before = quality_revision(episode_dir, episode, config={})
        release_before = release_revision(
            episode_dir, episode, config={}, environment={}
        )
        body = {
            "variant_id": None,
            "expected_revision": "sha256:selected-review",
            "request_id": "47db1913-4d32-4acf-bcfe-31763c50e9c2",
            "actor": "release-operator",
            "reason": "Rebuilt episode with current media",
        }

        prepared = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release", json=body
        )
        stored_after_first = (episode_dir / "clips.json").read_bytes()
        repeated = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release", json=body
        )
        conflicting = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                **body,
                "request_id": "87c7c0fe-ad21-44e0-a468-5a70609f6fbc",
            },
        )

        assert prepared.status_code == 200
        assert prepared.json()["status"] == "prepared"
        assert prepared.json()["requires_publish_approval"] is True
        assert prepared.json()["distribution"]["re_release_request_consumed"] is False
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "already_prepared"
        assert conflicting.status_code == 409
        assert "already prepared" in conflicting.json()["detail"]
        assert (episode_dir / "clips.json").read_bytes() == stored_after_first
        assert publish_path.read_bytes() == publish_before
        stored_clip = json.loads(stored_after_first)["clips"][0]
        request = stored_clip["distribution_release"]
        assert request["request_id"] == body["request_id"]
        assert request["target_revision"] == "sha256:selected-review"
        assert quality_revision(episode_dir, episode, config={}) == quality_before
        assert (
            release_revision(episode_dir, episode, config={}, environment={})
            != release_before
        )

        publish = json.loads(publish_path.read_text())
        publish["shorts"].append(
            {
                "clip_id": "clip_02",
                "status": "failed",
                "rerelease_request_id": body["request_id"],
            }
        )
        publish_path.write_text(json.dumps(publish))
        cross_clip_receipt = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                **body,
                "request_id": "233e53ba-341d-47f7-a659-b48e8b3846dd",
            },
        )
        assert cross_clip_receipt.status_code == 409
        assert "already prepared" in cross_clip_receipt.json()["detail"]
        assert (episode_dir / "clips.json").read_bytes() == stored_after_first

        publish["shorts"].append(
            {
                "clip_id": "clip_01",
                "status": "submitted",
                "rerelease_request_id": body["request_id"],
            }
        )
        publish_path.write_text(json.dumps(publish))
        consumed = clips_mod.publication_change_lock(episode_dir, "clip_01", request)
        assert consumed["re_release_request_consumed"] is True
        assert consumed["re_release_allowed"] is False

    def test_prepares_exact_destination_pair_without_rewriting_history(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        platforms = ["facebook", "instagram", "tiktok", "youtube", "x"]
        receipt = {
            "clip_id": "clip_01",
            "status": "published",
            "request_id": "old-request",
            "platforms": platforms,
            "response": {
                "status": "completed",
                "request_id": "old-request",
                "results": [
                    {
                        "platform": platform,
                        "success": True,
                        "post_url": f"https://example.com/{platform}/old",
                        "request_id": "old-request",
                        "profile_username": "up",
                    }
                    for platform in platforms
                ],
            },
        }
        publish_path = episode_dir / "publish.json"
        publish_path.write_text(
            json.dumps({"profile_username": "up", "shorts": [receipt]})
        )
        publish_before = publish_path.read_bytes()

        from agents.publish import valid_rerelease_authorization
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._destination_state(candidate),
        )
        body = {
            "request_id": "9d3722d3-1c5f-42b2-8f60-643d28ee274c",
            "actor": "release-operator",
            "reason": "Release exact gameplay and clean destination waves",
            "targets": [
                {
                    "variant_id": "gameplay_surround_v1",
                    "expected_revision": "sha256:" + "1" * 64,
                    "destinations": [
                        "facebook",
                        "instagram",
                        "tiktok",
                        "youtube",
                    ],
                },
                {
                    "variant_id": "speaker_panels_v1",
                    "expected_revision": "sha256:" + "3" * 64,
                    "destinations": ["x"],
                },
            ],
        }

        wrong_destinations = json.loads(json.dumps(body))
        wrong_destinations["targets"][1]["destinations"] = ["instagram", "x"]
        rejected = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets",
            json=wrong_destinations,
        )
        extra_field = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets",
            json={**body, "scope": "all"},
        )
        prepared = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets", json=body
        )
        stored_after_first = (episode_dir / "clips.json").read_bytes()
        repeated = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets", json=body
        )

        assert rejected.status_code == 422
        assert extra_field.status_code == 422
        assert prepared.status_code == 200
        assert prepared.json()["status"] == "prepared"
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "already_prepared"
        assert (episode_dir / "clips.json").read_bytes() == stored_after_first
        assert publish_path.read_bytes() == publish_before

        stored_clip = json.loads(stored_after_first)["clips"][0]
        authorization = stored_clip["distribution_release"]
        assert stored_clip["distribution_variant_id"] == "gameplay_surround_v1"
        assert authorization["schema"] == "cascade.destination-release/v1"
        assert authorization["targets"] == [
            {
                "variant_id": "gameplay_surround_v1",
                "target_revision": "sha256:" + "1" * 64,
                "render_fingerprint": "sha256:" + "2" * 64,
                "destinations": ["facebook", "instagram", "tiktok", "youtube"],
            },
            {
                "variant_id": "speaker_panels_v1",
                "target_revision": "sha256:" + "3" * 64,
                "render_fingerprint": "sha256:" + "4" * 64,
                "destinations": ["x"],
            },
        ]
        publish = json.loads(publish_before)
        gameplay = {
            "variant_id": "gameplay_surround_v1",
            "revision": "sha256:" + "1" * 64,
            "render_fingerprint": "sha256:" + "2" * 64,
        }
        clean = {
            "variant_id": "speaker_panels_v1",
            "revision": "sha256:" + "3" * 64,
            "render_fingerprint": "sha256:" + "4" * 64,
        }
        assert valid_rerelease_authorization(
            publish,
            stored_clip,
            gameplay,
            destinations=["facebook", "instagram", "tiktok", "youtube"],
        )
        assert valid_rerelease_authorization(
            publish, stored_clip, clean, destinations=["x"]
        )
        assert not valid_rerelease_authorization(
            publish, stored_clip, clean, destinations=["instagram", "x"]
        )
        assert not valid_rerelease_authorization(
            publish,
            {
                **stored_clip,
                "distribution_release": {**authorization, "scope": "all"},
            },
            clean,
            destinations=["x"],
        )

    def test_destination_pair_acknowledges_only_exact_legacy_history(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        publish = {
            "shorts": [
                {
                    "clip_id": "clip_01",
                    "status": "failed",
                    "error": "Provider returned an unreadable response",
                }
            ]
        }
        (episode_dir / "publish.json").write_text(json.dumps(publish))

        from agents.publish import short_receipt_history_revision
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._destination_state(candidate),
        )
        history_revision = short_receipt_history_revision(publish, "clip_01")
        body = {
            "request_id": "53802918-5a69-4af1-831e-b83395f9c95a",
            "actor": "release-operator",
            "reason": "Replace exact legacy destination publication",
            "targets": [
                {
                    "variant_id": "gameplay_surround_v1",
                    "expected_revision": "sha256:" + "1" * 64,
                    "destinations": [
                        "facebook",
                        "instagram",
                        "tiktok",
                        "youtube",
                    ],
                },
                {
                    "variant_id": "speaker_panels_v1",
                    "expected_revision": "sha256:" + "3" * 64,
                    "destinations": ["x"],
                },
            ],
        }

        omitted = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets", json=body
        )
        wrong = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets",
            json={
                **body,
                "acknowledge_unresolved_history_revision": "sha256:" + "0" * 64,
            },
        )
        prepared = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release-targets",
            json={
                **body,
                "acknowledge_unresolved_history_revision": history_revision,
            },
        )

        assert omitted.status_code == 409
        assert wrong.status_code == 409
        assert prepared.status_code == 200
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        acknowledgement = stored["distribution_release"][
            "unresolved_history_acknowledgement"
        ]
        assert acknowledgement["receipt_history_revision"] == history_revision
        assert len(acknowledgement["obligations"]) == 1
        assert acknowledgement["obligations"][0]["artifact_identity"] == "unknown"

    @pytest.mark.parametrize("variant_id", ACTIVE_VARIANT_IDS)
    def test_acknowledges_exact_legacy_history_for_variant_rerelease(
        self, test_client, monkeypatch, variant_id
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        publish = {
            "shorts": [
                {
                    "clip_id": "clip_01",
                    "status": "failed",
                    "error": "Expecting value: line 1 column 1 (char 0)",
                }
            ]
        }
        publish_path = episode_dir / "publish.json"
        publish_path.write_text(json.dumps(publish))
        publish_before = publish_path.read_bytes()

        from agents.publish import (
            short_receipt_history_revision,
            valid_rerelease_authorization,
        )
        from lib.short_variants import distribution_release_revision
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        history_revision = short_receipt_history_revision(publish, "clip_01")
        lock = clips_mod.publication_change_lock(episode_dir, "clip_01")
        obligations = lock["unresolved_receipt_obligations"]
        assert lock["unresolved_history_acknowledgement_allowed"] is True
        assert lock["re_release_history_revision"] == history_revision
        assert len(obligations) == 1
        assert obligations[0] == {
            "receipt_revision": obligations[0]["receipt_revision"],
            "status": "failed",
            "error": "Expecting value: line 1 column 1 (char 0)",
            "platforms": None,
            "request_id": None,
            "job_id": None,
            "external_id": None,
            "version": None,
            "variant_id": None,
            "render_fingerprint": None,
            "approval_revision": None,
            "artifact_identity": "unknown",
        }
        body = {
            "variant_id": variant_id,
            "expected_revision": "sha256:selected-review",
            "request_id": "793321a8-a45d-41d5-99b6-b4310bd6de90",
            "actor": "release-operator",
            "reason": "Release the approved rebuilt Motion clip",
            "acknowledge_unresolved_history_revision": history_revision,
        }

        wrong_history = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                **body,
                "acknowledge_unresolved_history_revision": "sha256:" + "0" * 64,
            },
        )
        base = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={**body, "variant_id": None},
        )
        prepared = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release", json=body
        )
        stored_after_first = (episode_dir / "clips.json").read_bytes()
        repeated = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release", json=body
        )
        omitted_ack = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                key: value
                for key, value in body.items()
                if key != "acknowledge_unresolved_history_revision"
            },
        )

        assert wrong_history.status_code == 409
        assert "history changed" in wrong_history.json()["detail"]
        assert base.status_code == 409
        assert "current approved short-variant replacement" in base.json()["detail"]
        assert prepared.status_code == 200
        assert prepared.json()["status"] == "prepared"
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "already_prepared"
        assert omitted_ack.status_code == 409
        assert "already bound to different inputs" in omitted_ack.json()["detail"]
        assert (episode_dir / "clips.json").read_bytes() == stored_after_first
        assert publish_path.read_bytes() == publish_before

        stored_clip = json.loads(stored_after_first)["clips"][0]
        authorization = stored_clip["distribution_release"]
        acknowledgement = authorization["unresolved_history_acknowledgement"]
        assert acknowledgement == {
            "receipt_history_revision": history_revision,
            "obligations": obligations,
        }
        version = {
            "variant_id": variant_id,
            "revision": "sha256:selected-review",
            "render_fingerprint": "sha256:selected-render",
        }
        assert valid_rerelease_authorization(publish, stored_clip, version) is True
        unacknowledged = dict(authorization)
        unacknowledged.pop("unresolved_history_acknowledgement")
        unacknowledged["revision"] = distribution_release_revision(
            request_id=unacknowledged["request_id"],
            actor=unacknowledged["actor"],
            reason=unacknowledged["reason"],
            variant_id=unacknowledged["variant_id"],
            target_revision=unacknowledged["target_revision"],
            render_fingerprint=unacknowledged["render_fingerprint"],
            receipt_history_revision=unacknowledged["receipt_history_revision"],
        )
        assert (
            valid_rerelease_authorization(
                publish,
                {**stored_clip, "distribution_release": unacknowledged},
                version,
            )
            is False
        )

        tampered_document = json.loads(stored_after_first)
        tampered_clip = tampered_document["clips"][0]
        tampered_clip.pop("distribution_variant_id")
        tampered = tampered_clip["distribution_release"]
        tampered["variant_id"] = None
        tampered["revision"] = distribution_release_revision(
            request_id=tampered["request_id"],
            actor=tampered["actor"],
            reason=tampered["reason"],
            variant_id=None,
            target_revision=tampered["target_revision"],
            render_fingerprint=tampered["render_fingerprint"],
            receipt_history_revision=tampered["receipt_history_revision"],
            unresolved_history_acknowledgement=acknowledgement,
        )
        (episode_dir / "clips.json").write_text(json.dumps(tampered_document))
        tampered_base_retry = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={**body, "variant_id": None},
        )
        assert tampered_base_retry.status_code == 409
        assert "identity cannot be verified" in tampered_base_retry.json()["detail"]

        publish["shorts"][0]["status_history"] = [{"status": "failed"}]
        assert valid_rerelease_authorization(publish, stored_clip, version) is False

    @pytest.mark.parametrize(
        "bound_field",
        [
            "version",
            "variant_id",
            "render_fingerprint",
            "approval_revision",
            "release_revision",
            "target_revision",
            "copy_revision",
            "schema",
            "destination_actor",
            "rerelease_actor",
            "parent_receipt_history_revision",
            "unresolved_history_acknowledgement",
            "artifact_sha256",
        ],
    )
    def test_legacy_ack_rejects_any_bound_receipt_field(
        self, test_client, monkeypatch, bound_field
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        receipt = {"clip_id": "clip_01", "status": "failed", bound_field: None}
        publish = {"shorts": [receipt]}
        (episode_dir / "publish.json").write_text(json.dumps(publish))

        from agents.publish import short_receipt_history_revision
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )
        lock = clips_mod.publication_change_lock(episode_dir, "clip_01")
        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                "variant_id": "gameplay_surround_v1",
                "expected_revision": "sha256:selected-review",
                "request_id": "8331d825-b41b-4cdc-9274-5f58d5fd2719",
                "actor": "release-operator",
                "reason": "Release the approved rebuilt Motion clip",
                "acknowledge_unresolved_history_revision": (
                    short_receipt_history_revision(publish, "clip_01")
                ),
            },
        )

        assert lock["unresolved_history_acknowledgement_allowed"] is False
        assert response.status_code == 409
        assert "Only pre-schema unresolved history" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("current", "approval_current"), [(False, True), (True, False)]
    )
    def test_legacy_ack_requires_current_approved_motion(
        self, test_client, monkeypatch, current, approval_current
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        publish = {"shorts": [{"clip_id": "clip_01", "status": "failed"}]}
        (episode_dir / "publish.json").write_text(json.dumps(publish))

        from agents.publish import short_receipt_history_revision
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(
                candidate, current=current, approval_current=approval_current
            ),
        )
        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                "variant_id": "gameplay_surround_v1",
                "expected_revision": "sha256:selected-review",
                "request_id": "fd3c4097-e10f-48ee-bb5e-3f27679242f2",
                "actor": "release-operator",
                "reason": "Release the approved rebuilt Motion clip",
                "acknowledge_unresolved_history_revision": (
                    short_receipt_history_revision(publish, "clip_01")
                ),
            },
        )

        assert response.status_code == 409
        assert "current and separately approved" in response.json()["detail"]

    @pytest.mark.parametrize("status", ["submitted", "unknown", "failed"])
    def test_rerelease_rejects_unresolved_remote_receipt(
        self, test_client, monkeypatch, status
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [dict(SAMPLE_CLIPS[0], status="approved")])
        clips_before = (episode_dir / "clips.json").read_bytes()
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "shorts": [
                        {
                            "clip_id": "clip_01",
                            "status": status,
                            "request_id": "unresolved-request",
                            "platforms": ["youtube", "instagram"],
                        }
                    ]
                }
            )
        )
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )

        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                "variant_id": None,
                "expected_revision": "sha256:selected-review",
                "request_id": "793321a8-a45d-41d5-99b6-b4310bd6de90",
                "actor": "release-operator",
                "reason": "Rebuilt episode with current media",
            },
        )

        assert response.status_code == 409
        assert "unresolved remote destinations" in response.json()["detail"]
        assert (episode_dir / "clips.json").read_bytes() == clips_before

    def test_rerelease_rejects_malformed_existing_identity(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            distribution_release={"request_id": "incomplete"},
        )
        _add_clips(episodes_dir, "ep_001", [clip])
        (episode_dir / "publish.json").write_text(
            json.dumps(
                {
                    "shorts": [
                        {
                            "clip_id": "clip_01",
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
                                        "profile_username": "up",
                                    }
                                ],
                            },
                        }
                    ]
                }
            )
        )
        clips_before = (episode_dir / "clips.json").read_bytes()
        from server.routes import clips as clips_mod

        monkeypatch.setattr(
            clips_mod,
            "_distribution_state",
            lambda _episode_dir, candidate: self._state(candidate),
        )

        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/re-release",
            json={
                "variant_id": None,
                "expected_revision": "sha256:selected-review",
                "request_id": "4530a444-c85a-49d7-af59-5a237fb4c22d",
                "actor": "release-operator",
                "reason": "Rebuilt episode with current media",
            },
        )

        assert response.status_code == 409
        assert "identity cannot be verified" in response.json()["detail"]
        assert (episode_dir / "clips.json").read_bytes() == clips_before


class TestManualClip:
    def test_add_manual_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)
        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={
                "start_seconds": 500.0,
                "end_seconds": 560.0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["manual"] is True

    def test_invalid_duration(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={
                "start_seconds": 100.0,
                "end_seconds": 50.0,  # end < start
            },
        )
        assert resp.status_code == 400

    def test_rejects_negative_start_without_creating_clip(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")

        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={"start_seconds": -1, "end_seconds": 30},
        )

        assert resp.status_code == 422
        assert not (episode_dir / "clips.json").exists()

    def test_rejects_end_beyond_probed_source_duration(self, test_client, monkeypatch):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "source_merged.mp4").write_bytes(b"probe fixture")

        import server.routes.clips as clips_mod

        monkeypatch.setattr(clips_mod, "get_duration", lambda _path: 100.0)
        resp = client.post(
            "/api/episodes/ep_001/clips/manual",
            json={"start_seconds": 50, "end_seconds": 101},
        )

        assert resp.status_code == 422
        assert resp.json()["detail"]["source_duration_seconds"] == 100.0
        assert not (episode_dir / "clips.json").exists()

    def test_rejects_nonfinite_json_without_server_error(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")

        for literal in ("NaN", "1e999", "-1e999"):
            resp = client.post(
                "/api/episodes/ep_001/clips/manual",
                content=f'{{"start_seconds":{literal},"end_seconds":30}}',
                headers={"content-type": "application/json"},
            )
            assert resp.status_code == 422
        assert not (episode_dir / "clips.json").exists()


class TestClipMutation:
    def test_typed_patch_keeps_clocks_in_sync_and_invalidates_approval(
        self, test_client
    ):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            approved_revision="sha256:old-copy",
            approved_render_fingerprint="sha256:old-render",
            metadata={"tiktok": {"caption": "keep", "hashtags": ["#old"]}},
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        resp = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            json={
                "title": "New title",
                "hook_text": "New hook",
                "compelling_reason": "It resolves the premise",
                "virality_score": 9.5,
                "speaker": "Speaker 1",
                "hashtags": ["#new"],
                "start_seconds": 65,
                "end_seconds": 125,
                "metadata": {"tiktok": {"caption": "updated"}},
            },
        )

        assert resp.status_code == 200
        updated = resp.json()
        assert updated["start"] == updated["start_seconds"] == 65
        assert updated["end"] == updated["end_seconds"] == 125
        assert updated["duration"] == 60
        assert updated["metadata"]["tiktok"] == {
            "caption": "updated",
            "hashtags": ["#old"],
        }
        assert updated["status"] == "pending"
        assert updated["selection_status"] == "selected"
        assert "approved_revision" not in updated
        assert "approved_render_fingerprint" not in updated

    def test_time_patch_rejects_nonfinite_and_out_of_source_bounds(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            start=10,
            start_seconds=10,
            end=50,
            end_seconds=50,
            duration=40,
        )
        _add_clips(episodes_dir, "ep_001", [clip])
        (episode_dir / "source_merged.mp4").write_bytes(b"probe fixture")

        import server.routes.clips as clips_mod

        monkeypatch.setattr(clips_mod, "get_duration", lambda _path: 100.0)
        nonfinite = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            content='{"start_seconds":NaN}',
            headers={"content-type": "application/json"},
        )
        past_end = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            json={"end_seconds": 101},
        )

        assert nonfinite.status_code == 422
        assert past_end.status_code == 422
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert stored["start_seconds"] == 10
        assert stored["end_seconds"] == 50

    def test_patch_rejects_nonfinite_score_without_persisting(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        resp = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            content='{"virality_score":1e999}',
            headers={"content-type": "application/json"},
        )

        assert resp.status_code == 422
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert stored["virality_score"] == 8

    def test_patch_rejects_nested_nonfinite_metadata(self, test_client):
        client, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        resp = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            content='{"metadata":{"tiktok":{"score":NaN}}}',
            headers={"content-type": "application/json"},
        )

        assert resp.status_code == 422
        stored = json.loads((episode_dir / "clips.json").read_text())["clips"][0]
        assert "metadata" not in stored

    def test_delete_removes_only_requested_clip(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", SAMPLE_CLIPS)

        resp = client.delete("/api/episodes/ep_001/clips/clip_01")

        assert resp.status_code == 200
        stored = json.loads((episodes_dir / "ep_001" / "clips.json").read_text())[
            "clips"
        ]
        assert [clip["id"] for clip in stored] == ["clip_02"]

    def test_render_demotes_approval_when_fingerprint_changes(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            approved_revision="sha256:review",
            approved_render_fingerprint="sha256:old",
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "render_single_clip",
            lambda episode_dir, _config, clip_id: {
                "clip_id": clip_id,
                "output_path": str(episode_dir / "shorts" / f"{clip_id}.mp4"),
                "caption_path": str(episode_dir / "subtitles" / f"{clip_id}.ass"),
                "reused": False,
                "render": {"fingerprint": "sha256:new"},
            },
            raising=False,
        )
        resp = client.post("/api/episodes/ep_001/clips/clip_01/render")

        assert resp.status_code == 200
        assert resp.json()["render"]["fingerprint"] == "sha256:new"
        stored = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert stored["status"] == "pending"
        assert "approved_revision" not in stored
        job = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]["clip_01"]
        assert job["status"] == "succeeded"
        assert job["render_fingerprint"] == "sha256:new"

    def test_render_rejects_duplicate_active_job(self, test_client):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        import server.routes.clips as clips_mod

        key = clips_mod._render_job_key(ep_dir, "clip_01")
        with clips_mod._render_jobs_lock:
            clips_mod._active_render_jobs.add(key)
        try:
            response = client.post("/api/episodes/ep_001/clips/clip_01/render")
        finally:
            with clips_mod._render_jobs_lock:
                clips_mod._active_render_jobs.discard(key)

        assert response.status_code == 409
        assert response.json()["detail"] == "Clip clip_01 is already rendering"

    def test_audio_repair_uses_public_adapter_and_demotes_prior_approval(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            approved_revision="sha256:review",
            approved_render_fingerprint="sha256:current",
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "repair_single_clip_audio",
            lambda episode_dir, _config, clip_id: {
                "clip_id": clip_id,
                "output_path": str(episode_dir / "shorts" / f"{clip_id}.mp4"),
                "reused": True,
                "audio_repaired": True,
                "video_reencoded": False,
                "render": {"fingerprint": "sha256:current"},
            },
            raising=False,
        )

        response = client.post("/api/episodes/ep_001/clips/clip_01/repair-audio")

        assert response.status_code == 200
        assert response.json()["audio_repaired"] is True
        assert response.json()["video_reencoded"] is False
        stored = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert stored["status"] == "pending"
        assert stored["selection_status"] == "selected"
        assert "approved_revision" not in stored
        job = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]["clip_01"]
        assert job["audio_repaired"] is True
        assert job["video_reencoded"] is False

    def test_variant_render_uses_qualified_job_and_preserves_base_approval(
        self, test_client, monkeypatch
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            selection_status="selected",
            approved_revision="sha256:base-review",
            approved_render_fingerprint="sha256:base-render",
        )
        _add_clips(episodes_dir, "ep_001", [clip])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "render_single_clip_variant",
            lambda episode_dir, _config, clip_id, variant_id, asset_id: {
                "clip_id": clip_id,
                "variant_id": variant_id,
                "asset_id": asset_id,
                "output_path": str(
                    episode_dir / "short_variants" / variant_id / f"{clip_id}.mp4"
                ),
                "reused": False,
                "render": {"fingerprint": "sha256:variant"},
            },
        )

        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/variants/speaker_panels_v1/render",
            json={},
        )

        assert response.status_code == 200
        assert response.json()["asset_id"] is None
        assert json.loads((ep_dir / "clips.json").read_text())["clips"][0] == clip
        job = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]["clip_01@speaker_panels_v1"]
        assert job["status"] == "succeeded"

    @pytest.mark.parametrize(
        ("variant_id", "asset_id"),
        (
            ("gameplay_surround_v1", "gameplay_surround_assets_v1"),
            ("speaker_panels_v1", None),
        ),
    )
    def test_variant_render_uses_bound_asset_and_job_identity(
        self, test_client, monkeypatch, variant_id, asset_id
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        import agents.shorts_render as render_mod

        monkeypatch.setattr(
            render_mod,
            "render_single_clip_variant",
            lambda episode_dir, _config, clip_id, requested_variant, requested_asset: {
                "clip_id": clip_id,
                "variant_id": requested_variant,
                "asset_id": requested_asset,
                "output_path": str(
                    episode_dir
                    / "short_variants"
                    / requested_variant
                    / f"{clip_id}.mp4"
                ),
                "reused": False,
                "render": {"fingerprint": "sha256:variant"},
            },
        )

        response = client.post(
            f"/api/episodes/ep_001/clips/clip_01/variants/{variant_id}/render",
            json={},
        )

        assert response.status_code == 200
        assert response.json()["variant_id"] == variant_id
        assert response.json()["asset_id"] == asset_id
        jobs = json.loads((ep_dir / "work" / "clip_render_jobs.json").read_text())[
            "jobs"
        ]
        assert jobs[f"clip_01@{variant_id}"]["status"] == "succeeded"

    @pytest.mark.parametrize("variant_id", RETIRED_VARIANT_IDS)
    def test_retired_variant_write_routes_fail_before_side_effects(
        self, test_client, monkeypatch, variant_id
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])
        clips_path = ep_dir / "clips.json"
        clips_before = clips_path.read_bytes()

        import agents.shorts_render as render_mod

        render_calls = []

        def unexpected_render(*args, **kwargs):
            render_calls.append((args, kwargs))
            raise AssertionError("retired variant reached the render adapter")

        monkeypatch.setattr(
            render_mod,
            "render_single_clip_variant",
            unexpected_render,
        )
        responses = (
            client.post(
                f"/api/episodes/ep_001/clips/clip_01/variants/{variant_id}/render",
                json={},
            ),
            client.post(
                f"/api/episodes/ep_001/clips/clip_01/variants/{variant_id}/approve",
                json={"expected_revision": "sha256:retired-review"},
            ),
            client.put(
                "/api/episodes/ep_001/clips/clip_01/distribution",
                json={
                    "variant_id": variant_id,
                    "expected_revision": "sha256:retired-review",
                },
            ),
            client.post(
                "/api/episodes/ep_001/clips/clip_01/re-release",
                json={
                    "variant_id": variant_id,
                    "expected_revision": "sha256:retired-review",
                    "request_id": "793321a8-a45d-41d5-99b6-b4310bd6de90",
                    "actor": "release-operator",
                    "reason": "Attempt a retired short variant write",
                },
            ),
        )

        for response in responses:
            assert response.status_code == 409
            assert response.json()["detail"] == {
                "code": "short_variant_retired",
                "variant_id": variant_id,
                "message": (
                    f"Short variant {variant_id} is retired; existing media and "
                    "history only"
                ),
            }
        assert render_calls == []
        assert clips_path.read_bytes() == clips_before
        assert not (ep_dir / "work" / "clip_render_jobs.json").exists()

    def test_variant_routes_reject_unknown_id(self, test_client):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        response = client.post(
            "/api/episodes/ep_001/clips/clip_01/variants/satisfying_background_v1/render",
            json={},
        )

        assert response.status_code == 404
        assert "Unknown short variant" in response.json()["detail"]

    @pytest.mark.parametrize("variant_id", ACTIVE_VARIANT_IDS)
    def test_variant_approval_restores_candidate_without_approving_base(
        self, test_client, monkeypatch, variant_id
    ):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        clip = dict(
            SAMPLE_CLIPS[0],
            status="approved",
            selection_status="selected",
            approved_revision="sha256:base-copy",
            approved_render_fingerprint="sha256:base-render",
            distribution_variant_id=variant_id,
        )
        _add_clips(episodes_dir, "ep_001", [clip, SAMPLE_CLIPS[1]])
        updated = client.patch(
            "/api/episodes/ep_001/clips/clip_01/metadata",
            json={
                "metadata": {
                    "facebook": {
                        "title": "New destination title",
                        "description": "New destination description",
                    }
                }
            },
        )
        assert updated.status_code == 200
        changed = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert changed["status"] == "pending"
        assert "approved_revision" not in changed
        assert "approved_render_fingerprint" not in changed
        record = {
            "fingerprint": "sha256:variant",
            "output": {
                "content_revision": "sha256:pixels",
                "scan_identity": {"inode": 1},
            },
        }

        from agents.qa import clip_review_revision
        from server.routes import clips as clips_mod

        revision = clip_review_revision(changed, record, None)
        monkeypatch.setattr(clips_mod, "_current_render", lambda *_args: {})
        monkeypatch.setattr(
            clips_mod,
            "background_variant_state",
            lambda *_args, **_kwargs: (
                record,
                {"current": True, "playable": True},
            ),
        )
        saved = []

        def save_with_concurrent_edit(*args):
            saved.append(args)
            document = json.loads((ep_dir / "clips.json").read_text())
            document["clips"][0]["metadata"]["facebook"]["title"] = (
                "Concurrent destination title"
            )
            _add_clips(episodes_dir, "ep_001", document["clips"])
            return record

        monkeypatch.setattr(
            clips_mod,
            "save_background_variant_approval",
            save_with_concurrent_edit,
        )

        stale = client.post(
            f"/api/episodes/ep_001/clips/clip_01/variants/{variant_id}/approve",
            json={"expected_revision": "sha256:stale"},
        )
        conflicted = client.post(
            f"/api/episodes/ep_001/clips/clip_01/variants/{variant_id}/approve",
            json={"expected_revision": revision},
        )

        assert stale.status_code == 409
        assert conflicted.status_code == 409
        assert "copy changed" in conflicted.json()["detail"]
        concurrent = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert concurrent["metadata"]["facebook"]["title"] == (
            "Concurrent destination title"
        )
        assert concurrent["status"] == "pending"
        revision = clip_review_revision(concurrent, record, None)

        def save_with_unrelated_edit(*args):
            saved.append(args)
            document = json.loads((ep_dir / "clips.json").read_text())
            document["clips"][1]["title"] = "Concurrent unrelated title"
            _add_clips(episodes_dir, "ep_001", document["clips"])
            return record

        monkeypatch.setattr(
            clips_mod,
            "save_background_variant_approval",
            save_with_unrelated_edit,
        )
        approved = client.post(
            f"/api/episodes/ep_001/clips/clip_01/variants/{variant_id}/approve",
            json={"expected_revision": revision},
        )

        assert approved.status_code == 200
        assert approved.json()["approved_revision"] == revision
        assert len(saved) == 2
        stored = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert stored["status"] == "approved"
        assert stored["selection_status"] == "selected"
        assert stored["distribution_variant_id"] == variant_id
        assert stored["metadata"] == concurrent["metadata"]
        assert "approved_revision" not in stored
        assert "approved_render_fingerprint" not in stored
        assert saved[-1][-1] == variant_id
        other = json.loads((ep_dir / "clips.json").read_text())["clips"][1]
        assert other["title"] == "Concurrent unrelated title"

    def test_current_render_requires_provenance_current_speaker_plan(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        audio = ep_dir / "work" / "audio_mix.wav"
        audio.parent.mkdir(exist_ok=True)
        audio.write_bytes(b"audio")

        from agents import speaker_cut
        from lib import audio_mix, delivery_video
        from server.routes import clips as clips_mod

        monkeypatch.setattr(speaker_cut, "current_speaker_segments", lambda *_: None)
        monkeypatch.setattr(audio_mix, "selected_audio_source", lambda *_: audio)
        called = []
        monkeypatch.setattr(
            delivery_video,
            "current_short_render",
            lambda *_: called.append(True) or {"fingerprint": "unexpected"},
        )

        assert clips_mod._current_render(ep_dir, SAMPLE_CLIPS[0]) is None
        assert called == []

    async def test_cancelled_request_keeps_clip_locked_until_worker_finishes(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])
        started = threading.Event()
        release = threading.Event()

        import agents.shorts_render as render_mod
        from server.routes import clips as clips_mod

        def render(*_args):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test worker timed out")
            return {
                "reused": False,
                "render": {"fingerprint": "sha256:variant"},
            }

        monkeypatch.setattr(render_mod, "render_single_clip_variant", render)
        request = asyncio.create_task(
            clips_mod._run_clip_render_operation(
                "ep_001",
                "clip_01",
                variant_id="gameplay_surround_v1",
                asset_id="gameplay_surround_assets_v1",
            )
        )
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request

            with pytest.raises(HTTPException) as blocked:
                await clips_mod._run_clip_render_operation(
                    "ep_001", "clip_01", variant_id="gameplay_surround_v1"
                )
            assert getattr(blocked.value, "status_code", None) == 409
            assert (
                clips_mod.render_job_state(ep_dir, "clip_01@gameplay_surround_v1")[
                    "status"
                ]
                == "rendering"
            )
        finally:
            release.set()

        for _ in range(100):
            if not clips_mod._active_render_jobs:
                break
            await asyncio.sleep(0.01)
        assert not clips_mod._active_render_jobs
        assert (
            clips_mod.render_job_state(ep_dir, "clip_01@gameplay_surround_v1")["status"]
            == "succeeded"
        )

    async def test_render_job_setup_failure_releases_clip_lock(
        self, test_client, monkeypatch
    ):
        _client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        _add_clips(episodes_dir, "ep_001", [SAMPLE_CLIPS[0]])

        from server.routes import clips as clips_mod

        def fail_write(*_args):
            raise OSError("disk unavailable")

        monkeypatch.setattr(clips_mod, "_write_render_job", fail_write)

        with pytest.raises(HTTPException, match="disk unavailable") as failed:
            await clips_mod._run_clip_render_operation("ep_001", "clip_01")
        assert failed.value.status_code == 500
        assert not clips_mod._active_render_jobs
