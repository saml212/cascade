"""Cascade agents package — podcast automation pipeline."""

from agents.ingest import IngestAgent
from agents.stitch import StitchAgent
from agents.audio_analysis import AudioAnalysisAgent
from agents.speaker_cut import SpeakerCutAgent
from agents.transcribe import TranscribeAgent
from agents.clip_miner import ClipMinerAgent
from agents.longform_render import LongformRenderAgent
from agents.shorts_render import ShortsRenderAgent
from agents.metadata_gen import MetadataGenAgent
from agents.thumbnail_gen import ThumbnailGenAgent
from agents.qa import QAAgent
from agents.podcast_feed import PodcastFeedAgent
from agents.video_feed import LongformVideoFeedAgent, VideoFeedAgent
from agents.publish import LongformPublishAgent, PublishAgent
from agents.backup import BackupAgent

AGENT_REGISTRY = {
    "ingest": IngestAgent,
    "stitch": StitchAgent,
    "audio_analysis": AudioAnalysisAgent,
    "speaker_cut": SpeakerCutAgent,
    "transcribe": TranscribeAgent,
    "clip_miner": ClipMinerAgent,
    "longform_render": LongformRenderAgent,
    "shorts_render": ShortsRenderAgent,
    "metadata_gen": MetadataGenAgent,
    "thumbnail_gen": ThumbnailGenAgent,
    "qa": QAAgent,
    "podcast_feed": PodcastFeedAgent,
    "video_feed": VideoFeedAgent,
    "publish": PublishAgent,
    "longform_publish": LongformPublishAgent,
    "longform_video_feed": LongformVideoFeedAgent,
    "backup": BackupAgent,
}

PIPELINE_ORDER = [
    "ingest",
    "stitch",
    "audio_analysis",
    "speaker_cut",
    "transcribe",
    "clip_miner",
    "longform_render",
    "shorts_render",
    "metadata_gen",
    "thumbnail_gen",
    "qa",
    "podcast_feed",
    "video_feed",
    "publish",
    "longform_video_feed",
    "longform_publish",
    "backup",
]
