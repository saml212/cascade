---
name: cascade-podcast-automation
description: Automate setup, execution, and troubleshooting of the Cascade podcast workflow. Use when asked to clone/run Cascade, stand up frontend/backend, ingest raw camera footage paths, validate episode artifacts, or diagnose why processing/publish steps did not complete.
---

# Cascade Podcast Automation

Operate Cascade in a local clone of the repository. Prefer the current repository root unless the user provides `--repo`.

## Run Workflow

Use the deterministic runner:

```bash
./skills/cascade-podcast-automation/scripts/run_cascade_workflow.sh \
  --source /absolute/path/to/raw/footage/or/folder
```

Optional flags:
- `--repo /absolute/path/to/cascade-working`
- `--manual-clip` to add and approve a sample clip after episode creation

Optional environment variable:
- `API_BASE_URL` (default `http://127.0.0.1:8420`)

## Required Reporting

Always report:
- `server_log`
- `api_base_url`
- `episode_id`
- `episode_dir`
- generated files found under the episode directory
- missing expected outputs and whether this is due to known product limitations

## Standard Procedure

1. Verify `--source` exists and is readable.
2. Run the script:
```bash
./skills/cascade-podcast-automation/scripts/run_cascade_workflow.sh \
  --source /absolute/path/to/source
```
3. Read `episode_id`, `episode_detail`, and `files` from script output.
4. Validate artifacts in `episode_dir`.
5. If only metadata files exist (for example `episode.json`, `clips.json`), classify this as current implementation state unless logs show a hard failure.

## Troubleshooting

If startup fails:
- Verify `start.sh` is executable.
- Verify Python 3.10+ is available and selected by `start.sh`.
- Check `server_log` for dependency/import errors.

If episode creation fails:
- Check HTTP status and body printed by the script.
- For permission errors referencing `/Volumes/...`, update `.env` `CASCADE_*` paths to writable directories and rerun.

If API endpoints return 404:
- Verify current branch includes the expected API routes and slash compatibility.

If outputs are missing:
- Read `references/cascade-current-state.md` before declaring a failure.
- Treat missing render/publish artifacts as a product gap unless logs show explicit runtime errors.

## Current Implementation Reality

Read `references/cascade-current-state.md` before promising full automated editing/publishing.

## Resource Map

- `scripts/run_cascade_workflow.sh`: start Cascade, create an episode, optionally add/approve a manual clip, and summarize artifacts.
- `references/cascade-current-state.md`: implemented vs placeholder functionality and expected artifacts.
