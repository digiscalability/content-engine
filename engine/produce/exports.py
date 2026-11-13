"""Hand-polish exports (SRD C7, §7.4; ADR 0013): every render writes `<name>.otio` and
`<name>.fcpxml` beside its MP4, so Abbas can open the edit in DaVinci Resolve.

Both come from the edit plan, in whole frames at the render's frame rate:

- OTIO (opentimelineio) is the exact one: V1 the shots (speed as `LinearTimeWarp`, a freeze
  as a `FreezeFrame` clip between the played parts), V2 captions, V3+ overlay blocks,
  A1 voice, A2 music, A3+ SFX. Zoom, crop and annotations ride as clip metadata and a
  marker, because OTIO has no standard effect for them.
- FCPXML 1.8 (written with `xml.etree`) carries the same cuts and layers: the shots on the
  spine, everything else as connected clips (captions and overlays on lanes 1+, voice -1,
  music -2, SFX -3 and down). Retimes are not written: a speed or freeze piece plays at
  1x for its timeline length, with a marker naming the op.

The grade (the look's LUT) is not exported; Resolve applies it as a node.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import opentimelineio as otio

from engine.produce.editplan import EditPlan, Shot


@dataclass(frozen=True)
class ExportMedia:
    assets: Mapping[int, Path]
    durations: Mapping[int, float]  # media length, for available ranges
    sizes: Mapping[int, tuple[int, int]] = field(default_factory=dict)  # video assets
    captions: Path | None = None
    overlays: Mapping[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class Piece:
    """One clip on the picture track: whole frames on the timeline and in the source."""

    asset_id: int
    start: int  # timeline frame
    length: int  # timeline frames
    source: int  # source frame
    speed: float = 1.0
    frozen: bool = False
    shot: Shot | None = None
    note: str = ""


@dataclass(frozen=True)
class Layer:
    """A clip above or below the picture: captions, an overlay, voice, music or SFX."""

    kind: str  # captions, overlay, voice, music, sfx
    name: str
    path: Path
    start: int
    length: int
    source: int = 0
    media_length: int | None = None
    metadata: dict = field(default_factory=dict)


def _f(seconds: float, fps: int) -> int:
    return round(seconds * fps)


def _ops_note(shot: Shot) -> str:
    ops = shot.ops
    notes = []
    if ops.speed != 1.0:
        notes.append(f"speed {ops.speed:g}x")
    if ops.freeze:
        notes.append(f"freeze {ops.freeze.duration:g} s at {ops.freeze.at:g} s")
    if ops.zoom:
        notes.append(f"zoom {ops.zoom.from_scale:g} to {ops.zoom.to_scale:g} ({ops.zoom.ease})")
    if ops.crop:
        notes.append(f"crop {ops.crop.w}x{ops.crop.h} at {ops.crop.x},{ops.crop.y}")
    if ops.annotate:
        notes.append("annotations " + ", ".join(a.block for a in ops.annotate))
    return "; ".join(notes)


def picture_pieces(plan: EditPlan, fps: int) -> list[Piece]:
    """The shots as frame-exact pieces; a freeze splits its shot into played, held, played."""
    pieces: list[Piece] = []
    for start_s, shot in zip(plan.shot_starts(), plan.timeline, strict=True):
        start, end = _f(start_s, fps), _f(start_s + shot.duration, fps)
        source = _f(shot.in_s, fps)
        speed = shot.ops.speed
        note = _ops_note(shot)
        common = {"asset_id": shot.asset_id, "speed": speed, "shot": shot, "note": note}
        freeze = shot.ops.freeze
        if freeze is None:
            pieces.append(Piece(start=start, length=end - start, source=source, **common))
            continue
        before = _f(freeze.at, fps)
        held = _f(freeze.duration, fps)
        frozen_at = source + _f(freeze.at * speed, fps)
        if before:
            pieces.append(Piece(start=start, length=before, source=source, **common))
        pieces.append(Piece(start=start + before, length=held, source=frozen_at, frozen=True,
                            **{**common, "speed": 1.0}))  # fmt: skip
        rest = end - (start + before + held)
        if rest > 0:
            pieces.append(Piece(start=start + before + held, length=rest, source=frozen_at,
                                **common))  # fmt: skip
    return pieces


def layers(plan: EditPlan, media: ExportMedia, fps: int) -> list[Layer]:
    total = _f(plan.duration, fps)
    out: list[Layer] = []
    if plan.captions and media.captions:
        out.append(Layer("captions", "captions", media.captions, 0, total, media_length=total))
    for overlay in plan.placed_overlays():
        if overlay.block in media.overlays:
            length = _f(overlay.duration, fps)
            out.append(Layer("overlay", overlay.block, media.overlays[overlay.block],
                             _f(overlay.start, fps), length, media_length=length,
                             metadata={"kind": overlay.kind}))  # fmt: skip

    def media_frames(asset_id: int) -> int | None:
        seconds = media.durations.get(asset_id)
        return _f(seconds, fps) if seconds else None

    for segment in plan.audio.voice:
        out.append(Layer("voice", media.assets[segment.asset_id].stem,
                         media.assets[segment.asset_id], _f(segment.at, fps),
                         _f(segment.out_s - segment.in_s, fps), source=_f(segment.in_s, fps),
                         media_length=media_frames(segment.asset_id)))  # fmt: skip
    if plan.audio.music:
        bed = plan.audio.music
        out.append(Layer("music", media.assets[bed.asset_id].stem, media.assets[bed.asset_id],
                         0, total, media_length=media_frames(bed.asset_id),
                         metadata={"gain_db": bed.gain_db, "duck_db": bed.duck_db,
                                   "loops": True}))  # fmt: skip
    for hit in plan.audio.sfx:
        length = media_frames(hit.asset_id) or fps
        start = _f(hit.at, fps)
        out.append(Layer("sfx", media.assets[hit.asset_id].stem, media.assets[hit.asset_id],
                         start, min(length, total - start), media_length=length,
                         metadata={"gain_db": hit.gain_db}))  # fmt: skip
    return out


def _lanes(items: list[Layer]) -> list[list[Layer]]:
    """Greedy lanes, so overlapping clips of one kind never share a track."""
    lanes: list[list[Layer]] = []
    for item in sorted(items, key=lambda i: i.start):
        for lane in lanes:
            if lane[-1].start + lane[-1].length <= item.start:
                lane.append(item)
                break
        else:
            lanes.append([item])
    return lanes


# OTIO -----------------------------------------------------------------------------------------


def _rt(frames: int, fps: int) -> otio.opentime.RationalTime:
    return otio.opentime.RationalTime(frames, fps)


def _range(start: int, length: int, fps: int) -> otio.opentime.TimeRange:
    return otio.opentime.TimeRange(_rt(start, fps), _rt(length, fps))


def _reference(path: Path, frames: int | None, fps: int) -> otio.schema.ExternalReference:
    available = _range(0, frames, fps) if frames else None
    return otio.schema.ExternalReference(target_url=path.as_uri(), available_range=available)


def _track(name: str, kind: str, items: list[Layer], fps: int) -> otio.schema.Track:
    track = otio.schema.Track(name=name, kind=kind)
    at = 0
    for item in items:
        if item.start > at:
            track.append(otio.schema.Gap(source_range=_range(0, item.start - at, fps)))
        clip = otio.schema.Clip(
            name=item.name,
            media_reference=_reference(item.path, item.media_length, fps),
            source_range=_range(item.source, item.length, fps),
            metadata={"content_engine": {"kind": item.kind, **item.metadata}},
        )
        track.append(clip)
        at = item.start + item.length
    return track


def plan_timeline(plan: EditPlan, media: ExportMedia, name: str, fps: int) -> otio.schema.Timeline:
    timeline = otio.schema.Timeline(name=name, global_start_time=_rt(0, fps))
    timeline.metadata["content_engine"] = {"look": plan.look.model_dump(), "fps": fps}
    picture = otio.schema.Track(name="V1 picture", kind=otio.schema.TrackKind.Video)
    for piece in picture_pieces(plan, fps):
        frames = (
            _f(media.durations[piece.asset_id], fps) if piece.asset_id in media.durations else None
        )
        clip = otio.schema.Clip(
            name=media.assets[piece.asset_id].stem,
            media_reference=_reference(media.assets[piece.asset_id], frames, fps),
            source_range=_range(piece.source, piece.length, fps),
            metadata={"content_engine": {"role": piece.shot.role if piece.shot else "",
                                         "ops": piece.shot.ops.model_dump(exclude_defaults=True)
                                         if piece.shot else {}}},
        )  # fmt: skip
        if piece.frozen:
            clip.effects.append(otio.schema.FreezeFrame())
        elif piece.speed != 1.0:
            clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=piece.speed))
        if piece.note:
            clip.markers.append(
                otio.schema.Marker(name=piece.note, marked_range=_range(piece.source, 0, fps),
                                   color=otio.schema.MarkerColor.YELLOW)
            )  # fmt: skip
        picture.append(clip)
    timeline.tracks.append(picture)
    found = layers(plan, media, fps)
    video_kinds = [("captions", "V2 captions"), ("overlay", "V3 overlays")]
    audio_kinds = [("voice", "A1 voice"), ("music", "A2 music"), ("sfx", "A3 sfx")]
    for kinds, track_kind in ((video_kinds, otio.schema.TrackKind.Video),
                              (audio_kinds, otio.schema.TrackKind.Audio)):  # fmt: skip
        for kind, label in kinds:
            for n, lane in enumerate(_lanes([i for i in found if i.kind == kind])):
                name = label if n == 0 else f"{label} {n + 1}"
                timeline.tracks.append(_track(name, track_kind, lane, fps))
    return timeline


def write_otio(timeline: otio.schema.Timeline, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    otio.adapters.write_to_file(timeline, str(path))
    return path


# FCPXML ---------------------------------------------------------------------------------------


def _t(frames: int, fps: int) -> str:
    if frames == 0:
        return "0s"
    value = Fraction(frames, fps)
    return f"{value.numerator}/{value.denominator}s" if value.denominator != 1 else f"{value}s"


def write_fcpxml(plan: EditPlan, media: ExportMedia, name: str, fps: int, path: Path) -> Path:
    total = _f(plan.duration, fps)
    root = ET.Element("fcpxml", version="1.8")
    resources = ET.SubElement(root, "resources")
    formats: dict[tuple[int, int], str] = {}

    def video_format(size: tuple[int, int]) -> str:
        if size not in formats:
            formats[size] = f"r{len(formats) + 1}"
            ET.SubElement(resources, "format", id=formats[size], frameDuration=f"1/{fps}s",
                          width=str(size[0]), height=str(size[1]))  # fmt: skip
        return formats[size]

    project_format = video_format((1080, 1920))
    refs: dict[Path, str] = {}

    def asset(path: Path, frames: int, video: bool, size: tuple[int, int] | None = None) -> str:
        if path not in refs:
            refs[path] = f"a{len(refs) + 1}"
            attrs = {"id": refs[path], "name": path.stem, "src": path.as_uri(), "start": "0s",
                     "duration": _t(frames, fps), "hasVideo": "1" if video else "0",
                     "hasAudio": "0" if video else "1"}  # fmt: skip
            if video:
                attrs["format"] = video_format(size or (1080, 1920))
            else:
                attrs.update(audioSources="1", audioRate="48000")  # channels: from the file
            ET.SubElement(resources, "asset", attrs)
        return refs[path]

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="content-engine")
    project = ET.SubElement(event, "project", name=name)
    sequence = ET.SubElement(project, "sequence", format=project_format, duration=_t(total, fps),
                             tcStart="0s", tcFormat="NDF", audioLayout="stereo",
                             audioRate="48k")  # fmt: skip
    spine = ET.SubElement(sequence, "spine")
    parents = []
    for piece in picture_pieces(plan, fps):
        path_ = media.assets[piece.asset_id]
        frames = _f(media.durations.get(piece.asset_id, plan.duration), fps)
        ref = asset(path_, max(frames, piece.source + piece.length), True,
                    media.sizes.get(piece.asset_id))  # fmt: skip
        clip = ET.SubElement(spine, "asset-clip", ref=ref, name=path_.stem,
                             offset=_t(piece.start, fps), start=_t(piece.source, fps),
                             duration=_t(piece.length, fps), tcFormat="NDF")  # fmt: skip
        if piece.note:
            ET.SubElement(clip, "marker", start=_t(piece.source, fps), duration=f"1/{fps}s",
                          value=piece.note)  # fmt: skip
        parents.append((piece, clip))

    lane_of = {"captions": 1, "overlay": 2, "voice": -1, "music": -2, "sfx": -3}
    roles = {"voice": "dialogue", "music": "music", "sfx": "effects"}
    found = layers(plan, media, fps)
    for kind in ("captions", "overlay", "voice", "music", "sfx"):
        for n, lane in enumerate(_lanes([i for i in found if i.kind == kind])):
            lane_number = lane_of[kind] + (n if lane_of[kind] > 0 else -n)
            if kind == "overlay":
                lane_number += len(_lanes([i for i in found if i.kind == "captions"])) - 1
            for item in lane:
                piece, parent = next((p, c) for p, c in reversed(parents) if p.start <= item.start)
                video = kind in ("captions", "overlay")
                ref = asset(item.path, item.media_length or item.length, video)
                local = piece.source + (item.start - piece.start)  # the parent's own time
                attrs = {"ref": ref, "lane": str(lane_number), "name": item.name,
                         "offset": _t(local, fps), "start": _t(item.source, fps),
                         "duration": _t(item.length, fps)}  # fmt: skip
                if not video:
                    attrs["audioRole"] = roles[kind]
                ET.SubElement(parent, "asset-clip", attrs)

    ET.indent(root, space="    ")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ET.tostring(root, encoding="unicode")
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n', encoding="utf-8"
    )
    return path
