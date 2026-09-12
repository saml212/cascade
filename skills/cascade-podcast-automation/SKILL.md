---
name: cascade-podcast-automation
description: Bootstrap Cascade locally and create an episode from a raw media path, then continue through the canonical API-first producer workflow. Use when asked to start Cascade, ingest source media, or troubleshoot initial local setup; use the produce skill for review, editing, rendering, QA, delivery, and publication.
---

# Cascade ingest bridge

Read the canonical [`produce` skill](../../.claude/skills/produce/SKILL.md) before changing episode state. This compatibility skill covers only service startup and ingest. It never selects or approves clips, approves a release, or publishes media.

From the repository root, run:

```bash
./skills/cascade-podcast-automation/scripts/run_cascade_workflow.sh \
  --source /absolute/path/to/raw/media/or/folder
```

Use `--repo /absolute/path/to/cascade` when the working repository differs. Set `API_BASE_URL` to target an existing local service; the default is `http://127.0.0.1:8420`.

The runner validates the source, reuses a healthy API or starts `./start.sh`, creates the episode through the API, and reports the service log, API URL, episode ID, episode directory, episode response, and files present after ingest. It leaves a service it started running for the review workflow and never stops an unrelated server.

After ingest, use `GET /openapi.json` and the `review`, `quality`, `pipeline-status`, and `delivery` episode views named in the `produce` skill. Treat missing outputs as current state to inspect through those APIs; do not classify them as product limitations or bypass a failed gate.
