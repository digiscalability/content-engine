import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config):
    """CI sets CE_OFFLINE=1: any test that reaches the network then fails (ADR 0024)."""
    if os.environ.get("CE_OFFLINE") == "1":
        from network_guard import install

        install()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def check_secrets() -> ModuleType:
    """scripts/check_secrets.py is a stdlib-only script, not part of the package."""
    path = REPO_ROOT / "scripts" / "check_secrets.py"
    spec = importlib.util.spec_from_file_location("check_secrets", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
