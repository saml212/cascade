#!/usr/bin/env bash
set -euo pipefail

# ── Navigate to the script's own directory ──────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── ASCII banner ────────────────────────────────────────────────────────────
cat << 'BANNER'

   ______                          __
  / ____/___ _______________ _____/ /__
 / /   / __ `/ ___/ ___/ __ `/ __  / _ \
/ /___/ /_/ (__  ) /__/ /_/ / /_/ /  __/
\____/\__,_/____/\___/\__,_/\__,_/\___/

  Podcast Automation Engine
  ─────────────────────────

BANNER

# ── Trap SIGINT for clean shutdown ──────────────────────────────────────────
cleanup() {
    echo ""
    echo "Cascade stopped."
    exit 0
}
trap cleanup SIGINT

# ── Check uv is installed ──────────────────────────────────────────────────
if ! command -v uv &> /dev/null; then
    echo "ERROR: uv not found. Install it: brew install uv"
    exit 1
fi

echo "Using uv $(uv --version | awk '{print $2}')"

# ── Create virtual environment if it doesn't exist ──────────────────────────
if [[ ! -d ".venv" ]]; then
    echo "Creating virtual environment (Python 3.12)..."
    uv venv --python 3.12
fi

# ── Install / update dependencies ───────────────────────────────────────────
echo "Installing dependencies..."
uv pip install -r requirements.txt

# ── Load .env if present ────────────────────────────────────────────────────
if [[ -f ".env" ]]; then
    echo "Loading .env..."
    set -a
    source .env
    set +a
else
    echo "No .env file found. Copy .env.example to .env and add your keys."
fi

# ── Validate API keys (warn only) ──────────────────────────────────────────
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "WARNING: ANTHROPIC_API_KEY is not set. LLM features will be unavailable."
fi

if [[ -z "${DEEPGRAM_API_KEY:-}" ]]; then
    echo "WARNING: DEEPGRAM_API_KEY is not set. Transcription will be unavailable."
fi

# ── Create default directories ──────────────────────────────────────────────
mkdir -p config
echo "Directories verified."

# ── Open browser after a short delay (background) ──────────────────────────
(sleep 2 && open "http://localhost:8420") &

# ── Start FastAPI server ────────────────────────────────────────────────────
echo ""
echo "Starting Cascade on http://localhost:8420"
echo "Press Ctrl+C to stop."
echo ""

.venv/bin/uvicorn server.app:app --host 0.0.0.0 --port 8420 --reload
