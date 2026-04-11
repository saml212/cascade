"""Audio enhancement — ML denoising, EQ, dynamics, two-pass loudness normalization.

Applied to the pre-mixed audio_mix.wav before muxing with video.

Pipeline:
1. ML denoise — DeepFilterNet 3 (default) or ClearerVoice-Studio
   Removes wind, HVAC, mic bumps, and other non-stationary noise.
2. ffmpeg static chain — afftdn → adeclick → highpass → lowpass → compressor → deesser
3. Two-pass loudnorm — analysis pass measures actual loudness, normalization pass
   applies linear offset to hit target exactly. Eliminates the ±1-2 LU drift
   single-pass loudnorm produces.

Output target (default): -16 LUFS, -1.0 dBTP, LRA 7
This is the cross-platform safe target — passes Apple ±1 dB gate, satisfies
Spotify/YouTube/Amazon (all of which normalize down to -14).
"""

import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger("cascade")


def enhance_audio(input_path: Path, output_path: Path, config: dict) -> Path:
    """Apply audio enhancement pipeline to a WAV file.

    Two-stage:
    1. ML denoise (DeepFilterNet 3 by default; ClearerVoice if configured) —
       handles non-stationary noise (wind, bumps, transients).
    2. ffmpeg chain (afftdn → adeclick → highpass → lowpass → compressor →
       alimiter → loudnorm) — handles stationary noise, dynamics, loudness.

    Returns path to the enhanced WAV file.
    """
    processing = config.get("processing", {}) if config else {}

    if not processing.get("audio_enhance", True):
        logger.info("Audio enhancement disabled, using raw mix")
        return input_path

    work_dir = input_path.parent
    current_input = input_path

    # Step 1: ML denoise (DeepFilterNet by default, ClearerVoice if specified)
    denoise_model = processing.get("audio_denoise_model", "deepfilternet")
    if denoise_model and denoise_model.lower() != "none":
        denoised_path = work_dir / "audio_mix_denoised.wav"
        if denoise_model.lower() == "deepfilternet":
            success = _apply_deepfilternet(current_input, denoised_path)
        else:
            success = _apply_clearervoice(current_input, denoised_path, denoise_model)
        if success:
            current_input = denoised_path

    # Step 2: ffmpeg static enhancement chain (denoise → eq → dynamics → deesser)
    static_chain = _build_static_filter_chain(processing)

    # Step 3: Two-pass loudnorm (analysis → linear normalization)
    # Pass 1: measure actual loudness through the static chain
    target_lufs = processing.get("audio_target_lufs", -16)
    target_tp = processing.get("audio_target_tp", -1.0)
    target_lra = processing.get("audio_target_lra", 7)

    measured = _measure_loudness(current_input, static_chain, target_lufs, target_tp, target_lra)
    if measured:
        logger.info(
            "Pass 1 measured: I=%s LUFS, LRA=%s LU, TP=%s dBTP",
            measured.get("input_i"), measured.get("input_lra"), measured.get("input_tp"),
        )
        loudnorm = (
            f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}"
            f":measured_I={measured['input_i']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            f":linear=true:print_format=summary"
        )
    else:
        # Pass 1 failed — fall back to single-pass dynamic loudnorm
        logger.warning("Pass 1 measurement failed — using single-pass dynamic loudnorm")
        loudnorm = f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}:print_format=summary"

    af = static_chain + "," + loudnorm if static_chain else loudnorm

    logger.info("Pass 2 enhancing with: %s", af)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(current_input),
        "-af", af,
        "-c:a", "pcm_s16le", "-ar", "48000",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Audio enhancement failed: %s", result.stderr[-500:])
        return current_input  # Fall back to denoised (or raw)

    size_mb = output_path.stat().st_size / 1e6
    logger.info("Enhanced audio: %s (%.1f MB)", output_path.name, size_mb)

    # Clean up intermediate denoised file
    if current_input != input_path and current_input.exists():
        try:
            current_input.unlink()
        except OSError:
            pass

    return output_path


