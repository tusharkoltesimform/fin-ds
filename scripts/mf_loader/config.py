"""Config loaded from environment, with .env (repo root) as a local fallback.

Per Phase 0 (`specs/phase0-environment-setup.md`), MFAPI_BASE_URL and the
Postgres connection string are config, not hardcoded literals, and secrets
never land in code. `.env` is gitignored; this loader only reads it, never
writes it.
"""
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(_REPO_ROOT / ".env")

MFAPI_BASE_URL = os.environ.get("MFAPI_BASE_URL", "https://api.mfapi.in")

PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DATABASE = os.environ.get("PG_DATABASE", "forpocdb")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")

# Full one-time load: curated scheme universe. Spec volume is ~500 (§0 dim_mf_scheme).
SCHEME_LIMIT = int(os.environ.get("MF_SCHEME_LIMIT", "500"))
RANDOM_SEED = int(os.environ.get("MF_RANDOM_SEED", "42"))
MAX_WORKERS = int(os.environ.get("MF_MAX_WORKERS", "16"))

# §2.1: retry 3x with exponential backoff 30s / 2m / 8m before marking a
# scheme's fetch failed for this run.
RETRY_BACKOFF_SECONDS = [30, 120, 480]
REQUEST_TIMEOUT_SECONDS = 30

LOG_DIR = _REPO_ROOT / "logs"
