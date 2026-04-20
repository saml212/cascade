# Server & Frontend

## Server (`server/`)
- FastAPI app on port 8420 (`server/app.py`).
- Routes in `server/routes/`: `episodes.py`, `clips.py`, `pipeline.py`, `chat.py`, `publish.py`, `analytics.py`, `trim.py`.
- `chat.py` is the AI chat route: maintains `chat_history.json`, loads full episode context into a system prompt, parses `action` JSON blocks from the model response to execute operations (approve/reject clips, update metadata, re-render shorts, edit longform, etc.).
- Serves `frontend/` as static files with SPA catch-all.

## Frontend (`frontend/`)
- Vanilla JS SPA — no framework, no build step.
- Served directly as static files by FastAPI.
- Jest tests run from `frontend/` with `npm test` (jsdom).
