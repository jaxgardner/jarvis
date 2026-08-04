"""The per-project working directory.

A deep job attached to a project runs here instead of the shared scratch dir,
so its artifacts accumulate somewhere stable and a resumed session — a reply to
the report — finds its own earlier work. This is a subdirectory of the existing
quarantine, not a loosening of it: .env and the repo are no more reachable than
before.

The filesystem is the record. There is no table of files, because there is
nothing about a file worth storing that the file does not already say.
"""

from datetime import datetime, timezone
from pathlib import Path

from app.config import WORK_DIR

# Enough to read a report the agent wrote; small enough that a runaway log file
# cannot be pulled through a phone.
MAX_READ_BYTES = 200_000


def root() -> Path:
    """Overridden in tests. A function rather than a constant so monkeypatching
    it moves every caller at once."""
    return WORK_DIR / "projects"


def dir_for(project: dict) -> Path:
    """`<id>-<slug>`. The slug is stored on the row, so renaming a project does
    not move the directory and invalidate paths already quoted in reports."""
    slug = (project.get("slug") or "project").strip() or "project"
    return root() / f"{project['id']}-{slug}"


def ensure(project: dict) -> Path:
    path = dir_for(project)
    path.mkdir(parents=True, exist_ok=True)
    return path


def listing(project: dict) -> list[dict]:
    path = dir_for(project)
    if not path.is_dir():
        return []
    files = []
    for entry in sorted(path.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        stat = entry.stat()
        files.append(
            {
                "name": entry.name,
                "bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            }
        )
    return files


def read_text(project: dict, name: str) -> str:
    """One artifact's text.

    The name is resolved against the project directory and rejected if it
    escapes — `resolve()` and `is_relative_to`, not string inspection, because
    an absolute path passed to `base / name` silently replaces the base and
    every string-matching guard misses it.
    """
    base = dir_for(project).resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError("no such file in this project")

    data = target.read_bytes()[:MAX_READ_BYTES]
    if b"\x00" in data:
        raise ValueError("not a text file")
    return data.decode("utf-8", errors="replace")
