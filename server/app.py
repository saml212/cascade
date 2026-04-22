"""Cascade API — FastAPI entry point."""

import os
from pathlib import Path

# Load .env BEFORE importing routes (they read CASCADE_OUTPUT_DIR at import time)
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.routes import episodes, clips, pipeline, chat, trim, schedule, edits

# Project root is the parent of server/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_output_env = os.getenv("CASCADE_OUTPUT_DIR", "")
if _output_env:
    # Env var points directly to episodes dir — parent is the cascade root
    OUTPUT_DIR = Path(_output_env).parent
else:
    OUTPUT_DIR = PROJECT_ROOT / "output"
# Frontend layout:
#   frontend/          — new Vite-built SPA (dist/ is the actual served tree)
#   frontend-legacy/   — previous vanilla-JS monolith, kept for emergency rollback
# Set CASCADE_LEGACY_UI=1 to serve frontend-legacy/ instead of frontend/dist/.
_use_legacy = os.getenv("CASCADE_LEGACY_UI", "").lower() in {"1", "true", "yes"}
_new_dist = PROJECT_ROOT / "frontend" / "dist"
_legacy_dir = PROJECT_ROOT / "frontend-legacy"

if _use_legacy and _legacy_dir.exists():
    FRONTEND_DIR = _legacy_dir
    FRONTEND_IS_BUILT = False
elif _new_dist.exists():
    FRONTEND_DIR = _new_dist
    FRONTEND_IS_BUILT = True
elif _legacy_dir.exists():
    # New UI not built yet — fall back to legacy so the site still renders.
    FRONTEND_DIR = _legacy_dir
    FRONTEND_IS_BUILT = False
else:
    FRONTEND_DIR = PROJECT_ROOT / "frontend"
    FRONTEND_IS_BUILT = False

app = FastAPI(title="Cascade API", version="0.1.0")

# CORS — allow all for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(episodes.router)
app.include_router(clips.router)
app.include_router(pipeline.router)
app.include_router(chat.router)
app.include_router(trim.router)
app.include_router(schedule.router)
app.include_router(edits.router)

# Mount output directory for video file serving
if OUTPUT_DIR.exists():
    app.mount("/media", StaticFiles(directory=str(OUTPUT_DIR)), name="media")

# Mount frontend static files (Vite dist/ or legacy)
if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")
    # Vite emits hashed bundles under /assets — mount so they resolve directly.
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/")
async def serve_index():
    """Serve the SPA index page."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{path:path}")
async def spa_catchall(path: str):
    """Catch-all: serve index.html for any non-API, non-static path (SPA routing)."""
    if path.startswith(("api/", "media/", "frontend/", "assets/")):
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": "not found"}, status_code=404)

    static_path = FRONTEND_DIR / path
    if static_path.is_file():
        return FileResponse(static_path)

    return FileResponse(FRONTEND_DIR / "index.html")
