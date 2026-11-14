"""Config models and loader (SRD §12), FR-SRC-1 validation and FR-SRC-3."""

import re
import shutil
import textwrap
from pathlib import Path

import pytest
import yaml

from engine.config import ConfigError, load_config
from engine.config.models import SourcesFile

VALID_SOURCES = r"""
sources:
  - id: own-pics
    kind: folder
    path: 'C:\Users\me\Pictures\Footage'
    rights_type: own
    rights_proof: Filmed by Abbas
  - id: pexels
    kind: library
    url: https://api.pexels.com/v1/videos/
    rights_type: stock
    rights_proof: Pexels licence
    licence_url: https://www.pexels.com/license/
    attribution: Pexels
  - id: films-a
    kind: drive
    volume_serial: 1A2B-3C4D
    volume_label: FILMS_A
    rights_type: film_reference
    rights_proof: Owned copies, breakdowns only under SRD 8.2
"""


@pytest.fixture
def cfg(tmp_path: Path, repo_root: Path) -> Path:
    directory = tmp_path / "config"
    shutil.copytree(repo_root / "config", directory)
    return directory


def write(cfg: Path, rel: str, text: str) -> None:
    (cfg / rel).write_text(textwrap.dedent(text), encoding="utf-8")


def edit_yaml(cfg: Path, rel: str, dotted: str, value) -> None:
    path = cfg / rel
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    *parents, last = dotted.split(".")
    node = data
    for key in parents:
        node = node[key]
    node[last] = value
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def problems(cfg: Path) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        load_config(cfg)
    return exc.value.problems


def one_source(**fields) -> str:
    entry = {
        "id": "s1",
        "kind": "folder",
        "path": "C:/x",
        "rights_type": "own",
        "rights_proof": "Filmed by Abbas",
        **fields,
    }
    entry = {k: v for k, v in entry.items() if v is not None}
    return yaml.safe_dump({"sources": [entry]}, sort_keys=False)


def test_repo_config_loads(repo_root):
    config = load_config(repo_root / "config")
    assert config.engine.agents.orchestrator.model == "claude-opus-5-5"
    assert config.engine.disk.stop_free_gb == 40
    assert [s.id for s in config.sources] == ["own-videos"]  # G0.9c, named by Abbas
    assert set(config.identities) == {"selfhelp"}
    assert config.identities["selfhelp"].autonomy_level == 0


def test_voice_and_avatar_ids_are_empty_until_gate_0b(repo_root, cfg):
    selfhelp = load_config(repo_root / "config").identities["selfhelp"]
    assert (selfhelp.voice_id, selfhelp.avatar_id) == ("", "")  # G0.4, G0.5
    edit_yaml(cfg, "identities/selfhelp.yaml", "voice_id", "v-123")
    edit_yaml(cfg, "identities/selfhelp.yaml", "avatar_id", "a-456")
    loaded = load_config(cfg).identities["selfhelp"]
    assert (loaded.voice_id, loaded.avatar_id) == ("v-123", "a-456")


def test_valid_sources_load(cfg):
    write(cfg, "sources.yaml", VALID_SOURCES)
    sources = load_config(cfg).sources
    assert [s.id for s in sources] == ["own-pics", "pexels", "films-a"]
    assert sources[0].path == r"C:\Users\me\Pictures\Footage"


def test_source_without_rights_type_fails(cfg):
    write(cfg, "sources.yaml", one_source(rights_type=None))
    assert "sources.yaml: sources.0.rights_type: Field required" in problems(cfg)


def test_source_without_rights_proof_fails(cfg):
    write(cfg, "sources.yaml", one_source(rights_proof=None))
    assert "sources.yaml: sources.0.rights_proof: Field required" in problems(cfg)


def test_blank_rights_proof_fails(cfg):
    write(cfg, "sources.yaml", one_source(rights_proof="   "))
    assert any(p.startswith("sources.yaml: sources.0.rights_proof:") for p in problems(cfg))


