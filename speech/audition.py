"""Render the same replies in every candidate voice, for a human to judge.

    uv run python -m speech.audition                  # whatever is installed
    uv run --with piper-tts python -m speech.audition # including Piper

Ran once to pick a Kokoro voice, and again to answer whether a community
JARVIS voice is worth giving up Kokoro's fidelity for. Nothing here is
imported by the server: this is a listening tool, and the decision it feeds
is one line in `.env`.

Two things it renders that a naive A/B would miss:

  - **Both engines, side by side, at the same speed.** Kokoro returns floats
    at 24kHz and Piper int16 at 22.05kHz, so they meet as int16 PCM and get a
    header at the last possible moment.
  - **Whole replies *and* chunked ones.** `/speech` never synthesizes a long
    reply in one go — it cuts on punctuation via `speech.segment`, and each
    piece gets its own intonation contour. A voice can sound fine read whole
    and audibly seamed when cut, which is a property of the engine and not
    something the model card will tell you. `-whole` and `-seams` files are
    the same words either way; if you cannot hear the difference, the cut
    policy transfers.

Getting the JARVIS voice, which does not ship with anything:

    VOICES="$HOME/Library/Application Support/jarvis/voices/piper"
    mkdir -p "$VOICES"
    BASE=https://huggingface.co/jgkawell/jarvis/resolve/main/en/en_GB/jarvis
    curl -L -o "$VOICES/jarvis-medium.onnx"      "$BASE/medium/jarvis-medium.onnx"
    curl -L -o "$VOICES/jarvis-medium.onnx.json" "$BASE/medium/jarvis-medium.onnx.json"
    curl -L -o "$VOICES/jarvis-high.onnx"        "$BASE/high/jarvis-high.onnx"
    curl -L -o "$VOICES/jarvis-high.onnx.json"   "$BASE/high/jarvis-high.onnx.json"

`.onnx.json` sits next to its `.onnx` and carries the sample rate, the espeak
voice and the inference defaults — which is why there is no `lang_for` on this
side. Any other Piper voice dropped in that directory is auditioned too.
"""

import array
import os
import sys
import wave
from pathlib import Path

from app import config
from speech import segment, wav as wavfile

# What every voice is scaled to before you hear it. Kokoro peaks around 20000
# on these lines and Piper around 8000 — roughly 8dB apart, which is easily
# enough to decide a listening test on its own, since louder reads as clearer.
# Short of full scale so the scaling cannot clip.
TARGET_PEAK = 22000

# Real replies, not lorem ipsum. The confirmations are what you hear forty
# times a day, and a voice that reads a paragraph beautifully can still be
# wrong for "Got it."
LINES = [
    "Got it. Reminder set for five o'clock.",
    "You have three things tomorrow. Dentist at nine, standup at ten thirty, "
    "and dinner with Sam at seven.",
    "The milk expires tomorrow, and you're out of coffee.",
]

# Kokoro is no longer what the server speaks with, so these are here purely as
# the reference the deployed voice gets compared against — the two that won
# their respective auditions.
KOKORO_VOICES = ["af_bella", "bm_george"]

# Kokoro's names carry their language in the first letter: `af_`/`am_` are
# American, `bf_`/`bm_` British, and espeak-ng has to be told which. This
# lived in `synth` while Kokoro was the engine; Piper carries the answer in
# its own `.onnx.json`, so the map came here with its last caller rather than
# staying behind in a module that no longer has a use for it.
KOKORO_LANGS = {"a": "en-us", "b": "en-gb"}

KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES_BIN = "voices-v1.0.bin"

# Weighted sums of Kokoro's style vectors, which `create` accepts in place of
# a voice name. Kokoro's own default `af` is a 50/50 of Bella and Sarah, so
# this is the model's idiom rather than a trick played on it.
#
# The point of these is crispness: a blend is *native* Kokoro output, so it
# keeps the fidelity that every conversion-based approach spends. What it
# cannot do is aim — there is no similarity metric here, only ears, and the
# reachable space is whatever Kokoro's speakers already span. These three walk
# from the deployed British male toward the other three, which is the only
# direction that could plausibly land nearer JARVIS.
BLENDS = {
    "blend-george-fable": {"bm_george": 0.5, "bm_fable": 0.5},
    "blend-george-lewis": {"bm_george": 0.6, "bm_lewis": 0.4},
    "blend-george-daniel": {"bm_george": 0.7, "bm_daniel": 0.3},
}

PIPER_DIR = config.TTS_MODEL_DIR / "piper"

