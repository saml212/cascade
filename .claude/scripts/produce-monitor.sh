#!/usr/bin/env bash
# /produce background watcher. Given an episode_id, emits structured events to
# /tmp/produce-<id>-events.jsonl whenever something interesting happens:
# stage-started, stage-completed, stage-stalled, error, status-change.
#
# The main agent reads this file at the start of every turn to stay caught up
# on what happened between messages.
set -u
EP="${1:?episode_id required}"
EP_DIR="/Volumes/1TB_SSD/cascade/episodes/$EP"
EVENTS="/tmp/produce-${EP}-events.jsonl"
LOG="/tmp/cascade-server.log"

emit() {
  printf '{"ts":"%s","event":"%s","data":%s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" >> "$EVENTS"
}

emit start "{\"ep\":\"$EP\"}"

last_status=""
last_completed=""
last_log_size=0

while true; do
  [ -d "$EP_DIR" ] || { emit episode-dir-missing "{}"; sleep 10; continue; }

  # Status transitions
  if [ -f "$EP_DIR/episode.json" ]; then
    status=$(jq -r '.status // ""' "$EP_DIR/episode.json" 2>/dev/null)
    completed=$(jq -r '.pipeline.agents_completed | join(",")' "$EP_DIR/episode.json" 2>/dev/null)
    if [ "$status" != "$last_status" ]; then
      emit status-change "{\"from\":\"$last_status\",\"to\":\"$status\"}"
      last_status="$status"
    fi
    if [ "$completed" != "$last_completed" ]; then
      emit stage-completed "{\"agents_completed\":\"$completed\"}"
      last_completed="$completed"
    fi
  fi

  # New errors in log since last check
  if [ -f "$LOG" ]; then
    cur_size=$(stat -f%z "$LOG" 2>/dev/null || echo 0)
    if [ "$cur_size" -gt "$last_log_size" ]; then
      new_errs=$(tail -c $((cur_size - last_log_size)) "$LOG" 2>/dev/null | grep -E "ERROR|Traceback|CRITICAL" | head -5)
      if [ -n "$new_errs" ]; then
        escaped=$(echo "$new_errs" | jq -Rs .)
        emit error "{\"tail\":$escaped}"
      fi
      last_log_size=$cur_size
    fi
  fi

  sleep 10
done
