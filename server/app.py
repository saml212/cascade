"""Distil API — FastAPI entry point."""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.routes import episodes, clips, publish, analytics

# Project root is the parent of server/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(os.getenv("DISTIL_OUTPUT_DIR", PROJECT_ROOT / "output"))
FRONTEND_DIR = PROJECT_ROOT / "frontend"

app = FastAPI(title="Distil API", version="0.1.0")

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
app.include_router(publish.router)
app.include_router(analytics.router)

# Mount output directory for video file serving
if OUTPUT_DIR.exists():
    app.mount("/media", StaticFiles(directory=str(OUTPUT_DIR)), name="media")

# Mount frontend static files
if FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")


@app.get("/")
async def serve_index():
    """Serve the SPA index page."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{path:path}")
async def spa_catchall(request: Request, path: str):
    """Catch-all: serve index.html for any non-API, non-static path (SPA routing)."""
    # Don't catch API routes or static files
    if path.startswith("api/") or path.startswith("media/") or path.startswith("frontend/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "not found"}, status_code=404)

    # Check if it's an actual static file in frontend/
    static_path = FRONTEND_DIR / path
    if static_path.is_file():
        return FileResponse(static_path)

    # Otherwise serve index.html for SPA routing
    return FileResponse(FRONTEND_DIR / "index.html")
