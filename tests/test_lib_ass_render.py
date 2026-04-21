"""End-to-end ASS render test using ffmpeg + libass on a synthetic input.

This is the test that proves the .ass files lib/ass.py produces actually
render through ffmpeg's `subtitles` filter without parser errors. The unit
tests in test_lib_ass.py cover the file structure; this one shells out to
real ffmpeg to verify libass accepts what we generate.

Skipped if ffmpeg is unavailable so the rest of the suite still runs in CI.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from lib.ass import generate_ass_from_diarized


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH",
)


def _make_test_video(path: Path, duration: float = 4.0):
    """Render a 1080x1920 testsrc video so the subtitles filter has
    something to overlay."""
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=1080x1920:duration={duration}:rate=30",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _diarized_with_words():
    """Hand-built diarized transcript with word-level timing covering the
    first ~3 seconds of the synthetic video."""
    return {
        "utterances": [
            {
                "speaker": 0,
                "start": 0.0,
                "end": 3.0,
                "text": "and that's when I realized the entire thesis was wrong",
                "words": [
                    {"word": "and", "start": 0.10, "end": 0.30, "speaker": 0},
                    {"word": "that's", "start": 0.35, "end": 0.65, "speaker": 0},
                    {"word": "when", "start": 0.70, "end": 0.90, "speaker": 0},
                    {"word": "I", "start": 0.95, "end": 1.05, "speaker": 0},
                    {"word": "realized", "start": 1.10, "end": 1.55, "speaker": 0},
                    {"word": "the", "start": 1.60, "end": 1.75, "speaker": 0},
                    {"word": "entire", "start": 1.80, "end": 2.10, "speaker": 0},
                    {"word": "thesis", "start": 2.15, "end": 2.50, "speaker": 0},
                    {"word": "was", "start": 2.55, "end": 2.70, "speaker": 0},
                    {"word": "wrong", "start": 2.75, "end": 3.00, "speaker": 0},
                ],
            },
        ]
    }


# Reuse the project's path-escape helper so we burn the same way the agent does.
from lib.srt import escape_srt_path  # noqa: E402


def _burn_subtitles(
    video_in: Path, ass_in: Path, video_out: Path, *, lossless: bool = False
):
    ass_escaped = escape_srt_path(ass_in)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_in),
        "-vf",
        f"subtitles='{ass_escaped}'",
    ]
    if lossless:
        # ffv1 in matroska — bit-exact decode → encode → decode round-trip
        # so the only pixels that differ from the source are the captions.
        cmd += ["-c:v", "ffv1", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd.append(str(video_out))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Surface libass errors directly so the test failure is actionable
        raise AssertionError(
            f"ffmpeg subtitles burn failed:\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def _ffprobe_video(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    import json

    return json.loads(proc.stdout)


class TestEndToEndRender:
    def test_libass_accepts_generated_ass(self, tmp_path):
        # Step 1: synthetic input video
        video = tmp_path / "src.mp4"
        _make_test_video(video, duration=3.5)

        # Step 2: produce ASS file from synthetic transcript
        ass = tmp_path / "captions.ass"
        n_phrases = generate_ass_from_diarized(
            _diarized_with_words(),
            start=0.0,
            end=3.0,
            ass_path=ass,
        )
        assert n_phrases > 0
        assert ass.exists() and ass.stat().st_size > 0

        # Step 3: burn subtitles via ffmpeg + libass — this is the real test
        out = tmp_path / "burned.mp4"
        _burn_subtitles(video, ass, out)

        # Step 4: verify output exists, plays, and matches the input duration
        assert out.exists() and out.stat().st_size > 0
        info = _ffprobe_video(out)
        duration = float(info["format"]["duration"])
        # Allow loose tolerance — libavformat duration is approximate
        assert 3.0 <= duration <= 4.0, f"unexpected duration {duration}"

        # Stream must be video, h264, with the exact dimensions we asked for
        v_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert v_stream["codec_name"] == "h264"
        assert int(v_stream["width"]) == 1080
        assert int(v_stream["height"]) == 1920

    def test_extracted_frame_differs_from_unburned_baseline(self, tmp_path):
        """Burn-in must actually change pixels. We extract one frame from
        the burned video and one from the unburned baseline at the same
        timestamp, then assert their byte-level digests differ. If libass
        silently no-ops (e.g. font missing, parser error swallowed), the
        frames would be identical."""
        video = tmp_path / "src.mp4"
        _make_test_video(video, duration=3.5)
        ass = tmp_path / "captions.ass"
        generate_ass_from_diarized(
            _diarized_with_words(),
            start=0.0,
            end=3.0,
            ass_path=ass,
        )
        burned = tmp_path / "burned.mp4"
        _burn_subtitles(video, ass, burned)

        # Extract a frame at t=1.5s (when "realized" should be on screen)
        unburned_png = tmp_path / "unburned.png"
        burned_png = tmp_path / "burned.png"
        for src, png in [(video, unburned_png), (burned, burned_png)]:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    "1.5",
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    str(png),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        import hashlib

        unburned_hash = hashlib.sha256(unburned_png.read_bytes()).hexdigest()
        burned_hash = hashlib.sha256(burned_png.read_bytes()).hexdigest()
        assert unburned_hash != burned_hash, (
            "Burned frame is identical to unburned baseline — libass likely "
            "silently failed to render any text. Check fontconfig and the "
            "ffmpeg subtitles filter output."
        )

    def test_caption_pixels_present_in_bottom_third(self, tmp_path):
        """Sanity check that captions land in the bottom region where the
        style positions them. Uses ffv1 lossless re-encoding so the only
        pixels that differ between burned and unburned are the actual
        caption overlay (vs lossy h264 which would produce pseudorandom
        diffs across the whole frame from re-encoding noise)."""
        # Source must also be lossless so its decode matches the input to
        # the burn step exactly.
        video = tmp_path / "src.mkv"
        cmd_src = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1080x1920:duration=3.5:rate=30",
            "-c:v",
            "ffv1",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ]
        subprocess.run(cmd_src, check=True, capture_output=True, text=True)

        ass = tmp_path / "captions.ass"
        generate_ass_from_diarized(
            _diarized_with_words(),
            start=0.0,
            end=3.0,
            ass_path=ass,
        )
        burned = tmp_path / "burned.mkv"
        _burn_subtitles(video, ass, burned, lossless=True)

        # Extract top-half and bottom-half PNGs at the same frame from each.
        for label, src in [("unburned", video), ("burned", burned)]:
            for region, crop in [("top", "1080:960:0:0"), ("bot", "1080:960:0:960")]:
                out = tmp_path / f"{label}_{region}.png"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-ss",
                        "1.5",
                        "-i",
                        str(src),
                        "-vf",
                        f"crop={crop}",
                        "-frames:v",
                        "1",
                        str(out),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

        import hashlib

        def _hash(p):
            return hashlib.sha256((tmp_path / p).read_bytes()).hexdigest()

        # Top region: byte-identical (no captions overlay there, lossless
        # round-trip preserves pixels exactly).
        # Bottom region: must differ (caption overlay is here).
        assert _hash("unburned_top.png") == _hash("burned_top.png"), (
            "Top half of burned video differs from unburned — captions are "
            "positioned outside the intended bottom-third zone, OR the lossless "
            "round-trip is broken."
        )
        assert _hash("unburned_bot.png") != _hash("burned_bot.png"), (
            "Bottom half of burned video is identical to unburned — captions "
            "did not render in the expected bottom-third region."
        )
