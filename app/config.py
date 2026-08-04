"""Settings, loaded once from .env at import."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


# Values carried over unedited from .env.example. Treated as unset so a
# half-configured install reports honestly instead of failing at first use.
_PLACEHOLDERS = {"sk-ant-...", ""}


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    return "" if value in _PLACEHOLDERS else value


def _require(name: str) -> str:
    value = _get(name)
    if not value:
        raise RuntimeError(
            f"{name} is unset (or still the .env.example placeholder). "
            "Fill it in in .env."
        )
    return value


def _db_path() -> Path:
    raw = os.getenv("JARVIS_DB", "").strip().strip('"').strip("'")
    if not raw:
        raw = "~/Library/Application Support/jarvis/jarvis.db"
    return Path(raw).expanduser()


DB_PATH = _db_path()
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/Denver").strip()

# ── pantry ────────────────────────────────────────────────
# Thermal receipt print is hard OCR and Haiku's accuracy on it is unproven.
# The review screen means a bad read costs edits rather than correctness, so
# this stays a config knob: if editing is tedious in practice, move to
# claude-sonnet-4-5 without touching a code path.
PANTRY_VISION_MODEL = os.getenv("PANTRY_VISION_MODEL", "claude-haiku-4-5").strip()

# Receipt photos live beside the database, outside the repo, for the same
# reason the database does: they survive a re-clone and are never committed.
RECEIPT_DIR = DB_PATH.parent / "receipts"

# Where deep jobs run. Deliberately NOT the repo: with full tool access the
# agent has a shell, and the repo root contains .env. Derived from DB_PATH's
# parent rather than hardcoded to ~/Library so tests get a throwaway copy —
# the default resolves to exactly the path worker/run.py used to hardcode.
WORK_DIR = DB_PATH.parent / "work"

# Local hour at which the day-before expiry push goes out. 17:00 is late
# enough that you can still cook or shop, early enough to act on.
PANTRY_EXPIRY_HOUR = int(os.getenv("PANTRY_EXPIRY_HOUR", "17"))

# ── gratitude ─────────────────────────────────────────────
# The local hour the evening prompt goes out, and the hour the gratitude day
# rolls over. Both are personal facts about when an evening ends rather than
# facts about the system, which is why they are .env and not constants.
GRATITUDE_HOUR = int(os.getenv("GRATITUDE_HOUR", "22"))
GRATITUDE_DAY_START = int(os.getenv("GRATITUDE_DAY_START", "4"))
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")

# ── speech ────────────────────────────────────────────────
# The weights are ~310 MB, so they live beside the database for the same
# reason it does: never committed, and a re-clone does not re-download them.
TTS_MODEL_DIR = Path(
    os.getenv("TTS_MODEL_DIR", "").strip() or DB_PATH.parent / "voices"
).expanduser()

# The Piper voice that speaks, as a filename stem under `voices/piper/`. It
# supplies the words, the accent and the timing; the timbre comes from
# TTS_REFERENCE below, via the conversion in speech/clone.py. Both were
# settled by listening — see speech/audition.py.
TTS_MODEL = os.getenv("TTS_MODEL", "jarvis-high").strip()

# The clip whose voice every reply is converted toward, relative to
# TTS_MODEL_DIR. Swapping this swaps the assistant's voice without retraining
# anything, which is the whole reason the pipeline is two stages.
TTS_REFERENCE = os.getenv("TTS_REFERENCE", "jarvis-reference.mp3").strip()

TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))

# Synthesize a reply in clause-sized pieces and stream them, so playback
# starts after the first clause instead of after the whole reply — worth about
# 850ms on a one-sentence reply. The cost is a seam at each cut, since every
# piece gets its own intonation. Set to 0 to go back to one utterance per
# reply; like TTS_VOICE, whether it sounds right is a question for ears, so it
# is a line in .env rather than a code path.
TTS_STREAM_CHUNKS = os.getenv("TTS_STREAM_CHUNKS", "1").strip() not in {"0", "false", "no"}

# Read lazily via the helpers below — /health must work before these are set,
# so importing this module cannot be allowed to fail on a missing key.
_ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
_JARVIS_TOKEN = _get("JARVIS_TOKEN")
_NTFY_TOPIC = _get("NTFY_TOPIC")


def anthropic_api_key() -> str:
    return _require("ANTHROPIC_API_KEY")


def jarvis_token() -> str:
    return _require("JARVIS_TOKEN")


def ntfy_topic() -> str:
    return _require("NTFY_TOPIC")


# ── push ──────────────────────────────────────────────────
# PUSH_BACKENDS is a comma-separated list. The Mini runs "apns"; the default
# here is deliberately "ntfy" alone, so a checkout without a .env does not
# depend on APNs credentials it does not have. Moving between backends means
# running "ntfy,apns" until the new one has proven quiet-free — "reminders
# silently stop firing" is on the risk table, and swapping the push backend is
# precisely how that happens.

APNS_KEY_PATH = _get("APNS_KEY_PATH")
APNS_KEY_ID = _get("APNS_KEY_ID")
APNS_TEAM_ID = _get("APNS_TEAM_ID")
APNS_BUNDLE_ID = _get("APNS_BUNDLE_ID")
APNS_ENV = (_get("APNS_ENV") or "prod").lower()


def push_backends() -> list[str]:
    raw = _get("PUSH_BACKENDS") or "ntfy"
    return [b.strip().lower() for b in raw.split(",") if b.strip()]


def apns_configured() -> bool:
    return all(
        (APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_BUNDLE_ID)
    ) and Path(APNS_KEY_PATH).expanduser().is_file()


def apns_key_path() -> Path:
    # Reads the module constant rather than the environment so that the
    # "is it configured" check and the "load it" path can never disagree.
    if not APNS_KEY_PATH:
        raise RuntimeError("APNS_KEY_PATH is unset. Fill it in in .env.")
    path = Path(APNS_KEY_PATH).expanduser()
    if not path.is_file():
        raise RuntimeError(f"APNS_KEY_PATH does not exist: {path}")
    return path


# ── Google ingestion ──────────────────────────────────────
# A "Desktop app" OAuth client. The client secret is not actually secret for
# this client type — Google says so, and it ships inside every installed app —
# but it still lives in .env rather than the repo, because there is no upside
# to publishing it and it pairs with a refresh token that IS a real credential.

_GOOGLE_CLIENT_ID = _get("GOOGLE_CLIENT_ID")
_GOOGLE_CLIENT_SECRET = _get("GOOGLE_CLIENT_SECRET")


def google_client_id() -> str:
    return _require("GOOGLE_CLIENT_ID")


def google_client_secret() -> str:
    return _require("GOOGLE_CLIENT_SECRET")


def google_configured() -> bool:
    return bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET)


def configured() -> dict[str, bool]:
    """Which secrets are present. Used by /health — never returns the values."""
    return {
        "anthropic_api_key": bool(_ANTHROPIC_API_KEY),
        "jarvis_token": bool(_JARVIS_TOKEN),
        "ntfy_topic": bool(_NTFY_TOPIC),
        "apns": apns_configured(),
        "google": google_configured(),
    }
