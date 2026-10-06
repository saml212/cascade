# Cascade

Turn podcast recordings into reviewed longform video, captioned shorts, thumbnails, and publish-ready files. Cascade supports single- and multi-camera recordings, separate microphones, speaker framing, audio repair, and optional publishing and scheduling.

**Use [OpenAI Codex](https://developers.openai.com/codex/quickstart) as your production agent.** Open this repository in Codex, give it your recordings, and let it operate Cascade's local API while you review the media in the browser. Codex is the recommended starting point; Claude Code remains compatible with the shared producer instructions.

## Get started with Codex

Clone this repository, then open the `cascade` folder in the Codex desktop app. For the terminal version, follow the [official Codex CLI setup](https://developers.openai.com/codex/cli):

```bash
git clone https://github.com/saml212/cascade.git
cd cascade
npm install -g @openai/codex
codex
```

Sign in when prompted. Give Codex this first task, replacing the media path:

> Read AGENTS.md and README.md, then help me install and start Cascade on this computer. Read the producer workflow before operating an episode. Set up my own podcast name and storage paths. My recordings are in /absolute/path/to/recordings. Check which services are configured before starting paid work. Prepare a short local sample for me to review before rendering the whole episode. Preserve my source recordings. Do not publish or schedule anything yet.

Codex operates the software through its API and local tools. Its login is separate from service credentials used by Cascade. In particular, the optional built-in automatic clip miner currently supports **OpenAI's Responses API or Claude CLI**, not a `codex_cli` provider. You can use Codex for production without installing Claude Code; configure the OpenAI generation option below if you want the built-in clip miner too.

## Install and launch

macOS is the primary development environment. You need Git, Node.js/npm, `uv`, and FFmpeg with the `ass` subtitle filter. The launcher creates a Python 3.12 environment (Python 3.11+ is supported).

With [Homebrew](https://brew.sh/) installed:

```bash
brew install git node uv ffmpeg-full
# If you have not cloned the repository yet:
git clone https://github.com/saml212/cascade.git
cd cascade
cp config/config.example.toml config/config.toml
cp .env.example .env
```

Before the first episode:

1. Edit `config/config.toml`: set your podcast title/author and storage paths. Use absolute paths to existing storage locations. Allow space for source copies, render intermediates, and final videos.
2. Add only the credentials you need to `.env`, using the table below. Neither this file nor your local config is committed to Git.
3. Start the app:

```bash
./start.sh
```

The launcher installs Python dependencies, builds the frontend, and starts [Cascade at http://127.0.0.1:8420](http://127.0.0.1:8420). Keep that terminal open; Ctrl+C stops the server. The local UI can launch without service credentials.

### Credentials and generation

| Feature | Setup |
| --- | --- |
| Local import, framing, manual editing, rendering and export | No service key required; transcription and automatic clip mining are separate stages |
| Transcription | `DEEPGRAM_API_KEY` in `.env` |
| Automatic clip mining with OpenAI | `OPENAI_API_KEY` in `.env`, plus the generation settings below |
| Automatic clip mining with Claude | An installed, authenticated `claude` CLI; `[generation] provider = "claude_cli"` is the existing example default |
| Social publishing/scheduling | `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER`, connected destination accounts, and reviewed platform configuration |
| Video podcast RSS and hosted media | `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, and your own `[podcast.r2]`/feed settings |

To use OpenAI for the built-in clip miner, **replace the existing `[generation]` section** in `config/config.toml` with:

```toml
[generation]
provider = "openai"
openai_model = "YOUR_MODEL_ID"
reasoning_effort = "medium"
timeout_seconds = 180
```

Replace `YOUR_MODEL_ID` with a model available to your API project that supports structured output and the configured reasoning effort. This API usage is billed separately from a Codex subscription. An Anthropic API key is not required by the current Claude CLI generation path. Service pricing and account limits vary; check them before processing a long recording.

### First episode

Start with a small copy of representative footage and matching recorder tracks. Ask Codex to follow [AGENTS.md](AGENTS.md) and the [producer workflow](.claude/skills/produce/SKILL.md), or import the source directory in the browser.

Review microphone mapping, synchronization, speaker names, crops, and a short rendered sample before running a full export. The release workflow is: confirm picture and sound, edit episode details, then prepare and download the local upload files. Publishing is a separate explicit action with revision-bound approval and QA checks.

The API contract is available at [http://127.0.0.1:8420/docs](http://127.0.0.1:8420/docs). For recovery and detailed production steps, see [docs/recovery-workflow.md](docs/recovery-workflow.md).

## What Cascade supports

- Media ingest, validation, stitching, and external audio synchronization.
- Per-microphone analysis, speaker segmentation, framing, and longform rendering.
- Transcription, captioned vertical shorts, and optional automatic clip selection.
- Clean shorts, one-gameplay and multi-gameplay compositions using your supplied media.
- Audio mastering and evidence-based repair with reviewable replacement files.
- Thumbnails from actual episode footage, metadata, QA, and local export.
- Optional Upload-Post publishing/scheduling and Cloudflare R2 video podcast RSS.
- Optional backups to your configured storage.

The dependency-aware pipeline runs independent stages in parallel. It does not install recurring social-growth jobs as part of app startup. Publishing still requires your own accounts and approvals; this repository does not include the maintainer's recordings or account credentials.

## Troubleshooting

| Problem | Next step |
| --- | --- |
| FFmpeg has no `ass` filter | Install `ffmpeg-full`. The launcher prefers its Homebrew binary over the minimal build. |
| Missing `uv`, `node`, or `npm` | Install the prerequisites above and reopen your terminal. |
| Transcription cannot start | Set `DEEPGRAM_API_KEY` and restart the server. |
| Clip mining asks for `claude`, or generation fails | Choose and configure one of the generation options above; Codex sign-in alone does not configure this stage. |
| Browser does not open | Visit `http://127.0.0.1:8420` directly and inspect the launch terminal for errors. |
| Port 8420 is already in use | Check whether Cascade is already running before starting another server. |
| Storage resolves somewhere unexpected | Verify absolute paths and mounted volumes; inspect the episode location returned by the API. |

The launcher opens the browser using macOS `open`. On another system, install the equivalent prerequisites and use the manual launch below; cross-platform production is less exercised than macOS.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8420
```

Create and edit the config and `.env` files first, as above. Keep the server on loopback; it is a local production tool, not a hosted multi-user service.

Optional DeepFilterNet restoration has a large PyTorch dependency. Install it only if you select that restoration path:

```bash
uv pip install --python .venv/bin/python -r requirements-restoration.txt
```

## Configuration and development

See [configuration](docs/configuration.md), [architecture](docs/architecture.md), and [recovery workflow](docs/recovery-workflow.md). Core directories:

| Directory | Purpose |
| --- | --- |
| `agents/` | Pipeline stages and dependency orchestration |
| `lib/` | Media, generation, storage, and approval helpers |
| `server/` | FastAPI application and production API |
| `frontend/` | TypeScript/Vite browser UI |
| `config/` | Example configuration; your `config.toml` stays local |
| `tests/`, `frontend/test/` | Python and frontend tests |

To update an existing installation, preserve your local config/media, stop active work, then run `git pull --ff-only` and `./start.sh`. If you have local code changes, review them before pulling.

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Storage can live on local or external disks. `CASCADE_OUTPUT_DIR`, `CASCADE_WORK_DIR`, and `CASCADE_BACKUP_DIR` override configured paths. Use absolute paths; missing external volumes can cause a local fallback, so verify the resolved location before ingesting large recordings.

For a public episode hub, configure your own `podcast.links.episode_url_template` as an HTTPS URL containing one `{episode_id}` placeholder. The maintainer's separate show website is not required to run Cascade. See the configuration guide for destination-specific link handling.

## License

MIT — see [LICENSE](LICENSE).
