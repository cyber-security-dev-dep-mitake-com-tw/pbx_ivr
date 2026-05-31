"""
Tiny .env loader for the test_protocol_simulation scripts.
Walks up from this file looking for a .env file (finds project root).
Values already in os.environ are NOT overwritten.
"""
import os
from pathlib import Path


def _find_env() -> Path | None:
    here = Path(__file__).resolve().parent
    for directory in [here, *here.parents]:
        candidate = directory / ".env"
        if candidate.exists():
            return candidate
    return None


def load() -> dict[str, str]:
    """Load .env into os.environ (skip keys already set). Returns the loaded dict."""
    path = _find_env()
    loaded: dict[str, str] = {}
    if not path:
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


# Auto-load on import
load()
