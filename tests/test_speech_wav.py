"""float32 samples to a WAV file, using nothing but the standard library.

The clipping test is the one that matters. Kokoro occasionally returns
samples a hair outside ±1.0, and the naive `int(sample * 32767)` raises
OverflowError inside array('h') rather than producing quiet distortion —
which means one loud reply would 500 instead of sounding slightly wrong.

`encode_pcm16` exists for engines that hand back int16 already. Routing
those through `encode` would quantize them a second time — see its own test.
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


def test_encode_pcm16_wraps_bytes_without_touching_them():
    """The samples come out the far side bit-identical.

    Piper hands back int16 little-endian bytes. The only job here is a header;
    anything that rescales or re-quantizes them is a bug.
    """
    pcm = b"\x00\x00\xff\x7f\x00\x80\x01\x00"

    payload = wav.encode_pcm16(pcm, 22_050)

    handle, frames = read(payload)
    assert handle.getnchannels() == 1
    assert handle.getsampwidth() == 2
    assert handle.getframerate() == 22_050
    assert handle.getnframes() == 4
    assert frames == pcm


def test_int16_through_the_float_encoder_would_be_silence():
    """Why `encode_pcm16` is a separate function and not a convenience.

    `encode` expects floats in [-1.0, 1.0]. Feeding it integers — which is
    what you get from iterating Piper's output as numbers rather than bytes —
    clips every non-zero sample to full scale instead of raising. The failure
    is audible noise, not a traceback, so it is worth a test that names it.
    """
    as_ints = [0, 32767, -32768, 1]

    payload = wav.encode(as_ints, 22_050)

    _, frames = read(payload)
    values = [
        int.from_bytes(frames[i : i + 2], "little", signed=True)
        for i in range(0, len(frames), 2)
    ]
    assert values == [0, 32767, -32768, 32767]  # the trailing 1 is now full scale
