"""NFR-4 cost control: the monthly ledger per vendor, its caps, and the preflights that run
before every paid batch. ADR 0019.

- Claude: a run's `max_budget_usd` is its command budget (daily 3, weekly 5) or what is
  left of the monthly cap, whichever is smaller; nothing left stops the run before it
  starts. The run's reported cost goes into the ledger once, keyed by run id.
- ElevenLabs: the engine cap (D10) and account floor (C16) live in `produce/voice.py` and H3.
- HeyGen: credits estimated from the voiced audio's length, charged once per script when an
  avatar is requested (either route), refused past the monthly cap.
- Renders: refused while free disk is below `disk.stop_free_gb` (NFR-5).

Every charge carries a `charge_key`, so a retried tool or a resumed run never charges twice.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from engine.config import Config
from engine.produce.voice import engine_characters

GB = 1024**3


class BudgetStop(RuntimeError):
    """A vendor's monthly cap leaves nothing for this batch (NFR-4)."""


def month_total(
    conn: sqlite3.Connection, vendor: str, month: str, column: Literal["units", "cost"] = "units"
) -> float:
    row = conn.execute(
        f"SELECT COALESCE(SUM({column}), 0) FROM budget_ledger WHERE vendor = ? AND month = ?",
        (vendor, month),
    ).fetchone()
    return float(row[0])


# Claude -----------------------------------------------------------------------------------


def claude_run_budget(
    conn: sqlite3.Connection, config: Config, month: str, command: Literal["daily", "weekly"]
) -> float:
    """The run's `max_budget_usd`: never more than what is left of the month."""
    claude = config.engine.budgets.claude
    left = claude.monthly_cap_usd - month_total(conn, "anthropic", month, "cost")
    if left <= 0:
        raise BudgetStop(
            f"Claude: the monthly cap of ${claude.monthly_cap_usd:g} is spent for {month}; "
            "no agent run until next month or a higher cap in engine.yaml (NFR-4)"
        )
    return round(min(getattr(claude.max_budget_usd, command), left), 4)


def record_claude_run(
    conn: sqlite3.Connection,
    run_id: str,
    month: str,
    cost_usd: float,
    *,
    model_usage: dict | None = None,
    turns: int | None = None,
    subtype: str | None = None,
) -> None:
    """The SDK's reported cost for a finished run: on its `agent_runs` row and, once, in the
    ledger."""
    conn.execute(
        "UPDATE agent_runs SET cost_usd = ?, model_usage = ?, turns = ?, subtype = ?, "
        "ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE run_id = ?",
        (cost_usd, json.dumps(model_usage) if model_usage is not None else None, turns,
         subtype, run_id),
    )  # fmt: skip
    conn.execute(
        "INSERT OR IGNORE INTO budget_ledger (vendor, month, units, cost, run_id, charge_key) "
        "VALUES ('anthropic', ?, ?, ?, ?, ?)",
        (month, cost_usd, cost_usd, run_id, f"claude:run:{run_id}"),
    )


# HeyGen -----------------------------------------------------------------------------------


def avatar_credits(seconds: float, credits_per_minute: float) -> float:
    return round(seconds / 60 * credits_per_minute, 3)


def heygen_preflight(conn: sqlite3.Connection, config: Config, month: str, credits: float):
    """None when the request fits this month's HeyGen cap, else the reason."""
    cap = config.engine.budgets.heygen.monthly_credit_cap
    used = month_total(conn, "heygen", month)
    if used + credits > cap:
        return f"HeyGen cap: {used:g} + {credits:g} credits > {cap:g} this month (NFR-4)"
    return None


def record_avatar(
    conn: sqlite3.Connection, month: str, script_id: int, credits: float, run_id: str | None = None
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO budget_ledger (vendor, month, units, run_id, charge_key) "
        "VALUES ('heygen', ?, ?, ?, ?)",
        (month, credits, run_id, f"avatar:script:{script_id}"),
    )


# Disk -------------------------------------------------------------------------------------


def disk_free(path: Path) -> int:
    """Free bytes on the drive holding `path`, or its nearest existing parent."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def disk_preflight(free_bytes: int, stop_free_gb: float):
    """None when a render may write, else the reason (NFR-5)."""
    free_gb = free_bytes / GB
    if free_gb < stop_free_gb:
        return f"disk: {free_gb:.1f} GB free, below the {stop_free_gb:g} GB stop line (NFR-5)"
    return None


# The month at a glance ----------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLine:
    vendor: str
    label: str
    used: float
    cap: float
    unit: str

    @property
    def stopped(self) -> bool:
        return self.used >= self.cap

    @property
    def share(self) -> float:
        return self.used / self.cap if self.cap else 0.0


def budget_lines(conn: sqlite3.Connection, config: Config, month: str) -> list[BudgetLine]:
    budgets = config.engine.budgets
    return [
        BudgetLine("anthropic", "Claude", month_total(conn, "anthropic", month, "cost"),
                   budgets.claude.monthly_cap_usd, "USD"),
        BudgetLine("elevenlabs", "ElevenLabs", float(engine_characters(conn, month)),
                   float(budgets.elevenlabs.monthly_char_cap), "characters"),
        BudgetLine("heygen", "HeyGen", month_total(conn, "heygen", month),
                   budgets.heygen.monthly_credit_cap, "credits"),
    ]  # fmt: skip
