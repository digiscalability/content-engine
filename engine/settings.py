"""Vendor keys from `.env`, held in a settings object (SRD §3.4, §12, FR-AGT-1).

Abbas fills `.env` from `.env.example`; Claude sessions never read it. Loading never
writes `os.environ`, so a key cannot leak into a subprocess environment by accident,
and values are `SecretStr` so they never show up in a repr, log or report.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


@dataclass(frozen=True)
class KeySpec:
    name: str
    needed_for: str  # the SRD §19 gate item or phase that needs the key


KEY_SPECS: tuple[KeySpec, ...] = (
    KeySpec("ANTHROPIC_API_KEY", "G0.3"),
    KeySpec("ELEVENLABS_API_KEY", "G0.4"),
    KeySpec("HEYGEN_API_KEY", "G0.5 (optional)"),
    KeySpec("POSTIZ_API_KEY", "G0.7"),
    KeySpec("PEXELS_API_KEY", "G0.9a"),
    KeySpec("YOUTUBE_DATA_API_KEY", "Phase 2"),
    KeySpec("REDDIT_CLIENT_ID", "Phase 2"),
    KeySpec("REDDIT_CLIENT_SECRET", "Phase 2"),
)


@dataclass(frozen=True)
class Settings:
    secrets: Mapping[str, SecretStr] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        """The key's value, or None when it is absent or blank."""
        secret = self.secrets.get(name)
        if secret is None:
            return None
        value = secret.get_secret_value()
        return value if value.strip() else None

    def key_status(self) -> dict[str, bool]:
        """Every known key name mapped to True (set) or False (missing)."""
        return {spec.name: self.get(spec.name) is not None for spec in KEY_SPECS}


def load_settings(env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    """Read the known keys from a dotenv file without touching `os.environ`."""
    if not env_file.is_file():
        return Settings()
    raw = dotenv_values(env_file, encoding="utf-8")
    known = {spec.name for spec in KEY_SPECS}
    return Settings({k: SecretStr(v) for k, v in raw.items() if k in known and v is not None})
