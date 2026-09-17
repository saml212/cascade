#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

cat <<'BANNER'

   ______                          __
  / ____/___ _______________ _____/ /__
 / /   / __ `/ ___/ ___/ __ `/ __  / _ \
/ /___/ /_/ (__  ) /__/ /_/ / /_/ /  __/
\____/\__,_/____/\___/\__,_/\__,_/\___/

  Podcast Automation Engine

BANNER

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

command -v uv >/dev/null 2>&1 || fail "uv is required. Install it with: brew install uv"
command -v node >/dev/null 2>&1 || fail "Node.js is required to build the frontend. Install it with: brew install node"
command -v npm >/dev/null 2>&1 || fail "npm is required to build the frontend. Install it with: brew install node"
# Homebrew's minimal ffmpeg omits subtitles. Prefer an installed full build.
for ffmpeg_dir in /opt/homebrew/opt/ffmpeg-full/bin /usr/local/opt/ffmpeg-full/bin; do
    if [[ -x "$ffmpeg_dir/ffmpeg" ]]; then
        export PATH="$ffmpeg_dir:$PATH"
        break
    fi
done
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg is required. Install an ffmpeg build that includes libass."

if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -E '(^|[[:space:]])ass[[:space:]]' >/dev/null; then
    fail "ffmpeg does not include the ass subtitle filter. Install ffmpeg-full (brew install ffmpeg-full) and ensure it is first on PATH."
fi

venv_valid=false
if [[ -x .venv/bin/python ]] && .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    venv_valid=true
fi

if [[ ( -e .venv || -L .venv ) && "$venv_valid" != true ]]; then
    backup=".venv-broken-$(date +%Y%m%d-%H%M%S)"
    echo "Existing .venv has no compatible working interpreter; preserving it as $backup"
    mv .venv "$backup"
fi

if [[ "$venv_valid" != true ]]; then
    echo "Creating Python 3.12 virtual environment..."
    uv venv --python 3.12 .venv
fi

echo "Installing core Python dependencies..."
uv pip install --python .venv/bin/python -r requirements.txt

echo "Building frontend..."
if [[ ! -f frontend/node_modules/.package-lock.json || frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]]; then
    npm --prefix frontend ci
fi
npm --prefix frontend run build
[[ -f frontend/dist/index.html ]] || fail "Frontend build did not produce frontend/dist/index.html"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
else
    echo "WARNING: no .env file found; API-backed features may be unavailable."
fi

[[ -n "${ANTHROPIC_API_KEY:-}" ]] || echo "ANTHROPIC_API_KEY is not set; optional API-based clip generation is unavailable. Local release preparation still works."
[[ -n "${DEEPGRAM_API_KEY:-}" ]] || echo "WARNING: DEEPGRAM_API_KEY is not set; transcription will be unavailable."

mkdir -p config output work

cleanup() {
    echo
    echo "Cascade stopped."
}
trap cleanup EXIT

(sleep 2 && open "http://127.0.0.1:8420") &

echo
echo "Starting Cascade on http://127.0.0.1:8420"
echo "Press Ctrl+C to stop."
echo

exec .venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8420
