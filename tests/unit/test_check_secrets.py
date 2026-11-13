"""Secret scan (NFR-7, C6). Fake keys are built at runtime so this file passes the scan."""

import pytest

FAKE_ANTHROPIC = "sk-ant-" + "x" * 40
FAKE_ELEVENLABS = "sk_" + "a" * 48
FAKE_JWT = ".".join(["eyJ" + "a" * 12, "b" * 12, "c" * 12])
FAKE_PRIVATE_KEY = "-----BEGIN " + "PRIVATE KEY-----"
FAKE_QUOTED = 'api_key = "' + "Zx1" * 10 + '"'  # mixed case: ALL_CAPS reads as a name


@pytest.mark.parametrize(
    ("line", "name"),
    [
        (FAKE_ANTHROPIC, "Anthropic key"),
        ("xi = '" + FAKE_ELEVENLABS + "'", "ElevenLabs key"),
        (FAKE_JWT, "JWT"),
        (FAKE_PRIVATE_KEY, "Service account private key"),
        (FAKE_QUOTED, "Generic assignment"),
    ],
)
def test_detects_vendor_key_formats(check_secrets, line, name):
    findings = check_secrets.scan_text("notes.txt", "first line\n" + line + "\n")
    assert f"notes.txt:2: possible {name}" in findings


def test_filled_env_example_is_blocked(check_secrets):
    filled = "# G0.9a\nPEXELS_API_KEY=" + "abc123" * 5 + "\n"
    assert check_secrets.scan_text("template", filled) == ["template:2: possible Dotenv assignment"]


def test_empty_env_example_lines_pass(check_secrets):
    template = "# G0.9a Pexels\nPEXELS_API_KEY=\nREDDIT_CLIENT_ID=\n"
    assert check_secrets.scan_text("template", template) == []


def test_env_var_names_are_not_values(check_secrets):
    text = 'token: "ANTHROPIC_API_KEY_NAME_HERE"\nkey = os.environ["POSTIZ_API_KEY"]\n'
    assert check_secrets.scan_text("cfg.yaml", text) == []


def test_allow_marker(check_secrets):
    text = FAKE_ANTHROPIC + "  # check-secrets: allow\n"
    assert check_secrets.scan_text("doc.md", text) == []


def test_binary_text_is_skipped(check_secrets):
    assert check_secrets.scan_text("clip.mp4", "\x00\x01" + FAKE_ANTHROPIC) == []


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", "config/.env.prod", "SECRETS.md", "a/serviceAccount.json", "k.pem"],
)
def test_secret_bearing_paths(check_secrets, path):
    assert check_secrets.is_secret_bearing_path(path)


@pytest.mark.parametrize("path", [".env.example", "README.md", "docs/env.md", "engine/settings.py"])
def test_ordinary_paths(check_secrets, path):
    assert not check_secrets.is_secret_bearing_path(path)


def test_scan_paths_reports_secret_bearing_file_type(check_secrets):
    findings = check_secrets.scan_paths(["config/.env.prod"], read=lambda p: "X=1\n")
    assert findings == ["config/.env.prod: secret-bearing file type must not be committed"]


def test_main_returns_1_on_finding_and_0_when_clean(check_secrets, tmp_path, capsys):
    leak = tmp_path / "leak.txt"
    leak.write_text(FAKE_ANTHROPIC + "\n", encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing to see\n", encoding="utf-8")

    assert check_secrets.main([str(clean)]) == 0
    assert check_secrets.main([str(leak)]) == 1
    err = capsys.readouterr().err
    assert "Secret scan blocked this commit" in err
    assert FAKE_ANTHROPIC not in err


@pytest.mark.parametrize(
    "line",
    [
        'SIDECHAIN = "sidechaincompress=threshold=0.001:ratio=20"',
        "WIDTH_GUIDE=abcdefghijklmnopqrstuv",
    ],
)
def test_dotenv_rule_needs_a_whole_name_segment(check_secrets, line):
    assert check_secrets.scan_text("x.py", line + "\n") == []


@pytest.mark.parametrize("name", ["REDDIT_CLIENT_ID", "ID_TOKEN", "POSTIZ_API_KEY", "DB_PASSWORD"])
def test_dotenv_rule_still_catches_secret_names(check_secrets, name):
    assert check_secrets.scan_text("x", f"{name}=" + "abc123" * 5 + "\n") == [
        "x:1: possible Dotenv assignment"
    ]


# Across history (P0.7b done-when: "secret scan clean across history") -------------------------


def git(repo, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "core.hooksPath=no-hooks", *args],
        cwd=repo, check=True, capture_output=True,
    )  # fmt: skip


def test_history_scan_finds_a_key_that_was_committed_and_removed(check_secrets, tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "notes.txt").write_text("key " + FAKE_ANTHROPIC + "\n", encoding="utf-8")
    git(tmp_path, "add", "notes.txt")
    git(tmp_path, "commit", "-q", "-m", "oops")
    (tmp_path / "notes.txt").write_text("clean now\n", encoding="utf-8")
    git(tmp_path, "commit", "-q", "-am", "remove the key")
    findings, scanned = check_secrets.scan_history(tmp_path)
    assert scanned == 2  # both versions of notes.txt
    assert len(findings) == 1 and "notes.txt" in findings[0] and "Anthropic key" in findings[0]


def test_history_scan_flags_a_secret_bearing_file_ever_committed(check_secrets, tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "deploy.key").write_text("nothing yet\n", encoding="utf-8")
    git(tmp_path, "add", "deploy.key")
    git(tmp_path, "commit", "-q", "-m", "add")
    git(tmp_path, "rm", "-q", "deploy.key")
    git(tmp_path, "commit", "-q", "-m", "drop")
    findings, _ = check_secrets.scan_history(tmp_path)
    assert findings == ["deploy.key: secret-bearing file type must not be committed"]
