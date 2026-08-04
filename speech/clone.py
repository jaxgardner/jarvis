"""Voice conversion: Piper's audio, wearing the reference clip's voice.

Piper supplies the words, the accent and the timing. This supplies the timbre.
Both halves are needed and neither is sufficient: conversion keeps the source's
prosody, so driving it from an American voice produced American phrasing in
the right timbre — audibly not the assistant — and Piper alone produced the
right phrasing at 22.05kHz with the artifacts of a small finetune. Converting
Piper's own output is what removed both problems at once.

The model is Kanade, a neural codec. It encodes the source to a mel
representation conditioned on a reference speaker, and a vocoder turns that
back into audio at 24kHz — which is *why* conversion sounds cleaner than its
input rather than dirtier, despite being a lossy round trip. The reconstruction
is drawn from the codec's training distribution, not from Piper's.

Weights are local. `KanadeModel.from_pretrained` takes explicit paths, and
TORCH_HOME is pointed at the same directory before torchaudio is imported, so
the whole engine is on disk under TTS_MODEL_DIR and the filesystem stays the
switch. Nothing here reaches the network; a machine with no weights answers
`available()` False and /speech 503s, exactly as it did with Kokoro.
"""

import threading

from app import config

# The mel decoder's RoPE table is precomputed for 1024 positions at a hop of
# 256 samples, so one pass can cover (1024 - 1) * 256 samples and no more.
# Past that the model does not fail — it silently attends to positions it has
# no embedding for, which is the kind of wrong that reaches the phone sounding
# merely odd. Derived from KokoClone's chunked_convert (Apache 2.0), which
# documents the constraint; the windowing below is the same idea.
_ROPE_MAX_FRAMES = 1024
_HOP_LENGTH = 256
_MAX_WINDOW = (_ROPE_MAX_FRAMES - 1) * _HOP_LENGTH

# Context carried either side of a window so the seam has something to blend
# against. Trimmed off the mel before the pieces are joined.
_OVERLAP_SECONDS = 0.5

# A 25% margin under the ceiling. The limit is exact but the frame count
# depends on centre-padding, and being one frame over is not worth the risk.
_SAFETY = 0.75

_lock = threading.RLock()
_engine = None


def paths():
    """`(config, weights, reference)` — the three files this needs on disk."""
    kanade = config.TTS_MODEL_DIR / "kanade"
    return (
        kanade / "config.yaml",
        kanade / "model.safetensors",
        config.TTS_MODEL_DIR / config.TTS_REFERENCE,
    )


def files_present() -> bool:
    return all(path.exists() for path in paths())


def loaded() -> bool:
    return _engine is not None


def engine():
    """The converter, built once.

    Reentrant lock for the same reason `synth._lock` is one: `convert` holds it
    across this call, which takes it again to build lazily. The reference clip
    is decoded once here rather than per utterance — it never changes, and
    decoding an mp3 on every clause would be pure waste on the hot path.
    """
    global _engine
    with _lock:
        if _engine is None:
            config_path, weights_path, reference = paths()

            # Before torchaudio, which resolves its download cache at import.
            # WavLM ships as a torch-hub checkpoint rather than through
            # from_pretrained, so this is the only way to keep it local.
            import os

            os.environ.setdefault("TORCH_HOME", str(config.TTS_MODEL_DIR / "torch"))

            from kanade_tokenizer import KanadeModel, load_audio, load_vocoder

            model = KanadeModel.from_pretrained(
                config_path=str(config_path), weights_path=str(weights_path)
            ).eval()
            vocoder = load_vocoder(model.config.vocoder_name)
            rate = model.config.sample_rate
            _engine = (model, vocoder, load_audio(str(reference), sample_rate=rate), rate)
        return _engine


def rate() -> int:
    """The sample rate conversion emits, which is not the rate it consumes."""
    return engine()[3]


def from_pcm16(pcm: bytes, source_rate: int):
    """int16 bytes at `source_rate` to a float tensor at the model's rate.

    Piper speaks at 22.05kHz and Kanade works at its own, so something has to
    resample. Doing it here in torch rather than by round-tripping a temporary
    WAV through the loader — which is the obvious shortcut — keeps a file write
    and a decode off the hot path of every clause.
    """
    import numpy as np
    import torch
    import torchaudio

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    waveform = torch.from_numpy(samples.copy())
    target = rate()
    if source_rate == target:
        return waveform
    return torchaudio.functional.resample(waveform, source_rate, target)


def to_pcm16(samples) -> bytes:
    """A float tensor back to int16 bytes, clipped rather than wrapped.

    The vocoder occasionally overshoots ±1.0. Letting that wrap would turn a
    loud syllable into a click, which is worse than the distortion clipping
    costs — the same trade `wav.encode` makes for Kokoro.
    """
    import torch

    clamped = torch.clamp(samples, -1.0, 1.0)
    return (clamped * 32767.0).to(torch.int16).numpy().tobytes()


def convert(samples):
    """`samples` (a float tensor at `rate()`) in the reference voice.

    Returns a 1-D CPU float32 tensor. Windowed only when the source is longer
    than one RoPE pass, which for clause-sized chunks it never is — the whole
    streaming path takes the single-pass branch, and the windowing exists for
    `TTS_STREAM_CHUNKS=0`, where a long reply arrives in one piece.
    """
    import torch

    from kanade_tokenizer import vocode

    model, vocoder, reference, sample_rate = engine()
    overlap = int(_OVERLAP_SECONDS * sample_rate)
    window = int((_MAX_WINDOW - 2 * overlap) * _SAFETY)

    with _lock, torch.inference_mode():
        if samples.shape[-1] <= window:
            mel = model.voice_conversion(
                source_waveform=samples, reference_waveform=reference
            )
            return vocode(vocoder, mel.unsqueeze(0)).squeeze().cpu()

        mel = _windowed_mel(model, samples, reference, window, overlap)
        return vocode(vocoder, mel.unsqueeze(0)).squeeze().cpu()


def _windowed_mel(model, samples, reference, window, overlap):
    """Mel for a source too long for one pass, joined from overlapping windows.

    Each window is converted with context on both sides and the context is
    trimmed from the mel before joining, so the model always had neighbours to
    condition on but no frame is emitted twice. Vocoding happens once, on the
    assembled mel, because a seam in mel space is far less audible than one
    between two independently vocoded waveforms.
    """
    import torch

    total = samples.shape[-1]
    overlap_frames = overlap // _HOP_LENGTH
    parts = []
    position = 0

    while position < total:
        start = max(0, position - overlap)
        end = min(total, position + window + overlap)
        piece = model.voice_conversion(
            source_waveform=samples[..., start:end], reference_waveform=reference
        ).cpu()

        left = 0 if position == 0 else overlap_frames
        right = piece.shape[-1] if end >= total else piece.shape[-1] - overlap_frames
        parts.append(piece[..., left:right])
        position += window

    return torch.cat(parts, dim=-1)
