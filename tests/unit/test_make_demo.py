"""scripts/make_demo.py: the replay's timing comes from the run's audit log, and the long
render is the only part shown faster (and says so)."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo.py"


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("make_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_the_long_render_gets_a_sped_up_bar(demo):
    rows = [("get_state", "ok", 0), ("render_post", "ok", 60_000), ("create_draft", "ok", 16)]
    events, end = demo.tool_timeline(rows)
    assert [e["at"] for e in events] == pytest.approx([2.1, 2.42, 7.74])
    assert [e["bar"] for e in events] == [0, 5.0, 0]
    assert events[1]["speedup"] == pytest.approx(12.0) and events[1]["took"] == "60.0 s"
    assert events[2]["took"] == "16 ms" and end == pytest.approx(8.06)


def test_summary_lists_what_the_run_wrote(demo, tmp_path):
    run = tmp_path / "dry-1"
    (run / "renders" / "selfhelp-1-1").mkdir(parents=True)
    report = {"identity_results": [{"posts_drafted": 1}]}
    (run / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    lines = demo.summary_lines(run)
    assert lines[0] == "Run dry-1 wrote (1 fake Postiz draft):"
    assert [Path(line.strip()).name for line in lines[1:]] == [
        "selfhelp-1-1.mp4", "render_report.json", "selfhelp-1-1.otio", "selfhelp-1-1.fcpxml",
        "run_report.json",
    ]  # fmt: skip
