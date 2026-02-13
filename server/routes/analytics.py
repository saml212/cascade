"""Analytics endpoints."""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = Path(os.getenv("CASCADE_OUTPUT_DIR", PROJECT_ROOT / "output"))
DATA_DIR = PROJECT_ROOT / "data"
EPISODES_DIR = OUTPUT_DIR / "episodes"


@router.get("/")
async def get_analytics() -> dict:
    """Aggregate analytics dashboard data."""
    # Try data/analytics.json
    analytics_file = DATA_DIR / "analytics.json"
    if analytics_file.exists():
        try:
            with open(analytics_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Try loading scoring weights
    scoring_weights = {}
    weights_file = DATA_DIR / "scoring_weights.json"
    if weights_file.exists():
        try:
            with open(weights_file) as f:
                scoring_weights = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # Default response — no analytics data yet
    return {
        "clips": [],
        "scoring_weights": scoring_weights or {
            "llm_virality": 0.30,
            "engagement_prediction": 0.20,
            "quotability": 0.12,
            "audio_energy": 0.08,
            "speaker_dynamics": 0.08,
            "topic_coherence": 0.07,
            "vocal_emphasis": 0.05,
            "laughter_detection": 0.04,
            "qa_pairs": 0.03,
            "boundary_quality": 0.03,
        },
        "last_collection": None,
        "episodes_tracked": 0,
        "weight_adjustments": 0,
    }


@router.get("/{episode_id}")
async def get_episode_analytics(episode_id: str) -> dict:
    """Get analytics for a specific episode."""
    ep_dir = EPISODES_DIR / episode_id
    if not ep_dir.exists():
        raise HTTPException(status_code=404, detail=f"Episode {episode_id} not found")

    # Try episode-level analytics
    analytics_file = ep_dir / "analytics.json"
    if analytics_file.exists():
        try:
            with open(analytics_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "episode_id": episode_id,
        "clips": [],
        "total_views": 0,
        "total_engagement": 0,
        "platforms": {},
    }
