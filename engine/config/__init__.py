"""Engine configuration: pydantic models and the loader for `config/` (SRD §12)."""

from engine.config.loader import CONFIG_DIR, Config, ConfigError, load_config
from engine.config.models import EngineConfig, IdentityConfig, SourceConfig

__all__ = [
    "CONFIG_DIR",
    "Config",
    "ConfigError",
    "EngineConfig",
    "IdentityConfig",
    "SourceConfig",
    "load_config",
]