def _measure_loudness(
    input_path: Path,
    static_chain: str,
    target_i: float,
    target_tp: float,
    target_lra: float,
) -> dict | None:
    """Pass 1 of two-pass loudnorm: measure actual loudness through the chain.

    Runs the static enhancement chain + an analysis loudnorm pass with
    print_format=json. Parses the JSON output and returns a dict ready for
    pass 2's measured_* parameters.

    Returns None if the measurement fails — caller should fall back to
    single-pass dynamic loudnorm.
    """
    analysis = (
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    )
    af = static_chain + "," + analysis if static_chain else analysis

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(input_path),
        "-af", af,
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning("Loudnorm pass 1 ffmpeg failed: %s", result.stderr[-300:])
        return None

    # ffmpeg writes the JSON block to stderr (after "Parsed_loudnorm... Output:")
    stderr = result.stderr
    # Find the JSON block — typically last { ... } in stderr
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not matches:
        logger.warning("Loudnorm pass 1 produced no JSON output")
        return None

    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        logger.warning("Loudnorm pass 1 JSON parse failed: %s", e)
        return None

    return data


def _build_static_filter_chain(processing: dict) -> str:
    """Build the static (non-loudnorm) part of the audio filter chain.

    Order matters: denoise → declick → highpass → lowpass → compressor → deesser.
    Loudnorm is added separately by enhance_audio() as a two-pass operation.

    Returns empty string if no filters are configured.
    """
    filters = []

    # afftdn — FFT-based stationary noise reduction. Reduced strength when
    # DeepFilterNet 3 is active (DFN3 already handles non-stationary noise
    # so we only need a light pass for residual hiss).
    denoise_model = processing.get("audio_denoise_model", "deepfilternet")
    if processing.get("audio_afftdn", True):
        if denoise_model and denoise_model.lower() == "deepfilternet":
            filters.append("afftdn=nr=6:nf=-50:tn=1")
        else:
            filters.append("afftdn=nr=12:nf=-40:tn=1")

    # adeclick — autoregressive declicking for mouth clicks, chair squeaks.
    if processing.get("audio_declick", True):
        filters.append("adeclick=threshold=5:burst=2:method=add")

    # Highpass — 4-pole Butterworth removes HVAC/traffic rumble.
    highpass = processing.get("audio_highpass_hz", 80)
    if highpass and highpass > 0:
        filters.append(f"highpass=f={highpass}:p=2")

    # Lowpass — remove high-frequency hiss while preserving "air" for headphones.
    # 16 kHz is the sweet spot: removes hiss above speech but keeps presence.
    lowpass = processing.get("audio_lowpass_hz", 16000)
    if lowpass and lowpass > 0:
        filters.append(f"lowpass=f={lowpass}")

    # Compressor — gentle 3:1 to even out speaker level variation.
    filters.append("acompressor=threshold=-20dB:ratio=3:attack=5:release=50")

    # De-esser — sibilance control at 5-8 kHz where DJI Mic Mini and H6E both
    # capture S/T/Sh frequencies without natural attenuation. Placement is
    # AFTER compressor, BEFORE loudnorm (industry standard).
    if processing.get("audio_deesser", True):
        # i=intensity (0-1), m=max reduction (0-1), f=frequency focus
        # f=0.5 ≈ 6 kHz which works for both male and female voices
        filters.append("deesser=i=0.4:m=0.5:f=0.5")

    # NOTE: alimiter removed. Loudnorm's built-in true-peak limiter handles
    # peak protection cleanly. Stacking a separate limiter before loudnorm
    # double-gates and steals headroom from the loudnorm normalization.

    return ",".join(filters)


# Backwards-compat alias used by tests / external callers
def _build_ffmpeg_enhance_filter(processing: dict) -> str:
    """Compat wrapper that returns the full chain INCLUDING single-pass loudnorm.

    Tests still call this; production now uses _build_static_filter_chain
    plus a separate two-pass loudnorm step.
    """
    static = _build_static_filter_chain(processing)
    target_lufs = processing.get("audio_target_lufs", -16)
    target_lra = processing.get("audio_target_lra", 7)
    target_tp = processing.get("audio_target_tp", -1.0)
    loudnorm = f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}"
    return static + "," + loudnorm if static else loudnorm


