# Working with Cascade

Codex is the recommended production agent. Start with README.md for installation,
credentials, and a first-episode prompt. Claude Code can use the same production
workflow; the `.claude` directory name does not make its Markdown instructions
exclusive to Claude.

## Producing an episode

Read `.claude/skills/produce/SKILL.md` and `docs/recovery-workflow.md` before episode
work. Operate through Cascade's local API; inspect `/openapi.json` for the live
contract and the episode's review, quality, pipeline-status, and delivery views
before acting. Do not infer completion from a filename or an old status label.

Preserve source recordings, distinguish source and output timestamps, and review
actual media before declaring an export ready. Prepare a bounded sample before
an expensive full render. Use the user's own config, storage, and accounts.

Codex operates the workflow; the app's automatic clip miner separately uses
`generation.provider = "openai"` or `"claude_cli"`. Never invent a `codex_cli`
provider or assume Codex login supplies an API key. Check credentials without
printing secrets. Local production does not authorize public posting, outreach,
recurring jobs, or spending beyond the user's request.

## Changing the software

Read `CLAUDE.md` for repository conventions and the relevant docs before editing.
Keep changes focused and general-purpose; do not copy a maintainer's episode
paths, account identifiers, credentials, or automation state into onboarding.
Run tests appropriate to the change; Python tests use `.venv/bin/python -m pytest`,
and frontend checks use `npm --prefix frontend test` and `npm --prefix frontend run build`.
Follow `.claude/skills/clean/SKILL.md` before committing. Do not require a separate
Claude installation merely to read the shared Markdown workflows.
