"""Tests for chat API routes."""

import json
import pytest
from tests.test_routes_episodes import test_client, _create_episode


class TestChatEndpoint:
    def test_no_api_key(self, test_client, monkeypatch):
        client, episodes_dir = test_client
        _create_episode(episodes_dir, "ep_001")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        resp = client.post("/api/episodes/ep_001/chat", json={"message": "Hello"})
        assert resp.status_code == 500
        assert "ANTHROPIC_API_KEY" in resp.json()["detail"]

    def test_episode_not_found(self, test_client):
        client, _ = test_client
        resp = client.post("/api/episodes/nonexistent/chat", json={"message": "Hello"})
        assert resp.status_code == 404


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