def _apply_deepfilternet(input_path: Path, output_path: Path) -> bool:
    """Apply DeepFilterNet 3 ML denoising. Returns True on success.

    DeepFilterNet handles non-stationary noise (wind gusts, mic bumps, sudden
    interruptions) that traditional ffmpeg filters can't touch. Trained on
    DNS Challenge data with non-stationary noise.

    Processes audio in 5-minute chunks to avoid OOM on long podcasts — the
    default enhance() path holds the full audio + intermediate tensors in
    memory, which can exceed 10+ GB for a 90-minute file.

    Operates at 48 kHz native. ~20× realtime per chunk on Apple Silicon CPU.
    """
    try:
        import torch
        from df.enhance import enhance, init_df, load_audio, save_audio
    except ImportError:
        logger.warning(
            "DeepFilterNet not installed — skipping ML denoise. "
            "Install with: pip install deepfilternet 'torch<2.2' 'torchaudio<2.2'"
        )
        return False

    try:
        logger.info("Loading DeepFilterNet 3 model...")
        model, df_state, _ = init_df()
        sr = df_state.sr()

        logger.info("Loading audio from %s...", input_path.name)
        audio, _ = load_audio(str(input_path), sr=sr)
        total_samples = audio.shape[-1]
        total_minutes = total_samples / sr / 60
        logger.info(
            "Loaded %.1f min audio (%d channels), processing in 5-min chunks...",
            total_minutes, audio.shape[0],
        )

        # Process in 5-minute chunks to keep memory bounded.
        # DeepFilterNet's internal STFT has a receptive field of ~1 second, so
        # chunk boundaries shouldn't produce audible artifacts.
        chunk_samples = sr * 300  # 5 minutes
        output_chunks = []
        n_chunks = (total_samples + chunk_samples - 1) // chunk_samples

        for i in range(n_chunks):
            start = i * chunk_samples
            end = min(start + chunk_samples, total_samples)
            chunk = audio[:, start:end]
            with torch.no_grad():
                enhanced_chunk = enhance(model, df_state, chunk)
            output_chunks.append(enhanced_chunk)
            # Free the input chunk aggressively
            del chunk
            logger.info(
                "DeepFilterNet chunk %d/%d (%.1f min → %.1f min)",
                i + 1, n_chunks, start / sr / 60, end / sr / 60,
            )

        # Concatenate along the time dimension
        enhanced = torch.cat(output_chunks, dim=-1)
        del output_chunks
        del audio

        logger.info("Saving denoised audio to %s...", output_path.name)
        save_audio(str(output_path), enhanced, sr)
        del enhanced

        size_mb = output_path.stat().st_size / 1e6
        logger.info("DeepFilterNet complete: %s (%.1f MB)", output_path.name, size_mb)
        return True
    except Exception as e:
        logger.error("DeepFilterNet failed: %s", e)
        import traceback
        logger.error(traceback.format_exc())
        return False


def _apply_clearervoice(input_path: Path, output_path: Path, model_name: str) -> bool:
    """Apply ClearerVoice-Studio ML denoising. Returns True on success.

    ClearerVoice is optional — not in requirements.txt. Users who want ML
    denoising via ClearerVoice install it separately: pip install clearvoice

    Slower than DeepFilterNet but slightly higher quality. Use for "rescue"
    passes on very noisy episodes.
    """
    try:
        import torch
        from clearvoice import ClearVoice
    except ImportError:
        logger.warning(
            "ClearerVoice-Studio not installed — skipping ML denoise. "
            "Install with: pip install clearvoice"
        )
        return False

    try:
        logger.info("Running ClearerVoice %s denoise (slow, may take hours)...", model_name)
        cv = ClearVoice(task="speech_enhancement", model_names=[model_name])
        with torch.no_grad():
            output_wav = cv(input_path=str(input_path), online_write=False)
        cv.write(output_wav, output_path=str(output_path))
        logger.info("ClearerVoice denoise complete: %s", output_path.name)
        return True
    except Exception as e:
        logger.error("ClearerVoice denoise failed: %s", e)
        return False
