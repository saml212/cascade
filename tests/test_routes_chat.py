"""Tests for the chat transport and its canonical API delegation."""

# ruff: noqa: F811 - imported pytest fixtures are intentionally shadowed by arguments

import asyncio
import json
from types import SimpleNamespace

from tests.test_routes_episodes import _create_episode, test_client  # noqa: F401


class TestChatTransport:
    def test_claude_cli_is_text_only_and_ignores_workspace_customizations(
        self, monkeypatch
    ):
        import server.routes.chat as chat_mod

        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=0, stdout='{"result":"Read-only answer"}', stderr=""
            )

        monkeypatch.setattr(chat_mod.subprocess, "run", run)

        assert chat_mod._call_claude("Canonical context", [{"content": "Status?"}]) == (
            "Read-only answer"
        )
        command, kwargs = calls[0]
        assert "--system-prompt" in command
        assert "--append-system-prompt" not in command
        assert {
            "--safe-mode",
            "--no-session-persistence",
            "--no-chrome",
            "--strict-mcp-config",
            "--disable-slash-commands",
        }.issubset(command)
        assert command[-2:] == ["--tools", ""]
        assert kwargs["input"] == "<user>\nStatus?\n</user>"

        calls.clear()
        chat_mod._call_claude("", [{"content": "Find trim bounds"}])
        fallback_command = calls[0][0]
        assert fallback_command[fallback_command.index("--system-prompt") + 1] == (
            chat_mod._TEXT_ONLY_PROMPT
        )


class TestChatContext:
    def test_context_separates_current_release_state_from_legacy_evidence(
        self, test_client, monkeypatch
    ):
        _, episodes_dir = test_client
        episode_dir = _create_episode(
            episodes_dir,
            "ep_001",
            {"title": "Current title", "description": "Current description"},
        )
        (episode_dir / "metadata" / "metadata.json").write_text(
            json.dumps(
                {
                    "longform": {"title": "Legacy title"},
                    "integrated_lufs": -14.0,
                }
            )
        )
        (episode_dir / "delivery.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "integrated_lufs": -16.1,
                    "true_peak_dbfs": -1.2,
                }
            )
        )

        import server.routes.chat as chat_mod

        monkeypatch.setattr(
            chat_mod,
            "quality_snapshot",
            lambda *_args, **_kwargs: {
                "quality": {"status": "passed"},
                "release_gate": {"status": "awaiting_publish_approval"},
            },
        )
        monkeypatch.setattr(
            chat_mod.episodes_api,
            "_delivery_snapshot",
            lambda _episode_dir: {"status": "ready"},
        )

        context = chat_mod._load_episode_context(episode_dir)

        assert context["release_metadata"]["longform"]["title"] == "Current title"
        assert context["verified_delivery"]["audio_measurements_current"] is True
        assert context["verified_delivery"]["audio_measurements"] == {
            "integrated_lufs": -16.1,
            "true_peak_dbfs": -1.2,
        }
        assert context["legacy_metadata_evidence"]["status"] == (
            "historical_unverified"
        )
        prompt = chat_mod._build_system_prompt(context)
        assert "never describe a legacy value as a current measurement" in prompt
        assert "<quality_snapshot>" in prompt
        assert "<verified_delivery>" in prompt

    def test_unverified_delivery_hides_cached_audio_measurements(
        self, test_client, monkeypatch
    ):
        _, episodes_dir = test_client
        episode_dir = _create_episode(episodes_dir, "ep_001")
        (episode_dir / "delivery.json").write_text(
            json.dumps({"status": "ready", "integrated_lufs": -14.0})
        )

        import server.routes.chat as chat_mod

        monkeypatch.setattr(
            chat_mod.episodes_api,
            "_delivery_snapshot",
            lambda _episode_dir: {"status": "not_prepared"},
        )

        delivery = chat_mod._verified_delivery_context(episode_dir)

        assert delivery["audio_measurements_current"] is False
        assert delivery["audio_measurements"] == {}