def test_unknown_rights_type_fails(cfg):
    write(cfg, "sources.yaml", one_source(rights_type="borrowed"))
    assert any(p.startswith("sources.yaml: sources.0.rights_type:") for p in problems(cfg))


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"path": None}, "folder sources need a path"),
        ({"kind": "drive", "path": None, "volume_label": "X"}, "volume_serial"),
        (
            {
                "kind": "channel",
                "url": "https://youtube.com/@x",
                "rights_type": "stock",
                "licence_url": "https://x",
            },
            "FR-ING-2",
        ),
        ({"rights_type": "film_reference"}, "film_reference"),
        ({"kind": "library", "rights_type": "stock"}, "licence_url"),
        ({"kind": "library", "rights_type": "stock", "licence_url": "https://x"}, "attribution"),
        ({"id": "engine"}, "reserved"),
    ],
)
def test_source_rules(cfg, fields, expected):
    write(cfg, "sources.yaml", one_source(**fields))
    assert any(expected in p for p in problems(cfg)), problems(cfg)


def test_duplicate_source_ids_fail(cfg):
    entry = yaml.safe_load(one_source())["sources"][0]
    write(cfg, "sources.yaml", yaml.safe_dump({"sources": [entry, entry]}))
    assert any("duplicate source id" in p for p in problems(cfg))


def test_unknown_key_rejected(cfg):
    write(cfg, "sources.yaml", one_source(rights_proof=None, rights_prof="typo"))
    found = problems(cfg)
    assert any("rights_prof: Extra inputs are not permitted" in p for p in found)
    assert "sources.yaml: sources.0.rights_proof: Field required" in found


def test_numeric_volume_serial_is_read_as_text(cfg):
    write(
        cfg,
        "sources.yaml",
        one_source(
            kind="drive",
            path=None,
            volume_serial=12345678,
            volume_label="FILMS_B",
            rights_type="film_reference",
        ),
    )
    assert load_config(cfg).sources[0].volume_serial == "12345678"


def test_blank_sources_file_is_an_error(cfg):
    write(cfg, "sources.yaml", "# cleared\n")
    found = problems(cfg)
    assert len(found) == 1
    assert found[0].startswith("sources.yaml:")
    assert "sources: []" in found[0]


def test_double_quoted_windows_path_gets_a_hint(cfg):
    write(cfg, "sources.yaml", one_source().replace("C:/x", '"C:\\Users\\abbas\\Pictures"'))
    (found,) = problems(cfg)
    assert found.startswith("sources.yaml: invalid YAML at line")
    assert "single quotes" in found


@pytest.mark.parametrize(
    ("dotted", "value", "expected"),
    [
        ("id", "other", "must match the file name"),
        ("formats", {"C": 0.9}, "formats"),
        ("autonomy_level", 2, "autonomy_level"),
        ("cadence.min_gap_hours", 2, "min_gap_hours"),
        ("film_breakdown_share_max", 0.3, "film_breakdown_share_max"),
        (
            "channels",
            [{"platform": "youtube", "postiz_integration_id": "", "ai_disclosure": "auto"}],
            "GR-5",
        ),
        ("playbook", "identities/missing.playbook.md", "not found"),
        ("channels", [], "channels"),
    ],
)
def test_identity_rules(cfg, dotted, value, expected):
    edit_yaml(cfg, "identities/selfhelp.yaml", dotted, value)
    found = problems(cfg)
    assert any(p.startswith("identities/selfhelp.yaml:") and expected in p for p in found), found


@pytest.mark.parametrize(
    ("dotted", "value", "expected"),
    [
        ("disk.stop_free_gb", 70, "stop_free_gb"),
        ("agents.orchestrator.effort", "extreme", "agents.orchestrator.effort"),
        ("schedule.daily", "4am", "schedule.daily"),
    ],
)
def test_engine_rules(cfg, dotted, value, expected):
    edit_yaml(cfg, "engine.yaml", dotted, value)
    found = problems(cfg)
    assert any(p.startswith("engine.yaml:") and expected in p for p in found), found


def test_problems_from_several_files_are_collected(cfg):
    edit_yaml(cfg, "engine.yaml", "disk.stop_free_gb", 70)
    write(cfg, "sources.yaml", one_source(rights_proof=None))
    found = problems(cfg)
    assert {p.split(":", 1)[0] for p in found} == {"engine.yaml", "sources.yaml"}


