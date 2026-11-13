# ADR 0003: Secret scan as a stdlib Python script behind a core.hooksPath hook

- Status: accepted, 2026-11-13 (slice P0.1)
- Requirements: NFR-7, C6, SRD §2.3, §3.4

## Context

The SRD requires a pre-commit secret scan and says to copy the ventures-ops convention (`scripts/check-secrets.mjs` behind `.githooks/pre-commit`, installed with `core.hooksPath`). It does not say which language or hook manager this repo uses.

## Decision

- Port the scanner to `scripts/check_secrets.py`, stdlib only, so it runs with any Python 3 and needs neither the venv nor Node.
- Keep the ventures-ops hook shape: `.githooks/pre-commit`, installed per clone with `git config core.hooksPath .githooks`. No `pre-commit` framework: it would download hook environments from the network and add a dependency for one script.
- The hook tries `python`, `python3` and `py` and refuses the commit when none exists (fails closed).
- Patterns: the ventures-ops set, plus an ElevenLabs key (`sk_` + 48 hex) and a dotenv-style `NAME=value` rule, so a value pasted into `.env.example` is caught. Secret-bearing file types (`.env*` other than `.env.example`, `SECRETS.md`, `*.pem`, `*.key`, service-account and credentials JSON) are blocked whatever they contain.
- Findings name file, line and pattern, never the value, and are written as UTF-8 so non-ASCII file names survive a Windows pipe.

## Consequences

- Each fresh clone needs the one `git config` line (in README and CLAUDE.md). P1.8's CI can run the same script over the whole tree.
- Test fixtures must build fake keys at runtime; a real false positive is waved through with `check-secrets: allow` on that line.
