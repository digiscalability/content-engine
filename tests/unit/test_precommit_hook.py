"""The pre-commit hook blocks a planted fake key (P0.1 done-when, NFR-7)."""

import shutil
import subprocess
from pathlib import Path

import pytest

FAKE_ANTHROPIC = "sk-ant-" + "x" * 40


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )


@pytest.fixture
def hook_repo(tmp_path: Path, repo_root: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q", "-b", "main").returncode == 0
    git(repo, "config", "user.name", "Hook Test")
    git(repo, "config", "user.email", "hook-test@example.invalid")
    for rel in (".githooks/pre-commit", "scripts/check_secrets.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_root / rel, repo / rel)
    git(repo, "config", "core.hooksPath", ".githooks")
    return repo


def commit_count(repo: Path) -> int:
    out = git(repo, "rev-list", "--all", "--count").stdout.strip()
    return int(out or 0)


def stage_and_commit(repo: Path, name: str, text: str) -> subprocess.CompletedProcess[str]:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "--", name)
    return git(repo, "commit", "-q", "-m", f"add {name}")


def test_hook_blocks_planted_fake_key(hook_repo: Path):
    result = stage_and_commit(hook_repo, "leak.txt", "ANTHROPIC_API_KEY=" + FAKE_ANTHROPIC + "\n")
    assert result.returncode != 0
    assert "Secret scan blocked this commit" in result.stderr
    assert "leak.txt:1" in result.stderr
    assert FAKE_ANTHROPIC not in result.stderr
    assert commit_count(hook_repo) == 0


def test_hook_allows_clean_commit(hook_repo: Path):
    result = stage_and_commit(hook_repo, "notes.txt", "nothing secret here\n")
    assert result.returncode == 0, result.stderr
    assert commit_count(hook_repo) == 1


def test_hook_blocks_dotenv_file(hook_repo: Path):
    result = stage_and_commit(hook_repo, ".env.local", "X=1\n")
    assert result.returncode != 0
    assert ".env.local: secret-bearing file type" in result.stderr


def test_hook_scans_non_ascii_file_names(hook_repo: Path):
    result = stage_and_commit(hook_repo, "notes — key.txt", FAKE_ANTHROPIC + "\n")
    assert result.returncode != 0
    assert "possible Anthropic key" in result.stderr