# The Desktop, not /tmp. Everything this script produces exists to be opened
# by a person, and Finder hides both /tmp and the /private/tmp it points at —
# so the files were findable from a shell and invisible from the GUI, which
# for a listening tool is the same as not being written at all.
OUT = Path(os.getenv("AUDITION_DIR", "").strip() or Path.home() / "Desktop" / "jarvis-audition")


def kokoro_lang(voice):
    """The espeak-ng language a Kokoro voice must be phonemized in."""
    try:
        return KOKORO_LANGS[voice[:1]]
    except KeyError:
        raise ValueError(
            f"{voice!r} is not an English Kokoro voice. Only the af_/am_ "
            "(American) and bf_/bm_ (British) families are wired up."
        ) from None


def kokoro_backend():
    """`(label, synth)` for each Kokoro voice; empty if the weights are absent.

    `synth` takes text and returns int16 PCM plus its rate — the same shape
    the Piper side returns, so the caller never branches on engine again.
    """
    model = config.TTS_MODEL_DIR / KOKORO_MODEL
    voices = config.TTS_MODEL_DIR / KOKORO_VOICES_BIN
    if not model.exists() or not voices.exists():
        print(f"skipping kokoro: no weights in {config.TTS_MODEL_DIR}", file=sys.stderr)
        return []

    from kokoro_onnx import Kokoro

    engine = Kokoro(str(model), str(voices))
    backends = []

    for voice in dict.fromkeys(KOKORO_VOICES):  # dedupe, keep order
        try:
            lang = kokoro_lang(voice)
        except ValueError as exc:
            print(f"skipping {voice}: {exc}", file=sys.stderr)
            continue

        def synth(text, voice=voice, lang=lang):
            samples, rate = engine.create(
                text, voice=voice, speed=config.TTS_SPEED, lang=lang
            )
            return wavfile.pcm16(samples), rate

        backends.append((f"kokoro-{voice}", synth))

    for label, weights in BLENDS.items():
        style = blended_style(engine, weights)
        if style is None:
            continue

        # Every blend here is British, and mixing across the a/b families
        # would leave no honest answer for the phonemizer. Guarded rather than
        # assumed, because a future blend that crosses them must fail loudly
        # instead of quietly phonemizing half the speakers wrong.
        langs = {kokoro_lang(name) for name in weights}
        if len(langs) > 1:
            print(f"skipping {label}: mixes {sorted(langs)}", file=sys.stderr)
            continue

        def synth(text, style=style, lang=langs.pop()):
            samples, rate = engine.create(
                text, voice=style, speed=config.TTS_SPEED, lang=lang
            )
            return wavfile.pcm16(samples), rate

        backends.append((f"kokoro-{label}", synth))

    return backends


def blended_style(engine, weights):
    """A weighted sum of named Kokoro style vectors, or None if any is absent.

    Weights are normalized, so `{a: 3, b: 1}` and `{a: 0.75, b: 0.25}` are the
    same voice. Without that, a pair summing to 1.4 is not a blend of two
    speakers — it is a blend scaled by 1.4, and style vectors are not gain.
    """
    total = sum(weights.values())
    if total <= 0:
        return None

    style = None
    for name, weight in weights.items():
        try:
            vector = engine.get_voice_style(name)
        except KeyError:
            print(f"skipping blend: no voice {name!r}", file=sys.stderr)
            return None
        scaled = vector * (weight / total)
        style = scaled if style is None else style + scaled
    return style


def piper_backend():
    """`(label, synth)` for each Piper voice in PIPER_DIR.

    Piper is not a dependency of this project and deliberately is not being
    made one for an experiment that may be rejected by ear. It is imported
    here, inside the function, so that a machine without it auditions Kokoro
    normally instead of failing at import.
    """
    models = sorted(PIPER_DIR.glob("*.onnx")) if PIPER_DIR.is_dir() else []
    if not models:
        print(f"skipping piper: no .onnx files in {PIPER_DIR}", file=sys.stderr)
        return []

    try:
        from piper import PiperVoice, SynthesisConfig
    except ImportError:
        print(
            "skipping piper: not installed — rerun as\n"
            "  uv run --with piper-tts python -m speech.audition",
            file=sys.stderr,
        )
        return []

    backends = []
    for path in models:
        voice = PiperVoice.load(str(path))

        # `normalize_audio` defaults to True and levels each *call* to full
        # scale. Chunked, that is per clause: measured, every chunk came back
        # peaking at exactly 32767, so a quiet clause and a loud one arrive
        # equally loud and the seam becomes a step in level. Kokoro does not
        # do this — its peaks vary by a third across these same lines — and an
        # A/B where one engine is compressed and the other is not is not an
        # A/B. Off here for the same reason /speech would want it off.
        #
        # `length_scale` is Piper's inverse of TTS_SPEED: larger is slower.
        # Dividing the model's own default keeps the voice's intended pace as
        # the 1.0 case, so the two engines are auditioned at one speed.
        syn = SynthesisConfig(
            normalize_audio=False,
            length_scale=voice.config.length_scale / config.TTS_SPEED,
        )

        def synth(text, voice=voice, syn=syn):
            # One call yields a chunk per sentence; the caller wants one
            # buffer, and every chunk shares a rate.
            chunks = list(voice.synthesize(text, syn_config=syn))
            pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
            return pcm, chunks[0].sample_rate

        backends.append((f"piper-{path.stem}", synth))

    return backends