class TestChatEndpoint:
    def test_episode_not_found(self, test_client):
        # Chat route now talks to the claude CLI, not the paid Anthropic API,
        # so it no longer requires ANTHROPIC_API_KEY. Missing episode should
        # return 404 cleanly.
        client, _ = test_client
        resp = client.post("/api/episodes/nonexistent/chat", json={"message": "Hello"})
        assert resp.status_code == 404

    def test_model_action_uses_canonical_episode_patch(self, test_client, monkeypatch):
        client, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")

        import server.routes.chat as chat_mod

        async def assistant(*_args, **_kwargs):
            return """Updated the guest.
```action
{"action":"update_episode_info","guest_name":"Ada","guest_title":"Engineer"}
```"""

        monkeypatch.setattr(chat_mod, "_assistant_turn", assistant)
        resp = client.post(
            "/api/episodes/ep_001/chat", json={"message": "Set the guest"}
        )

        assert resp.status_code == 200
        assert resp.json()["response"] == "Updated the guest."
        assert resp.json()["actions_taken"][0]["status"] == "ok"
        stored = json.loads((ep_dir / "episode.json").read_text())
        assert (stored["guest_name"], stored["guest_title"]) == ("Ada", "Engineer")


class TestCanonicalActionEffects:
    @staticmethod
    def _write_clips(ep_dir, clips):
        (ep_dir / "clips.json").write_text(json.dumps({"clips": clips}))

    def test_chat_clip_patch_matches_rest_patch(self, test_client):
        client, episodes_dir = test_client
        chat_dir = _create_episode(episodes_dir, "ep_chat")
        rest_dir = _create_episode(episodes_dir, "ep_rest")
        clip = {
            "id": "clip_01",
            "start": 10,
            "end": 70,
            "start_seconds": 10,
            "end_seconds": 70,
            "duration": 60,
            "title": "Old",
            "hook_text": "Old hook",
            "compelling_reason": "Old reason",
            "virality_score": 3,
            "speaker": "L",
            "status": "pending",
        }
        self._write_clips(chat_dir, [clip])
        self._write_clips(rest_dir, [clip])

        import server.routes.chat as chat_mod

        action = {
            "action": "update_clip_metadata",
            "clip_id": "clip_01",
            "title": "New",
            "hook_text": "Open here",
            "compelling_reason": "Complete payoff",
            "hashtags": ["#podcast"],
            "virality_score": 8.5,
            "speaker": "Speaker 0",
        }
        result = asyncio.run(chat_mod._execute_action(action, chat_dir))
        rest = client.patch(
            "/api/episodes/ep_rest/clips/clip_01/metadata",
            json={
                key: value
                for key, value in action.items()
                if key not in {"action", "clip_id"}
            },
        )

        assert result["status"] == "ok"
        assert rest.status_code == 200
        chat_clip = json.loads((chat_dir / "clips.json").read_text())["clips"][0]
        rest_clip = json.loads((rest_dir / "clips.json").read_text())["clips"][0]
        assert chat_clip == rest_clip

    def test_chat_platform_patch_matches_rest_deep_merge(self, test_client):
        client, episodes_dir = test_client
        chat_dir = _create_episode(episodes_dir, "ep_chat")
        rest_dir = _create_episode(episodes_dir, "ep_rest")
        clip = {
            "id": "clip_01",
            "start": 10,
            "end": 70,
            "status": "pending",
            "metadata": {"tiktok": {"caption": "old", "hashtags": ["#keep"]}},
        }
        self._write_clips(chat_dir, [clip])
        self._write_clips(rest_dir, [clip])

        import server.routes.chat as chat_mod

        result = asyncio.run(
            chat_mod._execute_action(
                {
                    "action": "update_platform_metadata",
                    "clip_id": "clip_01",
                    "platform": "tiktok",
                    "caption": "new",
                },
                chat_dir,
            )
        )
        rest = client.patch(
            "/api/episodes/ep_rest/clips/clip_01/metadata",
            json={"metadata": {"tiktok": {"caption": "new"}}},
        )

        assert result["status"] == "ok"
        assert rest.status_code == 200
        chat_clip = json.loads((chat_dir / "clips.json").read_text())["clips"][0]
        rest_clip = json.loads((rest_dir / "clips.json").read_text())["clips"][0]
        assert chat_clip == rest_clip
        assert chat_clip["metadata"]["tiktok"]["hashtags"] == ["#keep"]

    def test_chat_trim_matches_rest_and_replaces_previous_boundary(self, test_client):
        client, episodes_dir = test_client
        initial_edits = [
            {"type": "trim_start", "seconds": 5, "reason": "old"},
            {
                "type": "cut",
                "start_seconds": 100,
                "end_seconds": 110,
                "reason": "break",
            },
        ]
        chat_dir = _create_episode(
            episodes_dir, "ep_chat", {"longform_edits": initial_edits}
        )
        rest_dir = _create_episode(
            episodes_dir, "ep_rest", {"longform_edits": initial_edits}
        )

        import server.routes.chat as chat_mod

        chat_mod.edits_api.EPISODES_DIR = episodes_dir

        result = asyncio.run(
            chat_mod._execute_action(
                {
                    "action": "edit_longform",
                    "type": "trim_start",
                    "seconds": 20,
                    "reason": "conversation begins",
                },
                chat_dir,
            )
        )
        rest = client.post(
            "/api/episodes/ep_rest/edits",
            json={
                "type": "trim_start",
                "seconds": 20,
                "reason": "conversation begins",
            },
        )

        assert result["status"] == "ok"
        assert rest.status_code == 200
        chat_edits = json.loads((chat_dir / "episode.json").read_text())[
            "longform_edits"
        ]
        rest_edits = json.loads((rest_dir / "episode.json").read_text())[
            "longform_edits"
        ]
        comparable = lambda edits: [
            {key: value for key, value in edit.items() if key != "added_at"}
            for edit in edits
        ]
        assert comparable(chat_edits) == comparable(rest_edits)
        assert [edit["type"] for edit in chat_edits].count("trim_start") == 1
        assert any(edit["type"] == "cut" for edit in chat_edits)

    def test_add_clip_preserves_copy_and_uses_public_render(
        self, test_client, monkeypatch
    ):
        _, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")
        self._write_clips(ep_dir, [])

        import server.routes.chat as chat_mod

        calls = []

        async def render(episode_id, clip_id):
            calls.append((episode_id, clip_id))
            return {"clip_id": clip_id, "render": {"fingerprint": "sha256:new"}}

        monkeypatch.setattr(chat_mod.clips_api, "render_clip", render)
        result = asyncio.run(
            chat_mod._execute_action(
                {
                    "action": "add_clip",
                    "start_seconds": 30,
                    "end_seconds": 75,
                    "title": "A complete idea",
                    "hook_text": "Listen to this",
                    "compelling_reason": "Strong payoff",
                    "virality_score": 9,
                    "speaker": "Speaker 1",
                },
                ep_dir,
            )
        )

        assert result["status"] == "ok"
        assert calls == [("ep_001", "clip_01")]
        stored = json.loads((ep_dir / "clips.json").read_text())["clips"][0]
        assert stored["title"] == "A complete idea"
        assert stored["hook_text"] == "Listen to this"
        assert stored["start_seconds"] == 30
        assert stored["selection_status"] == "selected"

    def test_longform_render_action_delegates_to_apply_api(
        self, test_client, monkeypatch
    ):
        _, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")

        import server.routes.chat as chat_mod

        calls = []

        async def apply(episode_id):
            calls.append(episode_id)
            return {"episode_id": episode_id, "status": "started"}

        monkeypatch.setattr(chat_mod.edits_api, "apply_edits", apply)
        result = asyncio.run(
            chat_mod._execute_action({"action": "rerender_longform"}, ep_dir)
        )

        assert result["status"] == "ok"
        assert calls == ["ep_001"]

    def test_action_errors_are_machine_readable(self, test_client):
        _, episodes_dir = test_client
        ep_dir = _create_episode(episodes_dir, "ep_001")

        import server.routes.chat as chat_mod

        unknown = asyncio.run(chat_mod._execute_action({"action": "explode"}, ep_dir))
        missing = asyncio.run(
            chat_mod._execute_action({"action": "update_clip_times"}, ep_dir)
        )

        assert unknown == {
            "action": "explode",
            "status": "error",
            "detail": "Unknown action: explode",
        }
        assert missing["status"] == "error"
        assert "clip_id" in missing["detail"]


class TestParseActions:
    def test_parse_action_blocks(self):
        from server.routes.chat import _parse_actions

        text = """Here's what I'll do:

```action
{"action": "approve_clips", "clip_ids": ["clip_01"]}
```

Done!"""
        actions = _parse_actions(text)
        assert len(actions) == 1
        assert actions[0]["action"] == "approve_clips"

    def test_parse_multiple_actions(self):
        from server.routes.chat import _parse_actions

        text = """
```action
{"action": "approve_clips", "clip_ids": ["clip_01"]}
```

```action
{"action": "reject_clip", "clip_id": "clip_02"}
```
"""
        actions = _parse_actions(text)
        assert len(actions) == 2

    def test_parse_no_actions(self):
        from server.routes.chat import _parse_actions

        text = "Just a regular response with no actions."
        actions = _parse_actions(text)
        assert actions == []

    def test_strip_action_blocks(self):
        from server.routes.chat import _strip_action_blocks

        text = """I'll approve that.

```action
{"action": "approve_clips", "clip_ids": ["clip_01"]}
```

Done!"""
        result = _strip_action_blocks(text)
        assert "```action" not in result
        assert "Done!" in result
