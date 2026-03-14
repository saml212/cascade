"""Tests for the pipeline orchestrator — DAG resolution, failure handling, pause/resume."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agents.pipeline import (
    AGENT_DEPS,
    NON_CRITICAL_AGENTS,
    _slugify,
    _has_name_slug,
    _save_episode,
)
from agents import PIPELINE_ORDER, AGENT_REGISTRY


class TestDagDependencies:
    """Test that the dependency graph is correctly structured."""

    def test_all_pipeline_agents_have_deps_entry(self):
        """Every agent in PIPELINE_ORDER should have an entry in AGENT_DEPS."""
        for agent in PIPELINE_ORDER:
            assert agent in AGENT_DEPS, f"{agent} missing from AGENT_DEPS"

    def test_ingest_has_no_dependencies(self):
        assert AGENT_DEPS["ingest"] == set()

    def test_stitch_depends_on_ingest(self):
        assert AGENT_DEPS["stitch"] == {"ingest"}

    def test_audio_analysis_depends_on_stitch(self):
        assert AGENT_DEPS["audio_analysis"] == {"stitch"}

    def test_speaker_cut_depends_on_audio_analysis(self):
        assert AGENT_DEPS["speaker_cut"] == {"audio_analysis"}

    def test_transcribe_depends_on_stitch(self):
        assert AGENT_DEPS["transcribe"] == {"stitch"}

    def test_clip_miner_depends_on_transcribe_and_speaker_cut(self):
        assert AGENT_DEPS["clip_miner"] == {"transcribe", "speaker_cut"}

    def test_longform_render_depends_on_speaker_cut_and_transcribe(self):
        assert AGENT_DEPS["longform_render"] == {"speaker_cut", "transcribe"}

    def test_shorts_render_depends_on_clip_miner_and_speaker_cut(self):
        assert AGENT_DEPS["shorts_render"] == {"clip_miner", "speaker_cut"}

    def test_metadata_gen_depends_on_clip_miner(self):
        assert AGENT_DEPS["metadata_gen"] == {"clip_miner"}

    def test_thumbnail_gen_depends_on_transcribe(self):
        assert AGENT_DEPS["thumbnail_gen"] == {"transcribe"}

    def test_qa_depends_on_all_render_and_metadata(self):
        assert AGENT_DEPS["qa"] == {"longform_render", "shorts_render", "metadata_gen", "thumbnail_gen"}

    def test_publish_depends_on_qa(self):
        assert AGENT_DEPS["publish"] == {"qa"}

    def test_podcast_feed_depends_on_qa(self):
        assert AGENT_DEPS["podcast_feed"] == {"qa"}

    def test_backup_depends_on_publish_and_podcast_feed_and_thumbnail(self):
        assert AGENT_DEPS["backup"] == {"publish", "podcast_feed", "thumbnail_gen"}

    def test_no_circular_dependencies(self):
        """Verify the DAG has no cycles."""
        visited = set()
        stack = set()

        def visit(node):
            if node in stack:
                return True  # Cycle detected
            if node in visited:
                return False
            stack.add(node)
            for dep in AGENT_DEPS.get(node, set()):
                if visit(dep):
                    return True
            stack.remove(node)
            visited.add(node)
            return False

        for agent in AGENT_DEPS:
            if visit(agent):
                pytest.fail(f"Circular dependency detected involving {agent}")

    def test_all_deps_are_valid_agents(self):
        """All dependency references should be valid agent names."""
        all_agents = set(AGENT_DEPS.keys())
        for agent, deps in AGENT_DEPS.items():
            for dep in deps:
                assert dep in all_agents, f"{agent} depends on unknown agent {dep}"

    def test_parallel_agents_have_independent_deps(self):
        """transcribe and audio_analysis can run in parallel (both depend on stitch)."""
        assert "audio_analysis" not in AGENT_DEPS["transcribe"]
        assert "transcribe" not in AGENT_DEPS["audio_analysis"]


class TestNonCriticalAgents:
    def test_non_critical_agents_defined(self):
        assert "podcast_feed" in NON_CRITICAL_AGENTS
        assert "publish" in NON_CRITICAL_AGENTS
        assert "backup" in NON_CRITICAL_AGENTS
        assert "thumbnail_gen" in NON_CRITICAL_AGENTS

    def test_critical_agents_not_in_set(self):
        assert "ingest" not in NON_CRITICAL_AGENTS
        assert "stitch" not in NON_CRITICAL_AGENTS
        assert "clip_miner" not in NON_CRITICAL_AGENTS
        assert "longform_render" not in NON_CRITICAL_AGENTS
        assert "qa" not in NON_CRITICAL_AGENTS


class TestSlugify:
    def test_basic_name(self):
        assert _slugify("John Smith") == "john-smith"

    def test_special_characters(self):
        assert _slugify("O'Brien Jr.") == "o-brien-jr"

    def test_extra_spaces(self):
        assert _slugify("  Jane   Doe  ") == "jane-doe"

    def test_already_lowercase(self):
        assert _slugify("alice") == "alice"

    def test_numbers_preserved(self):
        assert _slugify("Agent 007") == "agent-007"

    def test_unicode_stripped(self):
        result = _slugify("Todd & Laura")
        assert result == "todd-laura"


class TestHasNameSlug:
    def test_base_episode_id_no_slug(self):
        assert _has_name_slug("ep_2026-01-01_120000") is False

    def test_episode_id_with_slug(self):
        assert _has_name_slug("ep_2026-01-01_120000_john-smith") is True

    def test_episode_id_with_multi_word_slug(self):
        assert _has_name_slug("ep_2026-01-01_120000_todd-laura") is True


class TestSaveEpisode:
    def test_save_creates_json(self, tmp_path):
        path = tmp_path / "episode.json"
        data = {"episode_id": "test", "status": "processing"}
        _save_episode(path, data)

        assert path.exists()
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["episode_id"] == "test"

    def test_save_handles_non_serializable(self, tmp_path):
        """default=str should handle non-serializable types."""
        path = tmp_path / "episode.json"
        data = {"episode_id": "test", "path": Path("/some/path")}
        _save_episode(path, data)

        with open(path) as f:
            loaded = json.load(f)
        assert loaded["path"] == "/some/path"


class TestAgentRegistry:
    def test_all_pipeline_agents_registered(self):
        for name in PIPELINE_ORDER:
            assert name in AGENT_REGISTRY, f"{name} not in AGENT_REGISTRY"

    def test_registry_count_matches_pipeline_order(self):
        assert len(AGENT_REGISTRY) == len(PIPELINE_ORDER)

    def test_pipeline_order_has_14_agents(self):
        assert len(PIPELINE_ORDER) == 14


class TestPipelinePauseAtCropSetup:
    """Test that pipeline pauses after stitch when crop_config is missing."""

    @patch("agents.pipeline._is_cancelled", return_value=False)
    @patch("agents.pipeline.load_config")
    def test_pauses_when_crop_config_missing(self, mock_config, mock_cancel, tmp_path):
        """Pipeline should pause with awaiting_crop_setup after stitch completes."""
        mock_config.return_value = {
            "paths": {"output_dir": str(tmp_path)},
            "processing": {},
        }

        episodes_dir = tmp_path / "episodes"
        episodes_dir.mkdir()

        from agents.pipeline import run_pipeline

        # Mock all agent classes to succeed immediately
        mock_agents = {}
        for name in PIPELINE_ORDER:
            mock_cls = MagicMock()
            mock_instance = MagicMock()
            mock_instance.run.return_value = {"duration_seconds": 60.0}
            mock_cls.return_value = mock_instance
            mock_agents[name] = mock_cls

        with patch("agents.pipeline.resolve_path", return_value=episodes_dir), \
             patch.dict("agents.pipeline.AGENT_REGISTRY", mock_agents):
            result = run_pipeline(
                source_path="/fake/source",
                agents=["ingest", "stitch", "audio_analysis", "speaker_cut"],
            )

        assert result["status"] == "awaiting_crop_setup"

    @patch("agents.pipeline._is_cancelled", return_value=False)
    @patch("agents.pipeline.load_config")
    def test_continues_when_crop_config_present(self, mock_config, mock_cancel, tmp_path):
        """Pipeline should NOT pause when crop_config is set before resuming."""
        mock_config.return_value = {
            "paths": {"output_dir": str(tmp_path)},
            "processing": {},
        }

        episodes_dir = tmp_path / "episodes"
        episodes_dir.mkdir()

        from agents.pipeline import run_pipeline

        mock_agents = {}
        for name in PIPELINE_ORDER:
            mock_cls = MagicMock()
            mock_instance = MagicMock()
            mock_instance.run.return_value = {"duration_seconds": 60.0}
            mock_cls.return_value = mock_instance
            mock_agents[name] = mock_cls

        with patch("agents.pipeline.resolve_path", return_value=episodes_dir), \
             patch.dict("agents.pipeline.AGENT_REGISTRY", mock_agents):
            # First run pauses at crop_setup (needs crop-dependent agent to trigger pause)
            result = run_pipeline(
                source_path="/fake/source",
                agents=["ingest", "stitch", "audio_analysis", "speaker_cut"],
            )
            assert result["status"] == "awaiting_crop_setup"

            # Simulate user setting crop_config
            ep_dir = episodes_dir / result["episode_id"]
            ep_file = ep_dir / "episode.json"
            with open(ep_file) as f:
                ep = json.load(f)
            ep["crop_config"] = {"speaker_l_center_x": 480}
            ep["status"] = "processing"
            with open(ep_file, "w") as f:
                json.dump(ep, f)

            # Resume with just speaker_cut (its deps are filtered to requested set)
            result2 = run_pipeline(
                source_path="/fake/source",
                agents=["speaker_cut"],
                episode_id=result["episode_id"],
            )

        assert result2["status"] == "ready_for_review"


class TestPipelineNonCriticalFailure:
    """Test that non-critical agent failures don't abort the pipeline."""

    @patch("agents.pipeline._is_cancelled", return_value=False)
    @patch("agents.pipeline.load_config")
    def test_non_critical_failure_continues(self, mock_config, mock_cancel, tmp_path):
        """A non-critical agent failing should not abort the pipeline."""
        mock_config.return_value = {
            "paths": {"output_dir": str(tmp_path)},
            "processing": {},
        }

        episodes_dir = tmp_path / "episodes"
        episodes_dir.mkdir()

        from agents.pipeline import run_pipeline

        mock_agents = {}
        for name in PIPELINE_ORDER:
            mock_cls = MagicMock()
            mock_instance = MagicMock()
            if name == "thumbnail_gen":
                mock_instance.run.side_effect = RuntimeError("API key missing")
            else:
                mock_instance.run.return_value = {"duration_seconds": 60.0}
            mock_cls.return_value = mock_instance
            mock_agents[name] = mock_cls

        with patch("agents.pipeline.resolve_path", return_value=episodes_dir), \
             patch.dict("agents.pipeline.AGENT_REGISTRY", mock_agents):

            ep_id = "ep_2026-01-01_120000"
            ep_dir = episodes_dir / ep_id
            ep_dir.mkdir(parents=True)
            for sub in ["source", "shorts", "subtitles", "metadata", "qa", "work"]:
                (ep_dir / sub).mkdir(exist_ok=True)

            ep_data = {
                "episode_id": ep_id,
                "status": "processing",
                "source_path": "/fake/source",
                "crop_config": {"speaker_l_center_x": 480},
                "pipeline": {
                    "started_at": "2026-01-01T12:00:00+00:00",
                    "completed_at": None,
                    "agents_completed": [],
                },
            }
            with open(ep_dir / "episode.json", "w") as f:
                json.dump(ep_data, f)

            result = run_pipeline(
                source_path="/fake/source",
                agents=["thumbnail_gen"],
                episode_id=ep_id,
            )

        # Pipeline should complete (not error) because thumbnail_gen is non-critical
        assert result["status"] == "ready_for_review"
        assert "thumbnail_gen" in result["pipeline"].get("errors", {})
