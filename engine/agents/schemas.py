"""What agents submit through the `submit_*` tools, and the RunReport a run ends with
(SRD §5.4, §5.6). Tool input schemas are generated from these models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.config.models import Format
from engine.produce.editplan import EditPlan

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MAX_WORDS_PER_SECOND = 3.0  # FR-PRD-1: captions must stay readable

# §4.1 runtime per format (format C long-form arrives in Phase 3)
FORMAT_SECONDS: dict[str, tuple[int, int]] = {"A": (30, 75), "B": (20, 60), "C": (30, 60)}

_COMMON_TIERS = ("own", "ai_generated", "stock", "partner_written", "cc_by", "public_domain")


def allowed_tiers(fmt: str) -> tuple[str, ...]:
    """Rights tiers a format may use (§8.1): film_reference only in A breakdowns."""
    return (*_COMMON_TIERS, "film_reference") if fmt == "A" else _COMMON_TIERS


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefSubmission(Model):
    identity_id: NonBlank
    format: Format
    topic: NonBlank
    angle: NonBlank
    hook: NonBlank
    cta: NonBlank
    source_moments: list[int] = []
    difference_note: NonBlank = Field(
        description="Why this post differs from the identity's last 20 posts (FR-PLN-3)."
    )
    exploration: bool = False


class Beat(Model):
    text: NonBlank = Field(description="The words spoken in this beat.")
    on_screen: str = ""
    target_s: Annotated[float, Field(gt=0)]

    @property
    def words(self) -> int:
        return len(self.text.split())

    @model_validator(mode="after")
    def _readable(self) -> Beat:
        if self.words / self.target_s > MAX_WORDS_PER_SECOND:
            raise ValueError(
                f"beat speaks {self.words} words in {self.target_s} s; "
                f"keep to {MAX_WORDS_PER_SECOND:g} words a second (FR-PRD-1)"
            )
        return self


class ScriptSubmission(Model):
    brief_id: Annotated[int, Field(ge=1)]
    beats: list[Beat] = Field(min_length=1)

    @property
    def words(self) -> int:
        return sum(b.words for b in self.beats)

    @property
    def duration(self) -> float:
        return round(sum(b.target_s for b in self.beats), 3)

    @property
    def spoken_text(self) -> str:
        return " ".join(b.text for b in self.beats)


class EditPlanSubmission(Model):
    brief_id: Annotated[int, Field(ge=1)]
    plan: EditPlan


class MomentScore(Model):
    asset_id: Annotated[int, Field(ge=1)]
    start_s: Annotated[float, Field(ge=0)]
    end_s: Annotated[float, Field(gt=0)]
    text: str = ""
    hook_score: Annotated[float, Field(ge=0, le=1)]
    standalone_score: Annotated[float, Field(ge=0, le=1)]


class MomentScoresSubmission(Model):
    scorer_version: NonBlank
    scores: list[MomentScore] = Field(min_length=1)


class QAReviewSubmission(Model):
    render_id: Annotated[int, Field(ge=1)]
    verdict: Literal["pass", "hold", "fail"]
    reasons: list[str] = []

    @model_validator(mode="after")
    def _reasons(self) -> QAReviewSubmission:
        if self.verdict != "pass" and not [r for r in self.reasons if r.strip()]:
            raise ValueError("a hold or fail needs reasons (FR-QA-7)")
        return self


class FailedPost(Model):
    post: str
    reason: str


class IdentityResult(Model):
    identity: str
    posts_drafted: int = 0
    posts_failed: list[FailedPost] = []
    qa_holds: list[str] = []


class BudgetUsed(Model):
    claude_usd: float = 0.0
    elevenlabs_chars: int = 0
    heygen_credits: float = 0.0


class NeedsAbbas(Model):
    item: str
    link: str = ""


class RunReport(Model):
    """What every daily run returns (§5.6); the digest is built from it."""

    run_id: str
    identity_results: list[IdentityResult]
    budget_used: BudgetUsed
    warnings: list[str]
    needs_abbas: list[NeedsAbbas]
