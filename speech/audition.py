"""Render the same replies in every candidate voice, for a human to judge.

Run once, before any of the rest of this exists:

    uv run python -m speech.audition

Kokoro ships four British males. Which one sounds like an assistant rather
than a narrator is not a thing to decide from a model card, so this writes
twelve files and gets out of the way.
"""

import sys
from pathlib import Path

from speech import wav as wavfile

# Real replies, not lorem ipsum. The confirmations are what you hear forty
# times a day, and a voice that reads a paragraph beautifully can still be
# wrong for "Got it."
LINES = [
    "Got it. Reminder set for five o'clock.",
    "You have three things tomorrow. Dentist at nine, standup at ten thirty, "
    "and dinner with Sam at seven.",
    "The milk expires tomorrow, and you're out of coffee.",
]

VOICES = ["bm_george", "bm_lewis", "bm_daniel", "bm_fable"]

OUT = Path("/tmp/jarvis-audition")


def main() -> int:
    from kokoro_onnx import Kokoro

    from app import config

    model = config.TTS_MODEL_DIR / "kokoro-v1.0.onnx"
    voices = config.TTS_MODEL_DIR / "voices-v1.0.bin"
    if not model.exists() or not voices.exists():
        print(f"missing model files in {config.TTS_MODEL_DIR}", file=sys.stderr)
        return 1

    kokoro = Kokoro(str(model), str(voices))
    OUT.mkdir(parents=True, exist_ok=True)

    for voice in VOICES:
        for index, line in enumerate(LINES, start=1):
            samples, rate = kokoro.create(line, voice=voice, speed=1.0, lang="en-gb")
            path = OUT / f"{voice}-{index}.wav"
            path.write_bytes(wavfile.encode(samples, rate))
            print(path)

    print(f"\n{len(VOICES) * len(LINES)} files in {OUT}. Listen, then set "
          f"TTS_VOICE in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
