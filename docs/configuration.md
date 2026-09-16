# Configuration

- **`config/config.toml`** — all paths, thresholds, API settings. Copy from `config.example.toml`. Gitignored.
- **`.env`** — API keys: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`. Copy from `.env.example`. Gitignored.
- **`requirements.txt`** — installed via `uv pip install`. Includes `ruff` for dev tooling.
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

## API Costs per Episode (current)
- Deepgram transcription: ~$0.50 (stays on API — best-in-class STT).
- Claude clip mining: ~$0.10-0.30 (pending migration to `claude` CLI / Max subscription).
- Claude metadata: ~$0.10-0.20 (pending migration).
