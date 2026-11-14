"""`ce look propose` and `ce look approve` (SRD §9.2, D8).

A `LookProposer` turns the identity's style profile into looks. P0.8.1 ships
`ScriptedProposer`, three variants by fixed rules with no model; P0.8.2 adds Claude. Each
proposed look renders the same sample post through the dry-run pipeline, side by side in
a contact sheet. `approve_look` saves the chosen look as the next version under
`config/looks/`, makes it the identity's active look and records it in `looks`. ADR 0015.
"""

from __future__ import annotations

import colorsys
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from engine.agents.dryrun import direct, start_dry_run
from engine.config import Config
from engine.produce.overlay import OverlayRenderer
from engine.produce.process import Runner, output_tail, run_command
from engine.style.look import (
    VERSIONED,
    CaptionLook,
    Grade,
    LookConfig,
    MusicFeel,
    Pacing,
    energy_band,
    load_look,
    look_path,
    save_look,
)

_ACTIVE = re.compile(r"^(active_look:\s*)(\S+)(\s*#.*)?$", re.MULTILINE)


class LookProposer(Protocol):
    def propose(self, identity: str, profile: dict | None, count: int) -> list[LookConfig]: ...


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _hsv(swatch: str) -> tuple[float, float, float]:
    r, g, b = (int(swatch[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)


def _accent(palette: list[str], default: str = "#ffd23f") -> str:
    """The palette's most saturated swatch that is bright enough to read on a caption."""
    bright = [s for s in palette if _hsv(s)[2] >= 0.5 and _hsv(s)[1] >= 0.3]
    return max(bright, key=lambda s: _hsv(s)[1]) if bright else default


@dataclass
class ScriptedProposer:
    """Three looks from the profile by fixed rules: the references' own measure, a warmer
    slower one and a cooler faster one. No model; dry runs and P0.8.1 use it."""

    def propose(self, identity: str, profile: dict | None, count: int = 3) -> list[LookConfig]:
        m = (profile or {}).get("metrics") or {}
        palette = (profile or {}).get("palette") or []
        median = m.get("shot_median_s")
        low = _clamp(round(median * 1.5, 1), 2.0, 10.0) if median else 5.0
        high = _clamp(round(median * 3.0, 1), low, 12.0) if median else 8.0
        y, h = m.get("caption_y"), m.get("caption_height")
        bottom = round(_clamp((1 - y) * 1920 - h * 1920 / 2, 420, 900)) if y and h else 650
        words = int(_clamp(m.get("words_per_page") or 4, 2, 6))
        tempo = m.get("tempo_bpm") or 90.0
        music = MusicFeel(energy=energy_band(tempo), tempo_bpm=(int(tempo - 15), int(tempo + 15)))
        saturation = _clamp(1 + ((m.get("saturation") or 0.35) - 0.35) * 0.5, 0.8, 1.2)
        gamma = _clamp(1 + (0.5 - (m.get("brightness") or 0.5)) * 0.4, 0.9, 1.15)
        source = f"{(profile or {}).get('ref_count', 0)} reference shorts"
        base = CaptionLook(bottom=bottom, max_words=words, accent=_accent(palette))
        looks = [
            LookConfig(
                id=identity, version=1, name="Measured",
                rationale=f"The {source} as measured: cuts every {low:g}-{high:g} s, "
                          f"{words} words a caption page, their light and colour.",
                grade=Grade(saturation=round(saturation, 3), gamma=round(gamma, 3)),
                captions=base, pacing=Pacing(cut_every_s=(low, high)), music=music,
            ),
            LookConfig(
                id=identity, version=2, name="Warm craft",
                rationale="Warmer and slower than the references: kitchen light, room "
                          "to think, a heavier serif-free title face.",
                grade=Grade(temperature=0.05, contrast=1.08, saturation=1.05),
                captions=base.model_copy(update={"font_family": "Montserrat",
                                                 "font_weight": 800, "ink": "#fff8ec"}),
                pacing=Pacing(cut_every_s=(min(low + 1, 12.0), min(high + 1, 12.0))),
                music=music.model_copy(update={"energy": "low"}),
            ),
            LookConfig(
                id=identity, version=3, name="Cool focus",
                rationale="Cooler and quicker than the references: crisp contrast, "
                          "condensed captions, fewer words a page.",
                grade=Grade(temperature=-0.04, tint=0.01, contrast=1.15, saturation=0.9),
                captions=base.model_copy(update={"font_family": "Oswald", "font_weight": 700,
                                                 "max_words": max(2, words - 1),
                                                 "accent": "#7cc4ff"}),
                pacing=Pacing(cut_every_s=(max(2.0, low - 1), max(2.0, high - 1))),
                music=music.model_copy(update={"energy": "medium"}),
            ),
        ]  # fmt: skip
        return looks[: max(1, min(count, 3))]


def latest_profile(conn: sqlite3.Connection, identity: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM style_profiles WHERE identity_id = ? ORDER BY measured_at DESC, id DESC "
        "LIMIT 1",
        (identity,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "metrics": json.loads(row["metrics"]),
        "palette": json.loads(row["palette"] or "[]"),
        "ref_count": row["ref_count"],
    }


def _next_version(folder: Path, identity: str) -> int:
    versions = [int(m["version"]) for p in folder.glob(f"{identity}-v*.yaml")
                if (m := VERSIONED.match(p.stem)) and m["id"] == identity]  # fmt: skip
    return max(versions, default=0) + 1


def set_active_look(identity_file: Path, relative: str) -> None:
    """Point the identity at its look, keeping the line's comment and every other line."""
    text = identity_file.read_text(encoding="utf-8")
    if not _ACTIVE.search(text):
        raise ValueError(f"{identity_file} has no active_look line")
    text = _ACTIVE.sub(lambda m: f"{m[1]}{relative}{m[3] or ''}", text, count=1)
    identity_file.write_text(text, encoding="utf-8")


def approve_look(
    config_dir: Path, conn: sqlite3.Connection, identity: str, proposal: Path, number: int
) -> Path:
    source = look_path(proposal, identity, number)
    if not source.is_file():
        raise ValueError(f"the proposal in {proposal} has no look {number}")
    looks_dir = config_dir / "looks"
    version = _next_version(looks_dir, identity)
    path = save_look(load_look(source).model_copy(update={"version": version}), looks_dir)
    set_active_look(config_dir / "identities" / f"{identity}.yaml", f"looks/{path.name}")
    conn.execute(
        "INSERT INTO looks (identity_id, version, path, approved_at) VALUES (?, ?, ?, ?)",
        (identity, version, f"looks/{path.name}", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    return path


@dataclass
class Proposal:
    folder: Path
    looks: list[LookConfig]
    samples: list[Path]
    contact: Path | None


def render_samples(
    config: Config,
    identity: str,
    looks: list[LookConfig],
    folder: Path,
    run: Runner = run_command,
    overlay: OverlayRenderer | None = None,
    today: date | None = None,
) -> list[Path]:
    """The same sample post under each look, through the dry-run pipeline (vendors faked)."""
    samples = []
    for look in looks:
        ctx = start_dry_run(config, folder / "samples", f"look-{look.version}", today=today,
                            run=run, overlay=overlay)  # fmt: skip
        ctx.look_dirs = [folder]
        report, produced = direct(ctx, only=identity, look_version=look.version)
        made = produced[0] if produced else None
        if made is None or made.mp4 is None:
            failures = [f.reason for r in report.identity_results for f in r.posts_failed]
            raise RuntimeError(f"sample for look {look.version} failed: {'; '.join(failures)}")
        samples.append(Path(made.mp4))
    return samples


def contact_sheet(samples: list[Path], output: Path, at: float = 3.0,
                  run: Runner = run_command) -> Path:  # fmt: skip
    """One frame of each sample, side by side in proposal order."""
    argv = ["ffmpeg", "-v", "error", "-y"]
    for sample in samples:
        argv += ["-ss", str(at), "-i", str(sample)]
    scaled = ";".join(f"[{i}:v]scale=360:640[s{i}]" for i in range(len(samples)))
    stack = "".join(f"[s{i}]" for i in range(len(samples)))
    graph = (
        f"{scaled};{stack}hstack=inputs={len(samples)}[out]"
        if len(samples) > 1
        else ("[0:v]scale=360:640[out]")
    )
    argv += ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", str(output)]
    result = run(argv, None)
    if result.returncode != 0:
        raise RuntimeError(f"contact sheet failed: {output_tail(result)}")
    return output


def propose_looks(
    config: Config,
    conn: sqlite3.Connection,
    identity: str,
    proposer: LookProposer,
    root: Path,
    count: int = 3,
    run: Runner = run_command,
    overlay: OverlayRenderer | None = None,
    today: date | None = None,
) -> Proposal:
    profile = latest_profile(conn, identity)
    if profile is None:
        raise ValueError(
            f"{identity} has no style profile yet: add reference shorts (G0.8) and run "
            f"`ce refs analyze {identity}` first"
        )
    folder = root / identity / datetime.now().strftime("proposal-%Y%m%d-%H%M%S")
    looks = proposer.propose(identity, profile, count)
    for look in looks:
        save_look(look, folder)
    samples = render_samples(config, identity, looks, folder, run, overlay, today)
    contact = contact_sheet(samples, folder / "contact.png", run=run)
    summary = {"profile_id": profile["id"], "looks": [
        {"number": lk.version, "name": lk.name, "rationale": lk.rationale, "sample": str(s)}
        for lk, s in zip(looks, samples, strict=True)]}  # fmt: skip
    (folder / "proposal.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return Proposal(folder=folder, looks=looks, samples=samples, contact=contact)


def latest_proposal(root: Path, identity: str) -> Path | None:
    folders = sorted((root / identity).glob("proposal-*")) if (root / identity).is_dir() else []
    return folders[-1] if folders else None
