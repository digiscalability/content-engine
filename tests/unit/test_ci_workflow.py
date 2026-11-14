"""The GitHub Actions workflow (P1.8, ADR 0024): the pure-module tests and ruff, with no keys,
GPU, models, ffmpeg or network, on every push and pull request, here and in the mirror."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CI_MARKERS = "not golden and not gpu and not live and not model and not media"


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(text) -> dict:
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def steps(workflow) -> list[dict]:
    (job,) = workflow["jobs"].values()
    return job["steps"]


def test_ci_runs_on_push_and_pull_request(workflow):
    triggers = workflow[True]  # PyYAML reads the bare key `on` as True
    assert "push" in triggers and "pull_request" in triggers


def test_ci_has_read_only_permissions_and_no_secrets(workflow, text):
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text


def test_ci_actions_are_pinned_to_commits(steps):
    uses = [step["uses"] for step in steps if "uses" in step]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_ci_runs_the_local_commands(steps):
    runs = [line.strip() for step in steps for line in step.get("run", "").splitlines()]
    for command in (
        "uv sync --locked",
        "uv run ruff check .",
        "uv run ruff format --check .",
        f'uv run pytest -m "{CI_MARKERS}"',
    ):
        assert command in runs, command


def test_each_step_runs_one_command(steps):
    """Windows runners use PowerShell, which fails a multi-line step only on its last
    command's exit code: a failing `ruff check` followed by a clean `ruff format` would pass."""
    for step in steps:
        lines = [line for line in step.get("run", "").splitlines() if line.strip()]
        assert len(lines) <= 1, step


def test_ci_runs_from_the_system_drive(workflow, steps):
    """Runners check out on D: while temp folders are on C:, and guard H2 reads only the
    repo's own drive, so the job copies the checkout to C: and runs there, as on the build
    machine (ADR 0024)."""
    (job,) = workflow["jobs"].values()
    workdir = job["defaults"]["run"]["working-directory"]
    assert workdir.upper().startswith("C:\\")
    copy = next(step for step in steps if workdir in step.get("run", ""))
    assert copy["working-directory"] == "${{ github.workspace }}"
    after = steps[steps.index(copy) + 1 :]
    assert all("working-directory" not in step for step in after if "run" in step)


def test_ci_excludes_every_registered_marker(steps):
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    registered = {m.split(":")[0] for m in pyproject["tool"]["pytest"]["ini_options"]["markers"]}
    (pytest_line,) = [
        line for step in steps for line in step.get("run", "").splitlines() if "pytest" in line
    ]
    excluded = set(re.findall(r"not (\w+)", pytest_line))
    assert excluded == registered


def test_ci_is_offline(workflow):
    (job,) = workflow["jobs"].values()
    env = {**workflow.get("env", {}), **job.get("env", {})}
    assert env.get("CE_OFFLINE") == "1" and env.get("HF_HUB_OFFLINE") == "1"
