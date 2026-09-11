"""Create a reviewable longform thumbnail from real episode footage."""

from __future__ import annotations

from agents.base import BaseAgent
from lib.thumbnail import render_episode_thumbnail


class ThumbnailGenAgent(BaseAgent):
    name = "thumbnail_gen"

    def execute(self) -> dict:
        source = self.episode_dir / "source_merged.mp4"
        episode = self.load_json_safe("episode.json")
        episode_info = self.load_json_safe("episode_info.json")
        settings = self.config.get("thumbnail", {})

        headline = (
            episode.get("thumbnail_headline")
            or episode_info.get("thumbnail_headline")
            or episode.get("episode_name")
            or episode_info.get("episode_title")
            or episode.get("title")
        )
        if not headline:
            raise ValueError(
                "Set thumbnail_headline or an episode title before rendering a thumbnail"
            )

        output = self.episode_dir / "thumbnails" / "longform.jpg"
        self.report_progress(1, 2, "Extracting a source-frame thumbnail")
        provenance = render_episode_thumbnail(
            source,
            output,
            str(headline),
            at_seconds=float(
                episode.get(
                    "thumbnail_frame_seconds",
                    settings.get("frame_seconds", 600.0),
                )
            ),
            podcast_name=str(
                self.config.get("podcast", {}).get("title", "The Local Podcast")
            ),
            font_path=settings.get("font_path") or None,
            title_position=str(
                episode.get(
                    "thumbnail_title_position",
                    settings.get("title_position", "top"),
                )
            ),
        )
        self.report_progress(2, 2, "Thumbnail ready for review")
        self.save_json("thumbnail_gen.json", provenance)
        return provenance
