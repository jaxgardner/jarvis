"""PCM16 WAV, from the standard library.

`soundfile` does this in one line and brings libsndfile onto the machine to
do it. This is ten lines and brings nothing.
"""

import array
import io
import sys
import wave
from collections.abc import Iterable


def encode(samples: Iterable[float], sample_rate: int) -> bytes:
    """Float samples in [-1.0, 1.0] to a complete mono WAV file.

    Out-of-range samples clip rather than raising. Kokoro returns the
    occasional sample a hair past full scale, and a reply that is imperceptibly
    distorted beats a reply that 500s.
    """
    return encode_pcm16(pcm16(samples), sample_rate)


def pcm16(samples: Iterable[float]) -> bytes:
    """Float samples to int16 little-endian bytes, without a WAV header.

    Split out from `encode` so audio can be concatenated before it is framed:
    a reply synthesized in chunks is several buffers that have to become one
    file, and joining headers is not a thing you can do.
    """
    pcm = array.array("h", (_quantize(sample) for sample in samples))
    if sys.byteorder == "big":
        # WAV is little-endian regardless of the host. Never true on the Mini,
        # one line to be right anyway.
        pcm.byteswap()
    return pcm.tobytes()


def encode_pcm16(pcm: bytes, sample_rate: int) -> bytes:
    """Already-quantized int16 little-endian samples to a complete mono WAV.

    For engines that hand back integers rather than floats — Piper does, as
    `AudioChunk.audio_int16_bytes`. Passing those through `encode` instead
    would treat each sample as a float in [-1.0, 1.0] and clip all of them to
    full scale: silence stays silent and everything else becomes a square
    wave, with no exception raised anywhere. This function is the header and
    nothing else, which is exactly the point.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def _quantize(sample: float) -> int:
    if sample >= 1.0:
        return 32767
    if sample <= -1.0:
        return -32768
    return int(sample * 32767)
