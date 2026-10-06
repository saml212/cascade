# Configuration

- **`config/config.toml`** — all paths, thresholds, API settings. Copy from `config.example.toml`. Gitignored.
- **`.env`** — service credentials such as `DEEPGRAM_API_KEY` and optional `OPENAI_API_KEY`. Copy from `.env.example`. Gitignored.
- **`requirements.txt`** — core runtime dependencies installed via `uv pip install`. Development tooling, including `ruff`, lives in `requirements-dev.txt`.
- **`tomllib`** (stdlib, Python 3.11+) is used for TOML parsing. `tomli` has been removed.

Short-form destinations are enabled independently under `platforms`. Facebook,
Threads, Bluesky, LinkedIn, and Pinterest stay disabled in the example until their
Upload-Post accounts are connected. Set each new destination's `account_username`
to the stable opaque `username` returned by the profile detail API. Facebook also
requires an exact `page_id`; Pinterest requires an exact public `board_id`;
LinkedIn uses the personal profile when `page_id` is empty and otherwise requires
that exact company Page. Preview and execution recheck the connected account,
available Page or board, and any pinned Page before an intent can be saved.

Enabling a destination adds its copy and account target to the release revision.
Run QA, review the final destination payload, and refresh publish approval before
submitting an explicit destination subset. Existing destination receipts remain
immutable, and later disjoint subsets publish only destinations still missing for
the same selected media.

`platforms.<destination>.required_short_variant_id` optionally requires one
reviewed short variant for that destination. Cascade rejects a preview or short
submission whose effective clip versions do not match. When a destination needs
a different artifact from the global selection, preview it as a separate
destination request and map each clip ID in `variant_overrides`; Cascade does not
split a multi-destination request automatically. Changing this setting changes
the release revision for an enabled destination and requires refreshed publish
approval.

`podcast.links.episode_url_template` optionally selects the human-facing episode
URL used in new short-form copy. It must be an HTTPS URL containing exactly one
`{episode_id}` placeholder; Cascade URL-encodes the ID before substitution. When
the setting is empty, copy keeps using the legacy
`podcast.r2.public_url/links/episodes/<episode_id>.html` route. The R2 setting
continues to own media, RSS, artwork, and legacy watch pages. Changing the
template changes the release revision for new work but does not edit existing
receipts or remote jobs.

To prepare one Facebook pilot, update only that clip's reviewed base copy:

```http
PATCH /api/episodes/{episode_id}/clips/{clip_id}/metadata
Content-Type: application/json

{
  "metadata": {
    "facebook": {
      "title": "Reviewed Reel title",
      "description": "Reviewed Reel description"
    }
  }
}
```

The route merges the Facebook block with other platform metadata and clears the
clip's prior approval. Keep the URL out of `description`: the preview builder
appends `Full episode: <resolved episode URL>`. Review that final payload, then
repeat the normal variant approval, QA, and release approval flow before a
Facebook-only submission.

The resolved URL is shared by destinations that receive episode links. Do not
include Instagram in a generated wave when the template uses a `#...` fragment:
Instagram parses the fragment as a hashtag and does not make caption URLs
clickable. Until Instagram has destination-specific URL formatting, use native
copy with the bare show URL plus an explicit guest or episode cue, such as `Full
episode at thelocalpod.link — choose Arnold Gray.` Facebook and YouTube can
retain the episode fragment.

## Generation and costs

Use Codex as the production agent as described in [the README](../README.md).
The app's automatic clip miner has its own transport: the example defaults to an
authenticated Claude CLI (`generation.provider = "claude_cli"`), or you can select
`"openai"` with `OPENAI_API_KEY` and an explicit `generation.openai_model`.
Codex login does not configure the app's generation transport. Transcription uses
Deepgram separately. Check current provider pricing and account limits before
processing; costs depend on recording length and the chosen model.
