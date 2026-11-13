"""Load and validate everything under `config/` (SRD §12, FR-SRC-1).

Every problem in every file is collected into one `ConfigError`, each line naming the
file and field, so Abbas can fix a whole edit in one pass. This module only reads:
new sources come from Abbas editing sources.yaml, never from code (FR-SRC-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from engine.config.models import EngineConfig, IdentityConfig, SourceConfig, SourcesFile
from engine.settings import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "config"
ENGINE_FILE = "engine.yaml"
SOURCES_FILE = "sources.yaml"
IDENTITIES_DIR = "identities"

M = TypeVar("M", bound=BaseModel)


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class Config:
    engine: EngineConfig
    sources: list[SourceConfig]
    identities: dict[str, IdentityConfig]


def _yaml_problem(rel: str, exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}" if mark is not None else ""
    what = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    text = f"{rel}: invalid YAML{where}: {what}"
    if "escape" in what:
        text += " (Windows paths: use single quotes or none)"
    return text


def _read_mapping(path: Path, rel: str, problems: list[str], empty_hint: str = "") -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        problems.append(f"{rel}: file not found")
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        problems.append(_yaml_problem(rel, exc))
        return None
    if not isinstance(data, dict):
        hint = f" ({empty_hint})" if empty_hint else ""
        problems.append(f"{rel}: expected a mapping at the top level{hint}")
        return None
    return data


def _validate(model: type[M], data: Any, rel: str, problems: list[str]) -> M | None:
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            loc = ".".join(str(part) for part in error["loc"]) or "(top level)"
            message = error["msg"].removeprefix("Value error, ")
            problems.append(f"{rel}: {loc}: {message}")
        return None


def _load_identity(path: Path, config_dir: Path, problems: list[str]) -> IdentityConfig | None:
    rel = f"{IDENTITIES_DIR}/{path.name}"
    identity = _validate(IdentityConfig, _read_mapping(path, rel, problems), rel, problems)
    if identity is None:
        return None
    if identity.id != path.stem:
        problems.append(f"{rel}: id: '{identity.id}' must match the file name '{path.stem}'")
    if not (config_dir / identity.playbook).is_file():
        problems.append(f"{rel}: playbook: {identity.playbook} not found")
    if identity.active_look and not (config_dir / identity.active_look).is_file():
        problems.append(f"{rel}: active_look: {identity.active_look} not found")
    return identity


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    problems: list[str] = []
    engine = _validate(
        EngineConfig,
        _read_mapping(config_dir / ENGINE_FILE, ENGINE_FILE, problems),
        ENGINE_FILE,
        problems,
    )
    sources = _validate(
        SourcesFile,
        _read_mapping(
            config_dir / SOURCES_FILE,
            SOURCES_FILE,
            problems,
            empty_hint="write `sources: []` when there are none",
        ),
        SOURCES_FILE,
        problems,
    )
    identity_files = sorted((config_dir / IDENTITIES_DIR).glob("*.yaml"))
    if not identity_files:
        problems.append(f"{IDENTITIES_DIR}/: no identity files")
    identities: dict[str, IdentityConfig] = {}
    for path in identity_files:
        identity = _load_identity(path, config_dir, problems)
        if identity is not None:
            identities[identity.id] = identity
    if problems or engine is None or sources is None:
        raise ConfigError(problems)
    return Config(engine=engine, sources=list(sources.sources), identities=identities)
