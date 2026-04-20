# Configuration

- **`config/config.toml`** — all paths, thresholds, API settings. Copy from `config.example.toml`. Gitignored.
- **`.env`** — API keys: `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`. Copy from `.env.example`. Gitignored.
- **`requirements.txt`** — installed via `uv pip install`. Includes `ruff` for dev tooling.
- **`tomllib`** (stdlib, Python 3.11+) is used for TOML parsing. `tomli` has been removed.

## API Costs per Episode (current)
- Deepgram transcription: ~$0.50 (stays on API — best-in-class STT).
- Claude clip mining: ~$0.10-0.30 (pending migration to `claude` CLI / Max subscription).
- Claude metadata: ~$0.10-0.20 (pending migration).
