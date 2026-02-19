# Cascade Current State (Observed)

## Implemented

- FastAPI backend and static frontend are runnable from `start.sh`.
- Episode records can be created with `POST /api/episodes`.
- Clip metadata workflows exist:
  - list/get clips
  - add manual clips
  - approve/reject clips
  - approve episode
- Schedule and analytics endpoints return basic/default JSON.

## Not Implemented End-to-End Yet

- No full ingest/stitch/transcribe/clip-mining/render pipeline is wired into API episode creation.
- No real publisher adapter execution to YouTube/TikTok/Instagram is currently performed.
- “Trigger publish” endpoint is a placeholder acknowledgement.
- MCP server implementation was not found in the repository.

## Expected Artifacts Today

After creating an episode, typical outputs are metadata files such as:
- `episode.json`
- `clips.json` (after clip actions)

Do not assume generated files like:
- `longform.mp4`
- `shorts/*.mp4`
- transcript/srt files
unless the underlying pipeline code is added.
