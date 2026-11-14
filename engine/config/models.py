"""Pydantic models for `config/` (SRD §12). Unknown keys are errors, so a typo never
silently drops a field. Rules beyond the SRD's wording are recorded in ADR 0007."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    StringConstraints,
    model_validator,
)

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
Unit = Annotated[float, Field(ge=0, le=1)]
PositiveInt = Annotated[int, Field(ge=1)]
PositiveFloat = Annotated[float, Field(gt=0)]
ClockTime = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
WeeklyTime = Annotated[
    str, StringConstraints(pattern=r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01]\d|2[0-3]):[0-5]\d$")
]

RightsType = Literal[
    "own", "ai_generated", "stock", "partner_written", "cc_by", "public_domain", "film_reference"
]
SourceKind = Literal["folder", "drive", "channel", "library"]
Platform = Literal["youtube", "tiktok", "instagram"]
Format = Literal["A", "B", "C"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

LICENSED_TIERS = frozenset({"stock", "cc_by", "partner_written"})  # FR-SRC-2
CHANNEL_TIERS = frozenset({"own", "partner_written"})  # FR-ING-2
MAX_FILM_SHARE = 0.2  # §8.2


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# Sources (FR-SRC-1, FR-SRC-2, §8) --------------------------------------------------------


class SourceConfig(Model):
    model_config = ConfigDict(coerce_numbers_to_str=True)  # volume serials may look numeric

    id: Slug
    kind: SourceKind
    rights_type: RightsType
    rights_proof: NonBlank
    path: str | None = None
    volume_serial: str | None = None
    volume_label: str | None = None
    url: str | None = None
    licence_url: str | None = None
    attribution: str | None = None
    expires_at: date | None = None

    @model_validator(mode="after")
    def _kind_and_tier_rules(self) -> SourceConfig:
        if self.id == "engine":
            raise ValueError("the id 'engine' is reserved for the engine's generated media")
        if self.kind == "folder" and not self.path:
            raise ValueError("folder sources need a path")
        if self.kind == "drive" and not (self.volume_serial and self.volume_label):
            raise ValueError(
                "drive sources need volume_serial and volume_label (drive letters change, SRD 8.2)"
            )
        if self.kind == "library" and not (self.path or self.url):
            raise ValueError(
                "library sources need a path (a local pool, such as music) or a url (a stock "
                "API, such as Pexels) (ADR 0023)"
            )
        if self.kind == "channel":
            if not self.url:
                raise ValueError("channel sources need a url")
            if self.rights_type not in CHANNEL_TIERS:
                raise ValueError("channel sources must be own or partner_written (FR-ING-2)")
        if self.rights_type == "film_reference" and self.kind != "drive":
            raise ValueError("film_reference sources are registered drives (SRD 8.2)")
        if self.rights_type in LICENSED_TIERS and not (self.licence_url or "").strip():
            raise ValueError(
                f"{self.rights_type} sources need a licence_url, a licence id or URL (FR-SRC-2)"
            )
        if self.rights_type in LICENSED_TIERS and self.attribution is None:
            raise ValueError(
                f"{self.rights_type} sources need an attribution, the credit text "
                '(write attribution: "" when the licence asks for none) (FR-SRC-2)'
            )
        return self


class SourcesFile(Model):
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def _unique_ids(self) -> SourcesFile:
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f"duplicate source id '{source.id}'")
            seen.add(source.id)
        return self


# Identities (§12, §16, §17) ---------------------------------------------------------------


class Cadence(Model):
    posts_per_week: PositiveInt
    max_per_channel_per_day: PositiveInt
    min_gap_hours: float = Field(ge=3)  # FR-PUB-4


class ChannelConfig(Model):
    platform: Platform
    postiz_integration_id: str = ""
    # required: every post carries the platform's AI label; auto: set per post (FR-QA-5)
    ai_disclosure: Literal["required", "auto"]


class Cta(Model):
    kind: Literal["follow", "link"]
    url: str | None = None

    @model_validator(mode="after")
    def _link_needs_url(self) -> Cta:
        if self.kind == "link" and not self.url:
            raise ValueError("a link CTA needs a url")
        return self


class IdentityQA(Model):
    """Deterministic screens of the playbook's rules; the qa-reviewer judges meaning."""

    banned_terms: dict[str, list[NonBlank]] = {}  # FR-QA-6, by category
    never_terms: list[NonBlank] = []  # FR-QA-8: the Fact Sheet's never list
    self_reference_terms: list[NonBlank] = []  # words that make a sentence about Abbas


