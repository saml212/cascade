#!/usr/bin/env bash
set -euo pipefail

# Infer repo root from this script location: skills/<skill>/scripts/<script>.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DEFAULT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SOURCE_PATH=""
REPO_PATH="$REPO_DEFAULT"
MANUAL_CLIP="false"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8420}"

usage() {
  cat <<USAGE
Usage: $0 --source <path> [--repo <path>] [--manual-clip]

Options:
  --source <path>   Raw footage file or folder path (required)
  --repo <path>     Cascade repo path (default: $REPO_DEFAULT)
  --manual-clip     Add/approve a sample manual clip after episode creation

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
    --manual-clip)
      MANUAL_CLIP="true"
      shift
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

if [[ ! -x "./start.sh" ]]; then
  echo "ERROR: start.sh not executable in $REPO_PATH" >&2
  exit 1
fi

mkdir -p logs
LOG_FILE="logs/skill-run-$(date +%Y%m%d-%H%M%S).log"

./start.sh >"$LOG_FILE" 2>&1 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Wait for server readiness (max 45s)
READY="false"
for _ in $(seq 1 45); do
  if curl -sS "$API_BASE_URL/api/episodes" >/dev/null 2>&1; then
    READY="true"
    break
  fi
  sleep 1
done

if [[ "$READY" != "true" ]]; then
  echo "ERROR: server did not become ready. See $REPO_PATH/$LOG_FILE" >&2
  exit 1
fi

create_tmp="$(mktemp)"
create_status=$(curl -sS -o "$create_tmp" -w '%{http_code}' -X POST "$API_BASE_URL/api/episodes" \
  -H 'content-type: application/json' \
  -d "{\"source_path\":\"$SOURCE_PATH\"}" || true)
CREATE_JSON="$(cat "$create_tmp")"
rm -f "$create_tmp"

if [[ ! "$create_status" =~ ^2 ]]; then
  echo "ERROR: episode create failed (HTTP $create_status): $CREATE_JSON" >&2
  echo "server_log=$REPO_PATH/$LOG_FILE" >&2
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
  echo "server_log=$REPO_PATH/$LOG_FILE" >&2
  exit 1
fi

if [[ "$MANUAL_CLIP" == "true" ]]; then
  curl -sS -X POST "$API_BASE_URL/api/episodes/$EPISODE_ID/clips/manual" \
    -H 'content-type: application/json' \
    -d '{"start_seconds":30,"end_seconds":75}' >/dev/null

  CLIPS_JSON=$(curl -sS "$API_BASE_URL/api/episodes/$EPISODE_ID/clips" || true)
  CLIP_ID=$(printf '%s' "$CLIPS_JSON" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
clips = data.get("clips", [])
print((clips[0] or {}).get("id", "") if clips else "")')

  if [[ -n "$CLIP_ID" ]]; then
    curl -sS -X POST "$API_BASE_URL/api/episodes/$EPISODE_ID/clips/$CLIP_ID/approve" >/dev/null || true
  fi
fi

OUTPUT_DIR=$(grep -E '^CASCADE_OUTPUT_DIR=' .env 2>/dev/null | cut -d= -f2- || true)
if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$REPO_PATH/output"
fi
OUTPUT_DIR="${OUTPUT_DIR%\"}"
OUTPUT_DIR="${OUTPUT_DIR#\"}"

EP_DIR="$OUTPUT_DIR/episodes/$EPISODE_ID"

echo "server_log=$REPO_PATH/$LOG_FILE"
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
