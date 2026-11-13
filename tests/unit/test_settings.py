"""Vendor keys load into a settings object, never os.environ (SRD §3.4, §12, FR-AGT-1)."""

import os
from pathlib import Path

from engine.settings import KEY_SPECS, load_settings

FAKE = "fake-" + "q" * 30
NAMES = [spec.name for spec in KEY_SPECS]


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "engine.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_key_specs_follow_srd_section_12():
    assert NAMES == [
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
        "HEYGEN_API_KEY",
        "POSTIZ_API_KEY",
        "PEXELS_API_KEY",
        "YOUTUBE_DATA_API_KEY",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
    ]


def test_load_does_not_touch_os_environ(tmp_path):
    before = dict(os.environ)
    load_settings(write_env(tmp_path, f"ANTHROPIC_API_KEY={FAKE}\nPOSTIZ_API_KEY={FAKE}\n"))
    assert dict(os.environ) == before


def test_key_status_set_and_missing(tmp_path):
    settings = load_settings(write_env(tmp_path, f"ANTHROPIC_API_KEY={FAKE}\n"))
    status = settings.key_status()
    assert list(status) == NAMES
    assert status["ANTHROPIC_API_KEY"] is True
    assert not any(v for k, v in status.items() if k != "ANTHROPIC_API_KEY")
    assert settings.get("ANTHROPIC_API_KEY") == FAKE
    assert settings.get("ELEVENLABS_API_KEY") is None


def test_unknown_names_ignored(tmp_path):
    settings = load_settings(write_env(tmp_path, f"SOMETHING_ELSE={FAKE}\n"))
    assert settings.get("SOMETHING_ELSE") is None
    assert "SOMETHING_ELSE" not in settings.key_status()


def test_missing_file_means_all_missing(tmp_path):
    settings = load_settings(tmp_path / "absent.env")
    assert settings.key_status() == {name: False for name in NAMES}


def test_repr_hides_values(tmp_path):
    settings = load_settings(write_env(tmp_path, f"ANTHROPIC_API_KEY={FAKE}\n"))
    assert FAKE not in repr(settings)
    assert FAKE not in str(settings)


def test_odd_values(tmp_path):
    text = (
        "ANTHROPIC_API_KEY=abc=def==\n"
        "ELEVENLABS_API_KEY='   '\n"
        'POSTIZ_API_KEY="quoted-value"\n'
        "PEXELS_API_KEY=\n"
    )
    settings = load_settings(write_env(tmp_path, text))
    assert settings.get("ANTHROPIC_API_KEY") == "abc=def=="
    assert settings.get("POSTIZ_API_KEY") == "quoted-value"
    status = settings.key_status()
    assert status["ANTHROPIC_API_KEY"] and status["POSTIZ_API_KEY"]
    assert not status["ELEVENLABS_API_KEY"]
    assert not status["PEXELS_API_KEY"]
    assert settings.get("ELEVENLABS_API_KEY") is None


def test_env_example_lists_exactly_key_specs(repo_root):
    lines = (repo_root / ".env.example").read_text(encoding="utf-8").splitlines()
    entries = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert [entry.split("=", 1)[0] for entry in entries] == NAMES
    assert all(entry.split("=", 1)[1] == "" for entry in entries), "names only, never values"
