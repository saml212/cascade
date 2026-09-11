# Server & Frontend

## Server (`server/`)
- FastAPI app bound to `127.0.0.1:8420` by `start.sh` (`server/app.py`).
- Routes in `server/routes/` cover episodes, clips, pipeline control, chat, edits, delivery, scheduling, and trimming.
- `chat.py` is the AI chat route: maintains `chat_history.json`, loads full episode context into a system prompt, parses `action` JSON blocks from the model response to execute operations (approve/reject clips, update metadata, re-render shorts, edit longform, etc.).
- Serves the compiled `frontend/dist/` application with an SPA catch-all.

## Frontend (`frontend/`)
- Vanilla TypeScript SPA with a small signals layer, hash router, Vite, and compiled Tailwind; there is no framework runtime.
- `npm run build` type-checks and writes the production application to `frontend/dist/`.
- `npm run dev` starts the HMR development server on port 8421 and proxies API requests to FastAPI.
