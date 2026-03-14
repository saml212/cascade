"""Tests for BaseAgent helpers."""

import json
import pytest
from pathlib import Path

from agents.base import BaseAgent


class ConcreteAgent(BaseAgent):
    """Minimal concrete agent for testing BaseAgent helpers."""
    name = "test_agent"

    def execute(self) -> dict:
        return {"ok": True}


class TestLoadJsonSafe:
    def test_returns_data_when_file_exists(self, tmp_episode_dir, sample_config):
        data = {"key": "value"}
        with open(tmp_episode_dir / "test.json", "w") as f:
            json.dump(data, f)

        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        result = agent.load_json_safe("test.json")
        assert result == {"key": "value"}

    def test_returns_empty_dict_on_missing_file(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        result = agent.load_json_safe("nonexistent.json")
        assert result == {}

    def test_returns_custom_default_on_missing_file(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        result = agent.load_json_safe("nonexistent.json", default={"fallback": True})
        assert result == {"fallback": True}

    def test_returns_empty_dict_on_invalid_json(self, tmp_episode_dir, sample_config):
        (tmp_episode_dir / "bad.json").write_text("not json{{{")
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        result = agent.load_json_safe("bad.json")
        assert result == {}

    def test_subdirectory_path(self, tmp_episode_dir, sample_config):
        sub = tmp_episode_dir / "metadata"
        sub.mkdir(exist_ok=True)
        with open(sub / "data.json", "w") as f:
            json.dump({"nested": True}, f)

        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        result = agent.load_json_safe("metadata/data.json")
        assert result == {"nested": True}


class TestGetConfig:
    def test_single_key(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        result = agent.get_config("processing")
        assert isinstance(result, dict)
        assert "video_crf" in result

    def test_nested_keys(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        assert agent.get_config("processing", "video_crf") == 18

    def test_missing_key_returns_default(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        assert agent.get_config("processing", "nonexistent", default=42) == 42

    def test_missing_section_returns_default(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        assert agent.get_config("nonexistent", "key", default="fallback") == "fallback"

    def test_deeply_nested(self, tmp_episode_dir):
        config = {"a": {"b": {"c": "deep"}}}
        agent = ConcreteAgent(tmp_episode_dir, config)
        assert agent.get_config("a", "b", "c") == "deep"

    def test_none_default(self, tmp_episode_dir, sample_config):
        agent = ConcreteAgent(tmp_episode_dir, sample_config)
        assert agent.get_config("nonexistent") is None
