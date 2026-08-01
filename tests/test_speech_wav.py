"""float32 samples to a WAV file, using nothing but the standard library.

The clipping test is the one that matters. Kokoro occasionally returns
samples a hair outside ±1.0, and the naive `int(sample * 32767)` raises
OverflowError inside array('h') rather than producing quiet distortion —
which means one loud reply would 500 instead of sounding slightly wrong.
"""

import io
import wave

from speech import wav


def read(payload: bytes):
    with wave.open(io.BytesIO(payload), "rb") as handle:
        return handle, handle.readframes(handle.getnframes())


def test_encode_produces_a_readable_mono_wav():
    payload = wav.encode([0.0] * 100, 24_000)

    handle, frames = read(payload)
    assert handle.getnchannels() == 1
    assert handle.getsampwidth() == 2
    assert handle.getframerate() == 24_000
    assert handle.getnframes() == 100
    assert frames == b"\x00\x00" * 100


def test_full_scale_clips_instead_of_overflowing():
    payload = wav.encode([2.0, -2.0, 1.0, -1.0], 24_000)

    _, frames = read(payload)
    values = [
        int.from_bytes(frames[i : i + 2], "little", signed=True)
        for i in range(0, len(frames), 2)
    ]
    assert values == [32767, -32768, 32767, -32768]


def test_empty_input_is_a_valid_empty_wav():
    payload = wav.encode([], 24_000)

    handle, frames = read(payload)
    assert handle.getnframes() == 0
    assert frames == b""
