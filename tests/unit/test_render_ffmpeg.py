"""Filter graphs and the render pipeline for an edit plan (SRD §7.2, FR-PRD-5). No ffmpeg runs."""

import json
import subprocess
from pathlib import Path

import pytest

from engine.produce.editplan import EditPlan
from engine.produce.ffmpeg import FfmpegError
from engine.produce.render_ffmpeg import (
    RenderError,
    RenderSettings,
    RenderSources,
    audio_graph,
    duck_floor_gain,
    filter_path,
    loudnorm_filter,
    parse_loudnorm,
    render,
    shot_chain,
    video_graph,
)

S = RenderSettings()
LOUDNORM_JSON = {
    "input_i": "-23.51",
    "input_tp": "-9.87",
    "input_lra": "1.20",
    "input_thresh": "-33.80",
    "output_i": "-14.02",
    "output_tp": "-1.00",
    "output_lra": "1.10",
    "output_thresh": "-24.30",
    "normalization_type": "linear",
    "target_offset": "0.02",
}
PLAN = EditPlan.model_validate(
    {
        "timeline": [
            {
                "asset_id": 1,
                "in": 0.0,
                "out": 2.0,
                "role": "avatar",
                "ops": {"zoom": {"from_scale": 1.0, "to_scale": 1.2, "ease": "in_out"}},
            },
            {
                "asset_id": 2,
                "in": 1.0,
                "out": 3.0,
                "role": "broll",
                "ops": {"speed": 2.0, "crop": {"x": 10, "y": 20, "w": 960, "h": 540}},
            },
            {
                "asset_id": 2,
                "in": 4.0,
                "out": 5.0,
                "role": "broll",
                "ops": {
                    "freeze": {"at": 0.5, "duration": 1.0},
                    "annotate": [{"block": "arrow-1", "start": 0.2, "duration": 1.0}],
                },
            },
            {"asset_id": 3, "in": 0.0, "out": 1.0, "role": "card"},
        ],
        "audio": {
            "voice": [{"asset_id": 10, "at": 1.0, "out": 3.0}],
            "music": {"asset_id": 11, "gain_db": -12, "duck_db": 12},
            "sfx": [{"asset_id": 12, "at": 2.0, "gain_db": -6}],
        },
        "overlays": [
            {"block": "lower-third", "kind": "lower_third", "start": 0.5, "duration": 2.0}
        ],
        "captions": {"block": "captions-v1"},
        "look": {"id": "selfhelp", "version": 1},
    }
)
SOURCES = RenderSources(
    assets={n: Path(f"C:/media/a{n}.mp4") for n in (1, 2, 3, 10, 11, 12)},
    overlays={"lower-third": Path("C:/work/lt.mov"), "arrow-1": Path("C:/work/arrow.mov")},
    captions=Path("C:/work/captions.mov"),
    lut=Path("C:/Users/me/looks/self help.cube"),
)


def test_filter_path_escapes_windows_paths():
    assert filter_path(Path("C:/Users/me/look v1.cube")) == "'C\\:/Users/me/look v1.cube'"
    assert filter_path(Path("C:\\x\\it's.cube")) == "'C\\:/x/it'\\''s.cube'"


def test_settings_follow_engine_yaml(repo_root):
    from engine.config import load_config

    settings = RenderSettings.from_engine(load_config(repo_root / "config").engine)
    assert settings == RenderSettings(1080, 1920, 30, 48000, -14.0, -1.0, "h264_nvenc")


def test_duck_floor_gain_math():
    k = duck_floor_gain(12)
    assert k == pytest.approx(1 / (10**0.6 - 1))
    ducked_db = 20 * __import__("math").log10(k / (1 + k))
    assert ducked_db == pytest.approx(-12.0)


def test_shot_chain_trims_fits_and_formats():
    chain = shot_chain(PLAN.timeline[3], 3, "v3", S)
    assert chain.startswith("[3:v]trim=start=0:end=1,setpts=PTS-STARTPTS,fps=30,")
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1" in chain
    assert "trim=end_frame=30" in chain
    assert chain.endswith(",format=yuv420p[v3]")