def test_paths_resolve_relative_and_expand_env(repo_root, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = load_config(repo_root / "config").engine.paths
    assert paths.resolve("db", repo_root) == repo_root / "data" / "engine.db"
    assert paths.resolve("logs", repo_root) == tmp_path / "content-engine" / "logs"
    with pytest.raises(KeyError):
        paths.resolve("nowhere", repo_root)


def test_no_code_path_writes_sources_yaml(repo_root):
    """FR-SRC-3: new sources come only from Abbas editing the file."""
    naming = sorted(
        p.relative_to(repo_root).as_posix()
        for p in (repo_root / "engine").rglob("*.py")
        if "sources.yaml" in p.read_text(encoding="utf-8")
    )
    assert naming == ["engine/config/loader.py"]
    loader = (repo_root / "engine" / "config" / "loader.py").read_text(encoding="utf-8")
    assert not re.search(r"write_text|write_bytes|\.write\(|open\([^)]*['\"][wax+]", loader)


def test_qa_config_names_the_fact_sheet_and_the_term_lists(repo_root):
    config = load_config(repo_root / "config")
    assert config.engine.qa.fact_sheet.endswith("Public Fact Sheet.md")  # D9
    qa = config.identities["selfhelp"].qa
    categories = {"medical", "mental_health", "diet", "money", "legal", "politics", "religion"}
    assert categories <= set(qa.banned_terms)  # the playbook's banned list (FR-QA-6)
    assert qa.never_terms == []  # FR-QA-8's list comes from the Fact Sheet at run time
    assert "Abbas" in qa.self_reference_terms


def test_a_playbook_the_planner_cannot_read_is_a_problem(cfg):
    path = cfg / "identities" / "selfhelp.playbook.md"
    text = path.read_text(encoding="utf-8").replace("#### Focus and deep work", "#### Cooking")
    path.write_text(text, encoding="utf-8")
    found = problems(cfg)
    assert any(
        p.startswith("identities/selfhelp.yaml: playbook:") and "Cooking" in p for p in found
    )


def test_a_library_source_needs_a_path_or_a_url(cfg):
    write(cfg, "sources.yaml", one_source(kind="library", path=None, rights_type="stock",
                                          licence_url="https://x"))  # fmt: skip
    assert any("library sources need a path" in p for p in problems(cfg)), problems(cfg)


def test_the_stock_source_must_be_a_library_api(cfg):
    edit_yaml(cfg, "engine.yaml", "stock.source", "own-videos")
    found = problems(cfg)
    assert any(p.startswith("engine.yaml: stock.source") for p in found), found


def ready_sources(repo_root: Path) -> list:
    """The commented-out entries under READY FOR P0.6b.3 in sources.yaml, uncommented."""
    text = (repo_root / "config" / "sources.yaml").read_text(encoding="utf-8")
    block = text.split("READY FOR P0.6b.3", 1)[1].splitlines()[1:]
    entries = [line for line in block if line.startswith(("  #   ", "  # - "))]
    lines = ["sources:", *(line.replace("  # ", "  ", 1) for line in entries)]
    return SourcesFile.model_validate(yaml.safe_load("\n".join(lines))).sources


def test_the_stock_and_music_sources_are_ready_but_off(repo_root):
    """ADR 0023: written out for P0.6b.3, valid once uncommented, off until Abbas says yes."""
    by_id = {s.id: s for s in ready_sources(repo_root)}
    pexels, music = by_id["pexels"], by_id["youtube-audio-library"]
    assert (pexels.kind, pexels.rights_type, pexels.path) == ("library", "stock", None)
    assert pexels.url.startswith("https://api.pexels.com/")
    assert pexels.licence_url == "https://www.pexels.com/license/"
    assert (music.kind, music.rights_type) == ("library", "stock")
    assert music.path == "media/library/music"
    config = load_config(repo_root / "config")
    assert config.engine.stock.source == "pexels" and config.engine.stock.daily_cap >= 1
    assert "pexels" not in {s.id for s in config.sources}
