"""Tests for the shared schema-bound generation transports."""

import json
from unittest.mock import MagicMock, patch

import pytest

from lib.generation import generate_structured


def _completed_response(text='{"answer":"grounded"}'):
    response = MagicMock()
    response.is_success = True
    response.json.return_value = {
        "id": "resp_123",
        "status": "completed",
        "model": "responses-test-model",
        "created_at": 1,
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"total_tokens": 42},
    }
    return response


def test_structured_generation_uses_responses_schema_and_keeps_no_state(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    with patch("lib.generation.httpx.post", return_value=_completed_response()) as post:
        generated, provenance = generate_structured(
            {
                "generation": {
                    "provider": "openai",
                    "openai_model": "responses-test-model",
                }
            },
            task="clip alternative",
            instructions="Use only supplied transcript evidence.",
            prompt="Transcript evidence",
            schema=schema,
        )

    request = post.call_args.kwargs
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["json"]["model"] == "responses-test-model"
    assert request["json"]["store"] is False
    assert request["json"]["text"]["format"] == {
        "type": "json_schema",
        "name": "clip_alternative",
        "strict": True,
        "schema": schema,
    }
    assert generated == {"answer": "grounded"}
    assert provenance["response_id"] == "resp_123"
    assert provenance["usage"] == {"total_tokens": 42}


def test_default_transport_uses_authenticated_claude_cli_schema():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    process = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "is_error": False,
                "uuid": "turn_123",
                "structured_output": {"answer": "grounded"},
                "modelUsage": {"claude-sonnet-5": {"inputTokens": 20}},
                "usage": {"input_tokens": 20, "output_tokens": 4},
                "total_cost_usd": 0.002,
            }
        ),
    )

    with patch("lib.generation.subprocess.run", return_value=process) as run:
        generated, provenance = generate_structured(
            {},
            task="clip alternative",
            instructions="Use only supplied transcript evidence.",
            prompt="Transcript evidence",
            schema=schema,
        )

    command = run.call_args.args[0]
    assert command[:4] == ["claude", "-p", "--output-format", "json"]
    assert command[command.index("--json-schema") + 1] == json.dumps(
        schema, sort_keys=True, separators=(",", ":")
    )
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert run.call_args.kwargs["input"] == "Transcript evidence"
    assert generated == {"answer": "grounded"}
    assert provenance["provider"] == "claude_cli"
    assert provenance["resolved_models"] == ["claude-sonnet-5"]
    assert provenance["stored"] is False


def test_structured_generation_requires_configured_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        generate_structured(
            {"generation": {"provider": "openai"}},
            task="test",
            instructions="ground output",
            prompt="text",
            schema={},
        )


def test_openai_transport_requires_an_explicit_verified_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    with pytest.raises(RuntimeError, match="openai_model"):
        generate_structured(
            {"generation": {"provider": "openai"}},
            task="test",
            instructions="ground output",
            prompt="text",
            schema={},
        )


def test_structured_generation_surfaces_refusal(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _completed_response()
    response.json.return_value["output"][0]["content"] = [
        {"type": "refusal", "refusal": "Cannot complete this request"}
    ]

    with (
        patch("lib.generation.httpx.post", return_value=response),
        pytest.raises(RuntimeError, match="refused"),
    ):
        generate_structured(
            {
                "generation": {
                    "provider": "openai",
                    "openai_model": "responses-test-model",
                }
            },
            task="test",
            instructions="ground output",
            prompt="text",
            schema={},
        )


def test_structured_generation_rejects_incomplete_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    response = _completed_response()
    response.json.return_value.update(
        status="incomplete", incomplete_details={"reason": "max_output_tokens"}
    )

    with (
        patch("lib.generation.httpx.post", return_value=response),
        pytest.raises(RuntimeError, match="max_output_tokens"),
    ):
        generate_structured(
            {
                "generation": {
                    "provider": "openai",
                    "openai_model": "responses-test-model",
                }
            },
            task="test",
            instructions="ground output",
            prompt="text",
            schema={},
        )
