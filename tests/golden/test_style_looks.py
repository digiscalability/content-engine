"""P0.8.1 end to end: generated reference shorts measured by real ffmpeg and RapidOCR, a
stored style profile, three looks rendered as the same sample post, and an approval.
Runs ffmpeg (NVENC), HyperFrames and OCR for real, about four minutes."""

import json
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.agents.state import sync_identities
from engine.config import load_config
from engine.db import open_db
from engine.produce.ffmpeg import probe
from engine.settings import REPO_ROOT
from engine.style.ocr import RapidOcrDetector
from engine.style.profile import MeasureDeps, analyze_refs, measure_reference
from engine.style.propose import ScriptedProposer, approve_look, propose_looks

from . import media

pytestmark = pytest.mark.golden

FONT = "C\\:/Windows/Fonts/arialbd.ttf"
CARD = (40, 80, 200, 200)  # top-left of the placeholder presenter card, away from captions


def reference(path: Path, colours: list[str], text: str, beat_s: float) -> Path:
    """A 6 s vertical short: three 2 s shots, a caption line, a click every `beat_s`."""
    shots = ";".join(f"color=c={c}:s=1080x1920:r=30:d=2[s{i}]" for i, c in enumerate(colours))
    joined = "".join(f"[s{i}]" for i in range(len(colours)))
    video = (f"{shots};{joined}concat=n={len(colours)}:v=1:a=0,drawtext=fontfile='{FONT}':"
             f"text='{text}':fontsize=84:fontcolor=white:borderw=6:bordercolor=black:"
             f"x=(w-text_w)/2:y=1300")  # fmt: skip
    clicks = f"aevalsrc='if(lt(mod(t\\,{beat_s})\\,0.012)\\,0.8\\,0)':s=44100:d=6"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", video, "-f", "lavfi",
                    "-i", clicks, "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(path)], check=True)  # fmt: skip
    return path


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    for tool in ("ffmpeg", "ffprobe", "npx"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH")
    if not Path("C:/Windows/Fonts/arialbd.ttf").is_file():
        pytest.skip("needs Arial Bold to draw the reference captions")
    root = tmp_path_factory.mktemp("style looks")
    config_dir = root / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    inbox = root / "inbox"
    inbox.mkdir()
    reference(inbox / "a.mp4", ["0xe08a2a", "0x1f6f8b", "0x202020"], "START SMALL TODAY", 0.6)
    reference(inbox / "b.mp4", ["0x202020", "0xe08a2a", "0x2a2a5a"], "KEEP IT TINY", 0.5)
    conn = open_db(root / "engine.db")
    sync_identities(conn, load_config(config_dir))
    deps = MeasureDeps(detector=RapidOcrDetector())
    profile = analyze_refs(conn, "selfhelp", inbox, lambda v: measure_reference(v, deps))
    proposal = propose_looks(
        load_config(config_dir), conn, "selfhelp", ScriptedProposer(), root / "looks"
    )
    return SimpleNamespace(config_dir=config_dir, conn=conn, inbox=inbox, profile=profile,
                           proposal=proposal)  # fmt: skip


def test_references_are_measured_stored_then_deleted(world):
    m = world.profile["metrics"]
    assert m["cuts_per_min"] == pytest.approx(20.0, abs=0.5)
    assert m["shot_median_s"] == pytest.approx(2.0, abs=0.1)
    assert m["tempo_bpm"] == pytest.approx(110.0, abs=4)  # the median of 100 and 120 bpm
    assert m["caption_y"] == pytest.approx((1300 + 40) / 1920, abs=0.02)
    assert 2 <= m["words_per_page"] <= 3  # estimated where OCR drops the spaces
    assert m["text_frame_share"] > 0.9
    orange = [s for s in world.profile["palette"] if int(s[1:3], 16) > int(s[5:7], 16) + 80]
    assert orange, world.profile["palette"]
    row = world.conn.execute("SELECT ref_count FROM style_profiles").fetchone()
    assert row["ref_count"] == 2
    assert list(world.inbox.iterdir()) == []  # C10


def test_three_looks_render_the_same_post_differently(world):
    samples = world.proposal.samples
    assert len(samples) == 3
    infos = [probe(s) for s in samples]
    assert {(i.width, i.height, i.video_codec) for i in infos} == {(1080, 1920, "h264")}
    assert len({round(i.duration, 2) for i in infos}) == 1  # the same post
    for look, sample in zip(world.proposal.looks, samples, strict=True):
        report = json.loads((sample.parent / "render_report.json").read_text("utf-8"))
        assert report["look"]["applied"] == look.name
        assert report["look"]["file"] == f"selfhelp-v{look.version}.yaml"
    warm = media.rgb_means(samples[1], 1.0, CARD)
    cool = media.rgb_means(samples[2], 1.0, CARD)
    assert warm[0] - warm[2] > cool[0] - cool[2] + 10  # the grades differ where it shows


def test_the_contact_sheet_shows_all_three(world):
    header = world.proposal.contact.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", header[16:24])  # the IHDR chunk
    assert (width, height) == (3 * 360, 640)


def test_approving_saves_v1_and_makes_it_active(world):
    path = approve_look(world.config_dir, world.conn, "selfhelp", world.proposal.folder, 2)
    assert path == world.config_dir / "looks" / "selfhelp-v1.yaml"
    selfhelp = load_config(world.config_dir).identities["selfhelp"]
    assert selfhelp.active_look == "looks/selfhelp-v1.yaml"
    assert world.conn.execute("SELECT version FROM looks").fetchone()[0] == 1
