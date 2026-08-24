"""Environment handling: read secrets from named variables, fail fast."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value lines); real env vars take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str, purpose: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing required environment variable {name} ({purpose}). "
            f"See config/env.example; set it in your shell or in config/.env."
        )
    return value
