# ADR 0005: uv_build backend with the package at the repo root

- Status: accepted, 2026-11-13 (slice P0.1)
- Requirements: SRD §5.7

## Context

SRD §5.7 fixes the layout: the `engine/` package sits at the repo root, beside `config/`, `scripts/` and `tests/`, and `ce` is installed by uv. It does not name a build backend, and uv's default backend expects a `src/` layout.

## Decision

Use uv's own `uv_build` backend with `module-root = ""` and `module-name = "engine"`, pinned to `>=0.10,<0.11` to match the installed uv. The `ce` entry point is `engine.cli:app`. Dev tools (pytest, ruff) live in the `dev` dependency group.

## Consequences

- No extra build tool to install; `uv sync` builds and installs `ce` in editable mode.
- Moving to a newer uv minor means widening the `uv_build` pin.
