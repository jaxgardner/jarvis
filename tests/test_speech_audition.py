"""The audition's loudness matching.

Nothing here runs in the server — this is a listening tool. It is tested
because the levelling is the one part that can be wrong without being
obvious: if it flattened dynamics, the `-seams` files would stop showing the
seams they exist to show, and the audition would quietly answer the wrong
question.
"""

import array
import wave

from speech import audition


def pcm(*values):
    return array.array("h", values).tobytes()


def samples(payload):
    values = array.array("h")
    values.frombytes(payload)
    return list(values)


def test_one_gain_for_the_whole_voice_not_per_file():
    """A quiet take stays quiet relative to a loud one.

    The gain comes from the loudest moment across every take, so the quiet
    file is scaled by the same factor rather than brought up to meet it.
    """
    takes = {
        "loud": (pcm(11000, -11000), 24_000),
        "quiet": (pcm(1100, -1100), 24_000),
    }

    out = audition.leveled(takes)

    assert samples(out["loud"][0]) == [22000, -22000]
    assert samples(out["quiet"][0]) == [2200, -2200]


def test_dynamics_inside_a_take_survive():
    """Within one file, the ratio between samples is unchanged."""
    takes = {"one": (pcm(2000, 4000, 8000), 22_050)}

    out = audition.leveled(takes)

    scaled = samples(out["one"][0])
    assert scaled[2] == 22000
    assert scaled[1] == scaled[2] // 2
    assert scaled[0] == scaled[2] // 4


def test_the_loudest_take_lands_on_target_without_clipping():
    takes = {"a": (pcm(32000), 22_050), "b": (pcm(-32000), 22_050)}

    out = audition.leveled(takes)

    assert samples(out["a"][0]) == [audition.TARGET_PEAK]
    assert samples(out["b"][0]) == [-audition.TARGET_PEAK]


def test_sample_rate_is_carried_through_untouched():
    """Engines differ here — Kokoro is 24kHz, Piper 22.05kHz."""
    takes = {"k": (pcm(100), 24_000), "p": (pcm(100), 22_050)}

    out = audition.leveled(takes)

    assert out["k"][1] == 24_000
    assert out["p"][1] == 22_050


def test_silence_is_returned_rather_than_divided_by_zero():
    takes = {"quiet": (pcm(0, 0), 24_000)}

    assert audition.leveled(takes) == takes


class FakeKokoro:
    """Just the one method `blended_style` uses."""

    def __init__(self, **voices):
        self.voices = voices

    def get_voice_style(self, name):
        return self.voices[name]


def test_a_blend_is_a_weighted_average_not_a_sum():
    """Weights are normalized, so the result stays in style-vector scale.

    Two voices at 1.0 each must not produce a vector at twice the magnitude
    of either — that is not a blend of two speakers, it is one blend at 2x,
    and the style vector is not a volume control.
    """
    import numpy as np

    engine = FakeKokoro(
        a=np.array([1.0, 0.0], dtype=np.float32),
        b=np.array([0.0, 1.0], dtype=np.float32),
    )

    style = audition.blended_style(engine, {"a": 1.0, "b": 1.0})

    assert list(style) == [0.5, 0.5]


def test_unnormalized_weights_match_their_normalized_equal():
    import numpy as np

    engine = FakeKokoro(
        a=np.array([1.0, 0.0], dtype=np.float32),
        b=np.array([0.0, 1.0], dtype=np.float32),
    )

    assert list(audition.blended_style(engine, {"a": 3, "b": 1})) == list(
        audition.blended_style(engine, {"a": 0.75, "b": 0.25})
    )


def test_a_missing_voice_yields_no_blend_rather_than_a_partial_one():
    import numpy as np

    engine = FakeKokoro(a=np.array([1.0], dtype=np.float32))

    assert audition.blended_style(engine, {"a": 0.5, "nope": 0.5}) is None


def test_zero_weights_yield_no_blend():
    import numpy as np

    engine = FakeKokoro(a=np.array([1.0], dtype=np.float32))

    assert audition.blended_style(engine, {"a": 0.0}) is None


def test_shipped_blends_stay_within_one_espeak_language():
    """A blend has to have one honest answer for the phonemizer.

    Crossing the af_/bm_ families would mean phonemizing some of the blended
    speakers in the wrong accent — the exact failure the language map exists
    to make impossible for single voices.
    """
    for label, weights in audition.BLENDS.items():
        langs = {audition.kokoro_lang(name) for name in weights}
        assert len(langs) == 1, f"{label} mixes {sorted(langs)}"


def write_wav(path, values, rate=24_000, channels=1, width=2):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm(*values))


def test_external_wavs_are_grouped_by_voice_not_by_file(tmp_path, monkeypatch):
    """`<label>-<index>-<mode>` — only the trailing two parts are stripped.

    The grouping decides what shares a gain, so a label containing a hyphen
    (every one of them does: `kokoclone-jarvis`) must not be split on the
    first one.
    """
    write_wav(tmp_path / "kokoclone-jarvis-1-whole.wav", [100])
    write_wav(tmp_path / "kokoclone-jarvis-2-whole.wav", [200])
    write_wav(tmp_path / "other-voice-1-seams.wav", [300])
    monkeypatch.setenv("AUDITION_EXTERNAL", str(tmp_path))

    groups = dict(audition.external_takes())

    assert set(groups) == {"kokoclone-jarvis", "other-voice"}
    assert set(groups["kokoclone-jarvis"]) == {
        "kokoclone-jarvis-1-whole",
        "kokoclone-jarvis-2-whole",
    }


def test_external_is_skipped_when_unset(monkeypatch):
    monkeypatch.delenv("AUDITION_EXTERNAL", raising=False)

    assert audition.external_takes() == []


def test_external_rejects_what_it_cannot_level(tmp_path, monkeypatch):
    """Stereo and 8-bit are skipped rather than reinterpreted as 16-bit mono.

    `leveled` walks the payload as int16; handing it stereo would scale the
    right channel as if it were the left, which is silent corruption of the
    one thing this directory exists to compare fairly.
    """
    write_wav(tmp_path / "stereo-1-whole.wav", [1, 2], channels=2)
    write_wav(tmp_path / "eight-bit-1-whole.wav", [1, 2], width=1)
    write_wav(tmp_path / "good-1-whole.wav", [1, 2])
    monkeypatch.setenv("AUDITION_EXTERNAL", str(tmp_path))

    assert [label for label, _ in audition.external_takes()] == ["good"]


def test_external_keeps_its_own_sample_rate(tmp_path, monkeypatch):
    """22.05kHz and 24kHz files coexist; nothing is resampled."""
    write_wav(tmp_path / "a-1-whole.wav", [1], rate=22_050)
    monkeypatch.setenv("AUDITION_EXTERNAL", str(tmp_path))

    groups = dict(audition.external_takes())

    assert groups["a"]["a-1-whole"][1] == 22_050
