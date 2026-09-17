"""Tests for the public ``python -m agents`` client."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from agents.__main__ import _start_pipeline, main


def test_retired_agent_fails_before_episode_creation(tmp_path, capsys):
    with (
        patch.object(
            sys,
            "argv",
            [
                "agents",
                "--source-path",
                str(tmp_path),
                "--agents",
                "metadata_gen",
            ],
        ),
        patch("agents.__main__._create_episode") as create_episode,
        patch("agents.__main__._check_server") as check_server,
        pytest.raises(SystemExit) as error,
    ):
        main()

    assert error.value.code == 2
    assert "retired agent(s): metadata_gen" in capsys.readouterr().err
    create_episode.assert_not_called()
    check_server.assert_not_called()


def test_start_pipeline_only_accepts_exact_already_running_conflict(capsys):
    response = MagicMock(status_code=409)
    response.json.return_value = {"detail": "Pipeline already running for this episode"}
    client = MagicMock()
    client.post.return_value = response

    assert _start_pipeline(client, "ep_001", "/source", None, ["ingest"])

    response.raise_for_status.assert_not_called()
    assert "Pipeline already running for ep_001" in capsys.readouterr().out


def test_start_pipeline_propagates_retired_agent_conflict(capsys):
    response = MagicMock(status_code=409)
    response.json.return_value = {
        "detail": {
            "code": "agent_retired",
            "agents": ["metadata_gen"],
            "message": "One or more requested agents are retired.",
        }
    }
    response.raise_for_status.side_effect = RuntimeError("HTTP 409")
    client = MagicMock()
    client.post.return_value = response

    with pytest.raises(RuntimeError, match="HTTP 409"):
        _start_pipeline(client, "ep_001", "/source", None, ["metadata_gen"])

    response.raise_for_status.assert_called_once_with()
    assert "already running" not in capsys.readouterr().out.lower()
