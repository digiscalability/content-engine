"""The edit plan (SRD §7.1): a validated JSON shot list that fully determines a render.

Claude never edits pixels; the scriptwriter submits one of these and the renderer turns it
into an MP4. Times are seconds. A shot's `in`/`out` are source times; everything else
(`at`, `start`) is timeline time. Semantics the SRD left open are in ADR 0008.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Role = Literal["hook", "evidence", "broll", "avatar", "real_you", "card"]
Ease = Literal["linear", "in", "out", "in_out"]
BlockKind = Literal[
    "title_card", "lower_third", "annotation", "credit_card", "quote_card", "text_beat"
]
BlockId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
Seconds = Annotated[float, Field(ge=0)]
Positive = Annotated[float, Field(gt=0)]
EPSILON = 1e-3  # 1 ms of float slack on "ends within the plan" checks
FILM_EXCERPT_MAX_S = 10.0  # §8.2: each film excerpt, in on-screen time
FILM_SHARE_MAX = 0.30  # §8.2: all film excerpts, as a share of the runtime


def _r(value: float) -> float:
    return round(value, 6)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Box(Model):
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    w: Annotated[int, Field(gt=0)]
    h: Annotated[int, Field(gt=0)]


class Zoom(Model):
    from_scale: Annotated[float, Field(ge=1, le=3)] = 1.0
    to_scale: Annotated[float, Field(ge=1, le=3)]
    ease: Ease = "in_out"


class Freeze(Model):
    at: Seconds  # shot-relative, after speed
    duration: Annotated[float, Field(gt=0, le=5)]


class Annotation(Model):
    block: BlockId
    start: Seconds  # shot-relative
    duration: Positive


class ShotOps(Model):
    crop: Box | None = None  # source pixels, before fitting to 9:16
    reframe: Literal["cover"] = "cover"  # subject tracking is Phase 2 (§7.3)
    zoom: Zoom | None = None
    freeze: Freeze | None = None
    speed: Annotated[float, Field(ge=0.25, le=4)] = 1.0
    annotate: list[Annotation] = []


class Shot(Model):
    asset_id: Annotated[int, Field(ge=1)]
    in_s: Seconds = Field(alias="in")
    out_s: Positive = Field(alias="out")
    role: Role
    ops: ShotOps = ShotOps()
    transition: Literal["cut"] = "cut"

    @property
    def played(self) -> float:
        """Seconds of source motion on the timeline, after speed."""
        return _r((self.out_s - self.in_s) / self.ops.speed)

    @property
    def duration(self) -> float:
        hold = self.ops.freeze.duration if self.ops.freeze else 0.0
        return _r(self.played + hold)

    @model_validator(mode="after")
    def _times(self) -> Shot:
        if self.out_s <= self.in_s:
            raise ValueError("a shot's out must be after its in")
        if self.ops.freeze and self.ops.freeze.at > self.played + EPSILON:
            raise ValueError("freeze.at must fall inside the shot's played part")
        for note in self.ops.annotate:
            if note.start + note.duration > self.duration + EPSILON:
                raise ValueError(f"annotation '{note.block}' runs past the end of its shot")
        return self


class VoiceSegment(Model):
    asset_id: Annotated[int, Field(ge=1)]
    at: Seconds
    in_s: Seconds = Field(default=0.0, alias="in")
    out_s: Positive = Field(alias="out")

    @property
    def end(self) -> float:
        return _r(self.at + self.out_s - self.in_s)

    @model_validator(mode="after")
    def _times(self) -> VoiceSegment:
        if self.out_s <= self.in_s:
            raise ValueError("a voice segment's out must be after its in")
        return self


class MusicBed(Model):
    asset_id: Annotated[int, Field(ge=1)]
    gain_db: Annotated[float, Field(ge=-40, le=0)] = -18.0
    duck_db: Annotated[float, Field(ge=0, le=30)] = 12.0  # how far the bed drops under VO


class SfxHit(Model):
    asset_id: Annotated[int, Field(ge=1)]
    at: Seconds
    gain_db: Annotated[float, Field(ge=-40, le=6)] = 0.0


class AudioPlan(Model):
    voice: list[VoiceSegment] = []
    music: MusicBed | None = None
    sfx: list[SfxHit] = []


class OverlayBlock(Model):
    block: BlockId  # a rendered HyperFrames block
    kind: BlockKind
    start: Seconds
    duration: Positive

    @property
    def end(self) -> float:
        return _r(self.start + self.duration)


class CaptionSpec(Model):
    block: BlockId  # the look's caption block
    overrides: dict[str, str | float] = {}  # checked against the look's allowed range (P0.8)


class LookRef(Model):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    version: Annotated[int, Field(ge=1)]


class EditPlan(Model):
    timeline: list[Shot] = Field(min_length=1)
    audio: AudioPlan = AudioPlan()
    overlays: list[OverlayBlock] = []
    captions: CaptionSpec | None = None
    look: LookRef

    @property
    def duration(self) -> float:
        return _r(sum(shot.duration for shot in self.timeline))

    def shot_starts(self) -> list[float]:
        starts, t = [], 0.0
        for shot in self.timeline:
            starts.append(_r(t))
            t += shot.duration
        return starts

    def placed_overlays(self) -> list[OverlayBlock]:
        """Plan overlays plus every shot annotation, on timeline time, in start order."""
        placed = list(self.overlays)
        for start, shot in zip(self.shot_starts(), self.timeline, strict=True):
            placed += [
                OverlayBlock(
                    block=note.block, kind="annotation", start=_r(start + note.start),
                    duration=note.duration,
                )
                for note in shot.ops.annotate
            ]  # fmt: skip
        return sorted(placed, key=lambda o: o.start)

    @model_validator(mode="after")
    def _fits(self) -> EditPlan:
        end = self.duration + EPSILON
        for overlay in self.overlays:
            if overlay.end > end:
                raise ValueError(f"overlay '{overlay.block}' runs past the end of the plan")
        for hit in self.audio.sfx:
            if hit.at > end:
                raise ValueError("an sfx hit starts past the end of the plan")
        voice = sorted(self.audio.voice, key=lambda v: v.at)
        for segment in voice:
            if segment.end > end:
                raise ValueError("a voice segment runs past the end of the plan")
        for first, second in zip(voice, voice[1:], strict=False):
            if second.at < first.end - EPSILON:
                raise ValueError("voice segments overlap")
        return self
