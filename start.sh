#!/usr/bin/env bash
set -euo pipefail

# ── Navigate to the script's own directory ──────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── ASCII banner ────────────────────────────────────────────────────────────
cat << 'BANNER'

    ____  _      __  _ __
   / __ \(_)____/ /_(_) /
  / / / / / ___/ __/ / /
 / /_/ / (__  ) /_/ / /
/_____/_/____/\__/_/_/

  Podcast Automation Engine
  ─────────────────────────

BANNER

# ── Trap SIGINT for clean shutdown ──────────────────────────────────────────
cleanup() {
    echo ""
    echo "Distil stopped."
    exit 0
}
trap cleanup SIGINT

# ── Check Python 3.10+ ─────────────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.10 or later."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 10 ]]; }; then
    echo "ERROR: Python 3.10+ is required (found $PYTHON_VERSION)."
    echo "       Install a newer version: https://www.python.org/downloads/"
    exit 1
fi

echo "Using Python $PYTHON_VERSION"

# ── Create virtual environment if it doesn't exist ──────────────────────────
if [[ ! -d ".venv" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# ── Activate virtual environment ────────────────────────────────────────────
source .venv/bin/activate
echo "Virtual environment activated."

# ── Install / update dependencies ───────────────────────────────────────────
echo "Installing dependencies..."
pip install -q -r requirements.txt

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
mkdir -p output output/episodes work config data logs
echo "Directories verified."

# ── Open browser after a short delay (background) ──────────────────────────
(sleep 2 && open "http://localhost:8420") &

# ── Start FastAPI server ────────────────────────────────────────────────────
echo ""
echo "Starting Distil on http://localhost:8420"
echo "Press Ctrl+C to stop."
echo ""

uvicorn server.app:app --host 0.0.0.0 --port 8420 --reload
