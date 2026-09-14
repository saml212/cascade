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

## API Costs per Episode (current)
- Deepgram transcription: ~$0.50 (stays on API — best-in-class STT).
- Claude clip mining: ~$0.10-0.30 (pending migration to `claude` CLI / Max subscription).
- Claude metadata: ~$0.10-0.20 (pending migration).