def test_shot_chain_speed_crop_freeze_zoom():
    speedy = shot_chain(PLAN.timeline[1], 1, "v1", S)
    assert "crop=960:540:10:20,setpts=PTS/2" in speedy
    assert "trim=end_frame=30" in speedy  # 2 s of source at 2x = 1 s
    frozen = shot_chain(PLAN.timeline[2], 2, "v2", S)
    assert "loop=loop=30:size=1:start=15,setpts=N/30/TB" in frozen
    zoomed = shot_chain(PLAN.timeline[0], 0, "v0", S)
    assert "scale=2160:3840,zoompan=z='1+0.2*(1-cos(PI*min(on/59,1)))/2'" in zoomed
    assert ":d=1:s=1080x1920:fps=30" in zoomed


def test_video_graph_grades_before_overlays():
    graph, inputs = video_graph(PLAN, SOURCES, S)
    assert inputs[:4] == [
        SOURCES.assets[1],
        SOURCES.assets[2],
        SOURCES.assets[2],
        SOURCES.assets[3],
    ]
    assert "[v0][v1][v2][v3]concat=n=4:v=1:a=0[vcat]" in graph
    lut = "lut3d=file='C\\:/Users/me/looks/self help.cube':interp=tetrahedral"
    assert graph.index(lut) < graph.index("overlay=")
    assert graph.endswith("format=yuv420p[vout]")


def test_video_graph_places_annotations_and_captions():
    graph, inputs = video_graph(PLAN, SOURCES, S)
    assert inputs[4:] == [
        SOURCES.overlays["lower-third"],
        SOURCES.overlays["arrow-1"],
        SOURCES.captions,
    ]
    assert "[4:v]setpts=PTS-STARTPTS+0.5/TB" in graph
    assert "enable='between(t,0.5,2.5)'" in graph
    assert "[5:v]setpts=PTS-STARTPTS+3.2/TB" in graph
    assert "enable='between(t,3.2,4.2)'" in graph
    assert "enable='between(t,0,6)'" in graph  # captions span the plan


def test_video_graph_missing_source_is_named():
    sources = RenderSources(assets={1: Path("a.mp4")})
    with pytest.raises(RenderError, match="asset 2"):
        video_graph(PLAN, sources, S)


def test_audio_graph_ducks_music_under_voice():
    graph, inputs = audio_graph(PLAN, SOURCES, S)
    assert "[0:a]atrim=start=0:end=3,asetpts=PTS-STARTPTS" in graph
    assert "adelay=1000:all=1[vo0]" in graph
    assert (
        "[mkey][vosc]sidechaincompress=threshold=0.001:ratio=20:attack=20:release=300[mduck]"
        in graph
    )
    k = duck_floor_gain(12)
    assert f"[mdry]volume={k:.6f}[mfloor]" in graph
    assert f"amix=inputs=2:normalize=0,volume={1 / (1 + k):.6f}[mbed]" in graph
    assert "adelay=2000:all=1,volume=-6dB[sx0]" in graph
    assert "amix=inputs=3:normalize=0:duration=longest,apad=whole_dur=6,atrim=0:6[aout]" in graph


def test_audio_graph_loops_music_input():
    _, inputs = audio_graph(PLAN, SOURCES, S)
    assert inputs == [
        ([], SOURCES.assets[10]),
        (["-stream_loop", "-1"], SOURCES.assets[11]),
        ([], SOURCES.assets[12]),
    ]


def test_audio_graph_without_any_audio_is_silence():
    silent = PLAN.model_copy(
        update={"audio": PLAN.audio.model_copy(update={"voice": [], "music": None, "sfx": []})}
    )
    graph, inputs = audio_graph(silent, SOURCES, S)
    assert inputs == []
    assert graph == "anullsrc=r=48000:cl=stereo,atrim=0:6[aout]"


def test_music_without_voice_is_not_ducked():
    no_vo = PLAN.model_copy(update={"audio": PLAN.audio.model_copy(update={"voice": []})})
    graph, _ = audio_graph(no_vo, SOURCES, S)
    assert "sidechaincompress" not in graph


