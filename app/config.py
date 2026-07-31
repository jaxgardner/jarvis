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
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")

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


def configured() -> dict[str, bool]:
    """Which secrets are present. Used by /health — never returns the values."""
    return {
        "anthropic_api_key": bool(_ANTHROPIC_API_KEY),
        "jarvis_token": bool(_JARVIS_TOKEN),
        "ntfy_topic": bool(_NTFY_TOPIC),
    }
