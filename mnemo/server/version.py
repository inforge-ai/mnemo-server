"""Build version string: semver + git commit hash.

The base version comes from pyproject.toml (via importlib.metadata).
The git commit hash is injected at Docker build time via BUILD_COMMIT env var.
If not in Docker (dev mode), falls back to reading git directly.
"""

import os
import subprocess
from functools import lru_cache
from importlib.metadata import version as pkg_version


def _get_base_version() -> str:
    try:
        return pkg_version("mnemodb")
    except Exception:
        return "0.0.0"


def _get_commit() -> str:
    # Docker build injects this
    commit = os.environ.get("BUILD_COMMIT", "")
    if commit:
        return commit[:7]

    # Dev mode: read from git
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return "unknown"


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return full version string like '0.2.0+a0e02f9'."""
    base = _get_base_version()
    commit = _get_commit()
    return f"{base}+{commit}"
