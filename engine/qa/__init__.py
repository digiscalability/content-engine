"""QA gate (SRD §6.7): deterministic checks before the qa-reviewer.

Each check returns a `CheckResult`; a hard fail stops the post, a hold sends it to Abbas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

Result = Literal["pass", "hold", "fail"]


@dataclass(frozen=True)
class CheckResult:
    name: str
    result: Result
    reasons: list[str] = field(default_factory=list)


def verdict(name: str, fails: Sequence[str], holds: Sequence[str] = ()) -> CheckResult:
    """Fail if anything failed, else hold if anything held, else pass."""
    if fails:
        return CheckResult(name, "fail", [*fails, *holds])
    if holds:
        return CheckResult(name, "hold", list(holds))
    return CheckResult(name, "pass")