class IdentityConfig(Model):
    id: Slug
    display_name: str = ""
    autonomy_level: Literal[0, 1]  # only Abbas edits this (§17)
    active_look: str | None = None
    playbook: NonBlank
    formats: dict[Format, Unit]
    cadence: Cadence
    exploration_share: Unit
    real_you_share_min: Unit
    film_breakdown_share_max: float = Field(ge=0, le=MAX_FILM_SHARE)
    channels: list[ChannelConfig] = Field(min_length=1)
    cta: Cta
    qa: IdentityQA = IdentityQA()
    voice_id: str = ""  # ElevenLabs clone (G0.4)
    avatar_id: str = ""  # HeyGen avatar (G0.5)

    @model_validator(mode="after")
    def _identity_rules(self) -> IdentityConfig:
        if abs(sum(self.formats.values()) - 1.0) > 1e-6:
            raise ValueError("formats shares must add up to 1.0")
        platforms = [c.platform for c in self.channels]
        if len(platforms) != len(set(platforms)):
            raise ValueError("one channel per platform")
        if self.formats.get("C", 0) > 0 and any(
            c.ai_disclosure != "required" for c in self.channels
        ):
            raise ValueError(
                "format C is an AI presenter: every channel needs ai_disclosure: required (GR-5)"
            )
        return self


# Engine (§12, §5.3, §13, NFR-4, NFR-5) -----------------------------------------------------


class AgentConfig(Model):
    model: NonBlank
    effort: Effort
    max_turns: PositiveInt


class Agents(Model):
    orchestrator: AgentConfig
    scriptwriter: AgentConfig
    moment_scorer: AgentConfig
    qa_reviewer: AgentConfig
    trend_scout: AgentConfig
    retro_analyst: AgentConfig
    source_scout: AgentConfig


class RunBudgets(Model):
    daily: PositiveFloat
    weekly: PositiveFloat


class ClaudeBudget(Model):
    max_budget_usd: RunBudgets
    monthly_cap_usd: PositiveFloat


class ElevenLabsBudget(Model):
    monthly_char_cap: PositiveInt
    account_floor_chars: int = Field(ge=0)


class HeyGenBudget(Model):
    monthly_credit_cap: PositiveFloat
    credits_per_minute: PositiveFloat  # the estimate per minute of avatar video


class Budgets(Model):
    claude: ClaudeBudget
    elevenlabs: ElevenLabsBudget
    heygen: HeyGenBudget


class Paths(Model):
    db: NonBlank
    media: NonBlank
    raw: NonBlank
    work: NonBlank
    renders: NonBlank
    avatar_inbox: NonBlank
    polished_inbox: NonBlank
    music_library: NonBlank
    references_inbox: NonBlank
    reports: NonBlank
    logs: NonBlank
    gpu_lock: NonBlank = "%LOCALAPPDATA%/content-engine/gpu.lock"  # §13, NFR-6

    def resolve(self, name: str, root: Path) -> Path:
        """The named path, with %VARS% expanded and relative paths taken from `root`."""
        if name not in type(self).model_fields:
            raise KeyError(name)
        path = Path(os.path.expandvars(getattr(self, name)))
        return path if path.is_absolute() else root / path


class Schedule(Model):
    timezone: NonBlank
    ingest: ClockTime
    index_films_window: tuple[ClockTime, ClockTime]
    daily: ClockTime
    metrics_reconcile_every_hours: int = Field(ge=1, le=24)
    weekly: WeeklyTime


class Disk(Model):
    warn_free_gb: PositiveFloat
    stop_free_gb: PositiveFloat

    @model_validator(mode="after")
    def _stop_below_warn(self) -> Disk:
        if self.stop_free_gb >= self.warn_free_gb:
            raise ValueError("stop_free_gb must be below warn_free_gb (NFR-5)")
        return self


class Retention(Model):
    raw_purge_days_after_last_use: PositiveInt
    renders_keep_days_after_publish: PositiveInt


class CrossIdentityQA(Model):
    frame_similarity_max: Unit
    script_cosine_max: Unit
    lookback_days: PositiveInt


class SelfDedupeQA(Model):
    script_cosine_max: Unit
    lookback_posts: PositiveInt


class SafeZone(Model):
    """Pixels of the frame a platform's UI covers on each side (FR-QA-4)."""

    top: NonNegativeInt
    bottom: NonNegativeInt
    left: NonNegativeInt
    right: NonNegativeInt


class QA(Model):
    cross_identity: CrossIdentityQA
    self_dedupe: SelfDedupeQA
    fact_sheet: str = ""  # D9: read at run time, never copied into the repo (NFR-9)
    caption_coverage_min: Unit
    loudness_tolerance_lu: float = Field(gt=0, le=3)
    safe_zones: dict[Platform, SafeZone]


class Render(Model):
    width: PositiveInt
    height: PositiveInt
    fps: PositiveInt
    audio_sample_rate: PositiveInt


class Loudness(Model):
    integrated_lufs: float = Field(lt=0)
    true_peak_dbtp: float = Field(le=0)


class StockConfig(Model):
    """FR-ING-5: stock fetched for the scriptwriter's weak beat queries, capped per day
    (ADR 0023). `thin_below` is the CLIP score under which a beat's best match is weak."""

    source: Slug = "pexels"  # a library source with a url in the sources config
    daily_cap: int = Field(10, ge=0)  # clips a local day
    per_request: PositiveInt = 2  # clips per beat query
    max_clip_s: PositiveFloat = 30.0
    thin_below: Unit = 0.23


class EngineConfig(Model):
    agents: Agents
    budgets: Budgets
    paths: Paths
    schedule: Schedule
    disk: Disk
    retention: Retention
    qa: QA
    render: Render
    loudness: Loudness
    stock: StockConfig = StockConfig()
