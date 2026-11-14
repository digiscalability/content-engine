"""Looks (SRD §9.2, D8): an identity's versioned visual and audio style, one YAML file per
version at `config/looks/<identity>-v<n>.yaml`. A change is a new version, never an edit.

- `grade`: a few colour numbers turned into a generated 3D LUT, so no third-party LUT
  file is ever loaded;
- `captions`: the caption block's style (fonts bundled with HyperFrames only);
- `title_card`: the title card's style (rendered once title blocks exist);
- `pacing`: cut rhythm the scriptwriter follows;
- `music`: the feel to pick from the licensed library, never tracks.

A plan whose look file does not exist renders with `DEFAULT_LOOK`, and the render
report says so. Choices are in ADR 0015.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.config.models import IdentityConfig
from engine.produce.captions import CaptionStyle
from engine.produce.editplan import LookRef

# HyperFrames resolves these by name (its format reference); anything else needs a font file.
BUNDLED_FONTS = Literal[
    "Inter", "Montserrat", "Outfit", "Nunito", "Oswald", "League Gothic", "Archivo Black",
    "Space Mono", "IBM Plex Mono", "JetBrains Mono", "EB Garamond", "Playfair Display",
    "Source Code Pro", "Roboto", "Open Sans", "Lato", "Poppins",
]  # fmt: skip
Hex = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
VERSIONED = re.compile(r"^(?P<id>[a-z0-9][a-z0-9-]*)-v(?P<version>\d+)$")  # <id>-v<n>


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Grade(Model):
    temperature: float = Field(0.0, ge=-0.2, le=0.2)  # + warmer: red up, blue down
    tint: float = Field(0.0, ge=-0.1, le=0.1)  # + magenta: green down
    contrast: float = Field(1.0, ge=0.7, le=1.4)  # around mid grey
    saturation: float = Field(1.0, ge=0.0, le=1.6)
    lift: float = Field(0.0, ge=-0.1, le=0.1)  # blacks up or down
    gamma: float = Field(1.0, ge=0.7, le=1.4)  # > 1 brightens the mids


class CaptionLook(Model):
    block: Slug = "captions"
    font_family: BUNDLED_FONTS = "Inter"
    font_weight: Literal[400, 500, 600, 700, 800, 900] = 900
    font_size: int = Field(92, ge=56, le=128)
    max_width: int = Field(740, ge=600, le=740)  # inside every platform's safe zone (ADR 0018)
    bottom: int = Field(650, ge=420, le=900)  # clear of the platform UI
    ink: Hex = "#ffffff"
    accent: Hex = "#ffd23f"
    outline: Hex = "#111111"
    outline_px: int = Field(14, ge=0, le=24)
    max_words: int = Field(4, ge=2, le=6)


class TitleCardLook(Model):
    font_family: BUNDLED_FONTS = "Montserrat"
    font_weight: Literal[400, 500, 600, 700, 800, 900] = 800
    ink: Hex = "#ffffff"
    background: Hex = "#111111"
    duration_s: float = Field(1.5, ge=0.8, le=3.0)


class Pacing(Model):
    cut_every_s: tuple[float, float] = (5.0, 8.0)
    first_cut_by_s: float = Field(6.0, ge=1.0, le=12.0)

    @model_validator(mode="after")
    def _ordered(self) -> Pacing:
        low, high = self.cut_every_s
        if not 1.0 <= low <= high <= 15.0:
            raise ValueError("cut_every_s must be [min, max] seconds with 1 <= min <= max <= 15")
        return self


Energy = Literal["low", "medium", "high"]


def energy_band(tempo_bpm: float) -> Energy:
    """A tempo's energy band: the one rule looks and the music pool share (ADR 0023)."""
    return "low" if tempo_bpm < 90 else "medium" if tempo_bpm < 120 else "high"


class MusicFeel(Model):
    energy: Literal["low", "medium", "high"] = "low"
    tempo_bpm: tuple[int, int] = (70, 100)
    gain_db: float = Field(-18.0, ge=-30.0, le=-10.0)


class LookConfig(Model):
    id: Slug  # the identity
    version: int = Field(ge=1)
    name: str = Field(min_length=1)
    rationale: str = ""  # why, from the style profile
    grade: Grade = Grade()
    captions: CaptionLook = CaptionLook()
    title_card: TitleCardLook = TitleCardLook()
    pacing: Pacing = Pacing()
    music: MusicFeel = MusicFeel()

    @property
    def ref(self) -> LookRef:
        return LookRef(id=self.id, version=self.version)


DEFAULT_LOOK = LookConfig(id="default", version=1, name="Engine default",
                          rationale="Used until an identity has an approved look.")  # fmt: skip


def look_path(folder: Path, identity: str, version: int) -> Path:
    return folder / f"{identity}-v{version}.yaml"


def load_look(path: Path) -> LookConfig:
    return LookConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def save_look(look: LookConfig, folder: Path) -> Path:
    """Write a look version; an existing version is never overwritten (§9.2)."""
    path = look_path(folder, look.id, look.version)
    folder.mkdir(parents=True, exist_ok=True)
    data = look.model_dump(mode="json")
    with path.open("x", encoding="utf-8") as handle:  # "x": fails if it exists
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
    return path


def find_look(folders: Iterable[Path], ref: LookRef) -> LookConfig | None:
    for folder in folders:
        path = look_path(folder, ref.id, ref.version)
        if path.is_file():
            return load_look(path)
    return None


def apply_grade(grade: Grade, rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """One colour through the grade: lift and gamma, contrast around mid grey, saturation
    around luma, then temperature and tint; every channel clamped to 0-1."""

    def clamp(v: float) -> float:
        return min(1.0, max(0.0, v))

    out = []
    for v in rgb:
        v = clamp(grade.lift + v * (1 - grade.lift))
        v = v ** (1 / grade.gamma)
        out.append(clamp((v - 0.5) * grade.contrast + 0.5))
    r, g, b = out
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    r, g, b = (luma + (c - luma) * grade.saturation for c in (r, g, b))
    return clamp(r + grade.temperature), clamp(g - grade.tint), clamp(b - grade.temperature)


def grade_lut(grade: Grade, size: int = 33) -> str:
    """The grade as a .cube 3D LUT (red varies fastest, as the format requires)."""
    lines = ['TITLE "content-engine look"', f"LUT_3D_SIZE {size}",
             "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]  # fmt: skip
    step = 1 / (size - 1)
    for b in range(size):
        for g in range(size):
            for r in range(size):
                red, green, blue = apply_grade(grade, (r * step, g * step, b * step))
                lines.append(f"{red:.6f} {green:.6f} {blue:.6f}")
    return "\n".join(lines) + "\n"


def is_neutral(grade: Grade) -> bool:
    return grade == Grade()


def caption_style(look: LookConfig) -> tuple[CaptionStyle, int]:
    c = look.captions
    style = CaptionStyle(font_family=c.font_family, font_weight=c.font_weight,
                         font_size=c.font_size, max_width=c.max_width, bottom=c.bottom,
                         ink=c.ink, accent=c.accent, outline=c.outline,
                         outline_px=c.outline_px)  # fmt: skip
    return style, c.max_words


def active_look_ref(identity: IdentityConfig) -> LookRef:
    """The identity's approved look, or version 1 (the default look) before any approval."""
    if identity.active_look:
        match = VERSIONED.match(Path(identity.active_look).stem)
        if match:
            return LookRef(id=match["id"], version=int(match["version"]))
    return LookRef(id=identity.id, version=1)
