"""Cascade API — FastAPI entry point."""

import os
from pathlib import Path

# Load .env BEFORE importing routes (they read CASCADE_OUTPUT_DIR at import time)
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from server.routes import (
    chat,
    clips,
    delivery,
    edits,
    episodes,
    pipeline,
    quality,
    review,
    schedule,
    source_recovery,
    trim,
    video_feed,
    watch_links,
)

# Project root is the parent of server/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_output_env = os.getenv("CASCADE_OUTPUT_DIR", "")
if _output_env:
    # Env var points directly to episodes dir — parent is the cascade root
    OUTPUT_DIR = Path(_output_env).parent
else:
    OUTPUT_DIR = PROJECT_ROOT / "output"
FRONTEND_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_IS_BUILT = (FRONTEND_DIR / "index.html").is_file()

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
app.include_router(delivery.router)
app.include_router(quality.router)
app.include_router(review.router)
app.include_router(source_recovery.router)
app.include_router(video_feed.router)
app.include_router(watch_links.router)

# Mount output directory for video file serving
if OUTPUT_DIR.exists():
    app.mount("/media", StaticFiles(directory=str(OUTPUT_DIR)), name="media")

# Mount the compiled Vite application when available.
if FRONTEND_IS_BUILT:
    app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR)), name="frontend")
    # Vite emits hashed bundles under /assets — mount so they resolve directly.
    assets_dir = FRONTEND_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.get("/")
async def serve_index():
    """Serve the SPA index page."""
    if not FRONTEND_IS_BUILT:
        return _frontend_not_built()
    return FileResponse(FRONTEND_DIR / "index.html")


def _frontend_not_built() -> HTMLResponse:
    return HTMLResponse(
        "<h1>Cascade frontend is not built</h1>"
        "<p>Run <code>npm --prefix frontend ci</code>, then "
        "<code>npm --prefix frontend run build</code>.</p>",
        status_code=503,
    )


@app.get("/{path:path}")
async def spa_catchall(path: str):
    """Catch-all: serve index.html for any non-API, non-static path (SPA routing).

    API paths: if the real route exists at `<path>/` (trailing-slash form),
    redirect. Otherwise return 404. FastAPI's built-in redirect_slashes
    doesn't fire here because this catchall matches before it can run.
    """
    if path.startswith(("api/", "media/", "frontend/", "assets/")):
        # api/ paths should never hit the SPA. If the path exists in the
        # real FastAPI route table at `<path>/` (trailing-slash form),
        # redirect — otherwise 404. This emulates FastAPI's redirect_slashes
        # behavior which the catchall would otherwise block.
        from fastapi.responses import JSONResponse, RedirectResponse

        if path.startswith("api/") and not path.endswith("/"):
            slashed = f"/{path}/"
            for route in app.router.routes:
                if getattr(route, "path", None) == slashed:
                    return RedirectResponse(url=slashed, status_code=307)

        return JSONResponse({"error": "not found"}, status_code=404)

    if not FRONTEND_IS_BUILT:
        return _frontend_not_built()

    static_path = FRONTEND_DIR / path
    if static_path.is_file():
        return FileResponse(static_path)

    return FileResponse(FRONTEND_DIR / "index.html")
