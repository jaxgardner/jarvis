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
    pcm = array.array("h", (_quantize(sample) for sample in samples))
    if sys.byteorder == "big":
        # WAV is little-endian regardless of the host. Never true on the Mini,
        # one line to be right anyway.
        pcm.byteswap()

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _quantize(sample: float) -> int:
    if sample >= 1.0:
        return 32767
    if sample <= -1.0:
        return -32768
    return int(sample * 32767)
