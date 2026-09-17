# Server & Frontend

## Server (`server/`)
- FastAPI app bound to `127.0.0.1:8420` by `start.sh` (`server/app.py`).
- Routes in `server/routes/` cover episodes, clips, pipeline control, edits, delivery, scheduling, and trimming.
- Cascade exposes explicit metadata, review, edit, render, approval, and publishing APIs. External Codex workflows call those contracts directly. Historical `chat_history.json` files remain episode recovery artifacts; the application does not expose or mutate them.
- Serves the compiled `frontend/dist/` application with an SPA catch-all.

## Frontend (`frontend/`)
- Vanilla TypeScript SPA with a small signals layer, hash router, Vite, and compiled Tailwind; there is no framework runtime.
- `npm run build` type-checks and writes the production application to `frontend/dist/`.
- `npm run dev` starts the HMR development server on port 8421 and proxies API requests to FastAPI.
