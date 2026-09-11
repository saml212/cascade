"""Shared schema-bound generation transport for Cascade content agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from typing import Any

import httpx

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _response_text(payload: dict) -> str:
    refusals = []
    texts = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(str(content["text"]))
            elif content.get("type") == "refusal":
                refusals.append(str(content.get("refusal", "Request refused")))
    if refusals:
        raise RuntimeError(f"OpenAI refused structured generation: {refusals[0]}")
    if not texts:
        raise RuntimeError("OpenAI response did not contain structured output text")
    return "".join(texts)


def _openai_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        error = {}
    code = error.get("code") or error.get("type")
    suffix = f", {code}" if code else ""
    return f"OpenAI structured generation failed (HTTP {response.status_code}{suffix})"


def _generate_openai(
    config: dict,
    *,
    task: str,
    instructions: str,
    prompt: str,
    schema: dict,
    max_output_tokens: int = 4096,
) -> tuple[dict, dict]:
    settings = config.get("generation", {})
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for content generation")

    schema_name = re.sub(r"[^A-Za-z0-9_-]", "_", task)[:64]
    if not schema_name:
        raise ValueError("Generation task name is required")
    model = str(settings.get("openai_model", "")).strip()
    if not model:
        raise RuntimeError(
            "generation.openai_model must name a model available to this API key"
        )
    effort = str(settings.get("reasoning_effort", "medium"))
    request_body = {
        "model": model,
        "input": [
            {"role": "developer", "content": instructions},
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    try:
        response = httpx.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=float(settings.get("timeout_seconds", 180)),
        )
        if not response.is_success:
            raise RuntimeError(_openai_error(response))
        payload = response.json()
    except RuntimeError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("OpenAI structured generation transport failed") from exc

    if payload.get("status") != "completed":
        detail = payload.get("error") or payload.get("incomplete_details") or {}
        raise RuntimeError(f"OpenAI structured generation was incomplete: {detail}")
    try:
        generated = json.loads(_response_text(payload))
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI returned invalid structured JSON") from exc
    if not isinstance(generated, dict):
        raise TypeError("OpenAI structured generation must return a JSON object")

    provenance = {
        "provider": "openai",
        "endpoint": "responses",
        "model": payload.get("model", model),
        "response_id": payload.get("id"),
        "created_at": payload.get("created_at"),
        "reasoning_effort": effort,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "schema_sha256": _digest(schema),
        "usage": payload.get("usage", {}),
        "stored": False,
    }
    return generated, provenance


def _generate_claude_cli(
    config: dict,
    *,
    task: str,
    instructions: str,
    prompt: str,
    schema: dict,
    max_output_tokens: int,
) -> tuple[dict, dict]:
    settings = config.get("generation", {})
    model = str(settings.get("model", "sonnet"))
    effort = str(settings.get("reasoning_effort", "medium"))
    payload = _run_claude_cli(
        instructions,
        prompt,
        model=model,
        effort=effort,
        timeout=float(settings.get("timeout_seconds", 180)),
        schema=schema,
    )

    generated = payload.get("structured_output")
    if generated is None:
        try:
            generated = json.loads(payload.get("result", ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Claude CLI response had no structured output") from exc
    if not isinstance(generated, dict):
        raise TypeError("Claude CLI structured generation must return a JSON object")

    provenance = {
        "provider": "claude_cli",
        "endpoint": "claude-cli",
        "model": model,
        "resolved_models": sorted(payload.get("modelUsage", {})),
        "response_id": payload.get("uuid"),
        "reasoning_effort": effort,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "schema_sha256": _digest(schema),
        "usage": payload.get("usage", {}),
        "cost_usd": payload.get("total_cost_usd"),
        "stored": False,
        "max_output_tokens_requested": max_output_tokens,
    }
    return generated, provenance


def _run_claude_cli(
    system_prompt: str,
    prompt: str,
    *,
    model: str,
    timeout: float,
    effort: str | None = None,
    schema: dict | None = None,
) -> dict:
    command = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--safe-mode",
        "--no-session-persistence",
        "--no-chrome",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--system-prompt",
        system_prompt,
    ]
    if effort:
        command.extend(["--effort", effort])
    if schema is not None:
        command.extend(
            [
                "--json-schema",
                json.dumps(schema, sort_keys=True, separators=(",", ":")),
            ]
        )
    command.extend(["--tools", ""])
    try:
        process = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("claude CLI is required for configured generation") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Claude CLI timed out after {timeout}s") from exc

    if process.returncode:
        raise RuntimeError(f"Claude CLI failed (exit {process.returncode})")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude CLI returned invalid response JSON") from exc
    if payload.get("is_error"):
        raise RuntimeError("Claude CLI reported a generation error")
    return payload


def generate_text(
    system_prompt: str,
    messages: list[dict],
    model: str = "sonnet",
    timeout: float = 120.0,
) -> str:
    """Run one isolated, tool-free Claude CLI text turn."""
    prompt = "\n\n".join(
        f"<{message.get('role', 'user')}>\n{message.get('content', '')}\n"
        f"</{message.get('role', 'user')}>"
        for message in messages
    )
    payload = _run_claude_cli(
        system_prompt,
        prompt,
        model=model,
        timeout=timeout,
    )
    text = payload.get("result") or payload.get("response") or ""
    if not text and isinstance(payload.get("content"), list):
        text = "".join(
            block.get("text", "")
            for block in payload["content"]
            if isinstance(block, dict)
        )
    if not text:
        raise RuntimeError("Claude CLI response had no text")
    return text


def generate_structured(
    config: dict,
    *,
    task: str,
    instructions: str,
    prompt: str,
    schema: dict,
    max_output_tokens: int = 4096,
) -> tuple[dict, dict]:
    """Generate one validated JSON object through the configured transport."""
    if not re.sub(r"[^A-Za-z0-9_-]", "_", task)[:64]:
        raise ValueError("Generation task name is required")
    provider = str(config.get("generation", {}).get("provider", "claude_cli"))
    kwargs = {
        "task": task,
        "instructions": instructions,
        "prompt": prompt,
        "schema": schema,
        "max_output_tokens": max_output_tokens,
    }
    if provider == "claude_cli":
        return _generate_claude_cli(config, **kwargs)
    if provider == "openai":
        return _generate_openai(config, **kwargs)
    raise ValueError(f"Unsupported generation provider: {provider!r}")