def external_takes():
    """Pre-rendered WAVs from an engine that is not installed here.

    `AUDITION_EXTERNAL=<dir>`, holding files already named the way this script
    names its own: `<label>-<index>-<mode>.wav`. They are read, loudness-
    matched with everything else, and copied into OUT alongside it.

    This exists because the interesting candidates are no longer things you
    would put in this repo. KokoClone wants torch, torchaudio and a codec
    model; a hosted voice wants a network call and an API key. Rendering those
    somewhere else and handing over WAVs keeps the comparison honest — same
    lines, same levelling — while keeping the dependency where it belongs,
    which is not in a project that chose `kokoro-onnx` specifically to avoid
    having PyTorch in it.

    Grouped by the label before the trailing `-<index>-<mode>`, because the
    gain has to be per voice and these arrive as a flat directory.
    """
    root = os.getenv("AUDITION_EXTERNAL", "").strip()
    if not root:
        return []

    directory = Path(root).expanduser()
    if not directory.is_dir():
        print(f"skipping external: {directory} is not a directory", file=sys.stderr)
        return []

    groups: dict[str, dict] = {}
    for path in sorted(directory.glob("*.wav")):
        label = path.stem.rsplit("-", 2)[0]
        if label == path.stem:
            print(f"skipping {path.name}: not <label>-<index>-<mode>.wav", file=sys.stderr)
            continue
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                print(f"skipping {path.name}: not 16-bit mono", file=sys.stderr)
                continue
            groups.setdefault(label, {})[path.stem] = (
                handle.readframes(handle.getnframes()),
                handle.getframerate(),
            )

    return list(groups.items())


def synthesized(label, synth):
    """Every take for one voice: each line, whole and chunked."""
    return {
        f"{label}-{index}-{mode}": render(synth, line, chunked)
        for index, line in enumerate(LINES, start=1)
        for mode, chunked in (("whole", False), ("seams", True))
    }


def render(synth, text, chunked):
    """`text` as int16 PCM plus its rate, optionally synthesized as /speech would.

    Chunked, this is several inferences concatenated — which is exactly what
    the phone plays back to back, seams and all.
    """
    pieces = (segment.segments(text) or [text]) if chunked else [text]
    rendered = [synth(piece) for piece in pieces]
    return b"".join(payload for payload, _ in rendered), rendered[0][1]


def leveled(takes):
    """Every take from one voice scaled by a single gain.

    One gain for the whole voice, computed from its loudest moment — never per
    file and never per chunk. Per-chunk levelling is what `normalize_audio`
    does and it is exactly what must not happen here: it would flatten the
    difference between a quiet clause and a loud one, which is the seam the
    `-seams` files exist to expose. This only removes the engine-to-engine
    offset, and leaves every dynamic inside a voice untouched.
    """
    peak = max(
        (max(abs(sample) for sample in _samples(pcm)) for pcm, _ in takes.values()),
        default=0,
    )
    if not peak:
        return takes

    gain = TARGET_PEAK / peak
    return {
        name: (_scaled(pcm, gain), rate) for name, (pcm, rate) in takes.items()
    }


def _samples(pcm):
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _scaled(pcm, gain):
    values = array.array("h", (int(sample * gain) for sample in _samples(pcm)))
    if sys.byteorder == "big":
        values.byteswap()
    return values.tobytes()


def main() -> int:
    voices = [
        (label, synthesized(label, synth))
        for label, synth in kokoro_backend() + piper_backend()
    ] + external_takes()

    if not voices:
        print("nothing to audition", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    written = 0

    for _, takes in voices:
        for name, (pcm, rate) in leveled(takes).items():
            path = OUT / f"{name}.wav"
            path.write_bytes(wavfile.encode_pcm16(pcm, rate))
            print(path)
            written += 1

    print(
        f"\n{written} files in {OUT}, loudness-matched across engines.\n"
        f"  afplay {OUT}/*.wav        # or open the directory in Finder\n"
        "Listen for two things: which voice you want, and whether -seams\n"
        "sounds worse than -whole. Then set TTS_VOICE in .env.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
