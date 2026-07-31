"""Point every test at a throwaway database.

Must run before app.config is imported — pytest loads conftest first, so
setting the env var here wins over the .env value (load_dotenv does not
override an already-set variable).
"""

import os
import tempfile
from pathlib import Path

_TMP_DB = Path(tempfile.mkdtemp(prefix="jarvis-test-")) / "test.db"
os.environ["JARVIS_DB"] = str(_TMP_DB)
