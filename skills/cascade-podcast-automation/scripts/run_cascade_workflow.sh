#!/usr/bin/env bash
set -euo pipefail

# Infer repo root from this script location: skills/<skill>/scripts/<script>.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DEFAULT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SOURCE_PATH=""
REPO_PATH="$REPO_DEFAULT"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8420}"

usage() {
  cat <<USAGE
Usage: $0 --source <path> [--repo <path>]

Options:
  --source <path>   Raw footage file or folder path (required)
  --repo <path>     Cascade repo path (default: $REPO_DEFAULT)

Environment:
  API_BASE_URL      Cascade API base URL (default: $API_BASE_URL)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE_PATH="${2:-}"
      shift 2
      ;;
    --repo)
      REPO_PATH="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$SOURCE_PATH" ]]; then
  echo "ERROR: --source is required" >&2
  usage
  exit 1
fi

if [[ ! -e "$SOURCE_PATH" ]]; then
  echo "ERROR: source path does not exist: $SOURCE_PATH" >&2
  exit 1
fi

if [[ ! -d "$REPO_PATH" ]]; then
  echo "ERROR: repo path not found: $REPO_PATH" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl is required but not found in PATH" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required but not found in PATH" >&2
  exit 1
fi

cd "$REPO_PATH"

STARTED_SERVER="false"
SERVER_PID=""
SERVER_LOG="existing API process (not started by runner)"

if ! curl -fsS "$API_BASE_URL/api/episodes" >/dev/null 2>&1; then
  if [[ ! -x "./start.sh" ]]; then
    echo "ERROR: start.sh not executable in $REPO_PATH" >&2
    exit 1
  fi

  mkdir -p logs
  LOG_FILE="logs/skill-run-$(date +%Y%m%d-%H%M%S).log"
  SERVER_LOG="$REPO_PATH/$LOG_FILE"
  nohup ./start.sh >"$LOG_FILE" 2>&1 &
  SERVER_PID=$!
  STARTED_SERVER="true"

  READY="false"
  for _ in $(seq 1 45); do
    if curl -fsS "$API_BASE_URL/api/episodes" >/dev/null 2>&1; then
      READY="true"
      break
    fi
    sleep 1
  done

  if [[ "$READY" != "true" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    echo "ERROR: server did not become ready. See $SERVER_LOG" >&2
    exit 1
  fi
fi

CREATE_BODY=$(python3 -c 'import json, sys; print(json.dumps({"source_path": sys.argv[1]}))' "$SOURCE_PATH")
create_tmp="$(mktemp)"
create_status=$(curl -sS -o "$create_tmp" -w '%{http_code}' -X POST "$API_BASE_URL/api/episodes" \
  -H 'content-type: application/json' \
  -d "$CREATE_BODY" || true)
CREATE_JSON="$(cat "$create_tmp")"
rm -f "$create_tmp"

if [[ ! "$create_status" =~ ^2 ]]; then
  echo "ERROR: episode create failed (HTTP $create_status): $CREATE_JSON" >&2
  echo "server_log=$SERVER_LOG" >&2
  exit 1
fi

EPISODE_ID=$(printf '%s' "$CREATE_JSON" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
print(data.get("episode_id",""))')

if [[ -z "$EPISODE_ID" ]]; then
  echo "ERROR: could not parse episode_id from response: $CREATE_JSON" >&2
  exit 1
fi

detail_tmp="$(mktemp)"
detail_status=$(curl -sS -o "$detail_tmp" -w '%{http_code}' "$API_BASE_URL/api/episodes/$EPISODE_ID" || true)
DETAIL_JSON="$(cat "$detail_tmp")"
rm -f "$detail_tmp"

if [[ ! "$detail_status" =~ ^2 ]]; then
  echo "ERROR: episode detail fetch failed (HTTP $detail_status): $DETAIL_JSON" >&2
  echo "server_log=$SERVER_LOG" >&2
  exit 1
fi

EPISODES_DIR=$(grep -E '^CASCADE_OUTPUT_DIR=' .env 2>/dev/null | cut -d= -f2- || true)
if [[ -z "$EPISODES_DIR" ]]; then
  EPISODES_DIR="$REPO_PATH/output/episodes"
fi
EPISODES_DIR="${EPISODES_DIR%\"}"
EPISODES_DIR="${EPISODES_DIR#\"}"

EP_DIR="$EPISODES_DIR/$EPISODE_ID"

echo "server_log=$SERVER_LOG"
echo "server_started=$STARTED_SERVER"
if [[ -n "$SERVER_PID" ]]; then
  echo "server_pid=$SERVER_PID"
fi
echo "api_base_url=$API_BASE_URL"
echo "episode_id=$EPISODE_ID"
echo "episode_dir=$EP_DIR"
echo "episode_detail=$DETAIL_JSON"

if [[ -d "$EP_DIR" ]]; then
  echo "files:"
  find "$EP_DIR" -maxdepth 2 -type f | sed 's/^/  - /'
else
  echo "files:"
  echo "  - (episode directory not found)"
fi