def test_parse_loudnorm():
    stderr = "[Parsed_loudnorm_0 @ 0000] \n" + json.dumps(LOUDNORM_JSON, indent=1) + "\n"
    assert parse_loudnorm(stderr)["input_i"] == "-23.51"
    with pytest.raises(FfmpegError):
        parse_loudnorm("no json here")


def test_loudnorm_filter_is_linear_pass_two():
    f = loudnorm_filter(LOUDNORM_JSON, S)
    assert f.startswith("loudnorm=I=-14.0:TP=-1.0:LRA=20:measured_I=-23.51:measured_TP=-9.87")
    assert "measured_LRA=1.20:measured_thresh=-33.80:offset=0.02:linear=true" in f
    assert f.endswith(",aresample=48000")


def test_silent_mix_skips_loudnorm():
    assert loudnorm_filter({**LOUDNORM_JSON, "input_i": "-inf"}, S) is None


class FakeFfmpeg:
    def __init__(self, fail_stage=None):
        self.calls: list[list[str]] = []
        self.fail_stage = fail_stage

    def __call__(self, argv, extra_env=None):
        self.calls.append(argv)
        stage = (
            "measure"
            if "-f" in argv and "null" in argv
            else "mix"
            if argv[-1].endswith(".wav")
            else "encode"
        )
        if stage == self.fail_stage:
            return subprocess.CompletedProcess(argv, 1, "", f"{stage} broke")
        if stage == "mix":
            Path(argv[-1]).write_bytes(b"RIFF")
        stderr = json.dumps(LOUDNORM_JSON) if stage == "measure" else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)


def test_render_runs_mix_measure_encode_in_order(tmp_path):
    run = FakeFfmpeg()
    result = render(PLAN, SOURCES, tmp_path / "out.mp4", tmp_path / "work", S, run=run)
    mix, measure, encode = run.calls
    assert mix[-1] == str(tmp_path / "work" / "mix.wav")
    assert "loudnorm=I=-14.0:TP=-1.0:LRA=20:print_format=json" in measure
    assert encode[-1] == str(tmp_path / "out.mp4")
    assert encode[encode.index("-c:v") + 1] == "h264_nvenc"
    assert encode[encode.index("-t") + 1] == "6"
    assert "+bitexact" in encode
    assert result.duration == 6.0 and result.measured["input_i"] == "-23.51"


def test_render_reads_graphs_from_files(tmp_path):
    run = FakeFfmpeg()
    render(PLAN, SOURCES, tmp_path / "out.mp4", tmp_path / "work", S, run=run)
    mix, _, encode = run.calls
    audio_file = Path(mix[mix.index("-/filter_complex") + 1])
    video_file = Path(encode[encode.index("-/filter_complex") + 1])
    assert "[aout]" in audio_file.read_text(encoding="utf-8")
    final = video_file.read_text(encoding="utf-8")
    assert "[vout]" in final and "loudnorm=" in final and "[aout]" in final


def test_render_failure_names_the_stage(tmp_path):
    with pytest.raises(FfmpegError, match="encode"):
        render(PLAN, SOURCES, tmp_path / "out.mp4", tmp_path / "w", S, run=FakeFfmpeg("encode"))


def test_mix_audio_alone_writes_the_mix(tmp_path):
    from engine.produce.render_ffmpeg import mix_audio

    run = FakeFfmpeg()
    mix = mix_audio(PLAN, SOURCES, tmp_path / "work", S, run=run)
    assert mix == tmp_path / "work" / "mix.wav" and mix.is_file()
    assert len(run.calls) == 1


def test_render_reuses_a_given_mix(tmp_path):
    mix = tmp_path / "final mix.wav"
    mix.write_bytes(b"RIFF")
    run = FakeFfmpeg()
    render(PLAN, SOURCES, tmp_path / "out.mp4", tmp_path / "work", S, run=run, mix=mix)
    measure, encode = run.calls
    assert str(mix) in measure
    assert encode[encode.index(str(mix)) - 1] == "-i"
