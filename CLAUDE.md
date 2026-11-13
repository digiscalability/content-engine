# content-engine — guidance for Claude

A local Python engine that plans, scripts, voices, edits and drafts social video posts for niche identities, then learns from the results. Built one slice per session.

## Where context lives

- `docs/SRD.md`: every requirement by id. §0 decisions are settled, §18 the slices and their done-when, §19 the steps only Abbas can do. The SRD wins any disagreement.
- `docs/BUILD_PROMPT.md`: the per-session procedure (orient, gate check, plan, red-green, verify, record, report).
- `docs/PROGRESS.md`: the only record of progress. `docs/adr/`: decisions the SRD left open. `docs/superpowers/plans/`: one plan per slice.

## Run and verify

- `uv sync`, then once per clone: `git config core.hooksPath .githooks` (pre-commit secret scan, NFR-7).
- `uv run pytest` (markers `golden`, `gpu`, `live` are off by default; `-m golden` runs them), `uv run ruff check .`, `uv run ruff format --check .`.
- `uv run pytest -m gpu -s`: Whisper on the RTX 5070 (needs the cached large-v3 model and the `nvidia-cublas-cu12` wheel, ADR 0009). After changing the transcriber, re-record `tests/fixtures/speech/discipline.whisper.json` with `uv run python scripts/make_whisper_fixture.py`.
- Golden frame hashes live in `tests/golden/expected/`. After a deliberate render change, look at the frames first, then `UPDATE_GOLDEN=1 uv run pytest -m golden`.
- `uv run ce doctor`: tools, GPU, disk, config validity, and which `.env` keys are set or missing. It is the only way to learn whether a key exists, and the quickest check after editing anything under `config/`.
- `uv run ce daily --dry-run`: one sample post per due identity, brief to MP4 to QA to a fake Postiz draft, on fakes (Claude's part scripted), in about a minute. Each render folder holds the MP4, `render_report.json`, `.fcpxml` and `.otio`. Each run writes about 130 MB to `media/dry-run/<run_id>/` (its own DB too); old run folders can be deleted by hand.
- Schema changes are a new numbered file in `engine/db/migrations/`; an applied migration is never edited (ADR 0006).
- Secret scan by hand: `uv run python scripts/check_secrets.py <files>`; the whole history: `--history`.
- Public mirror (C11, ADR 0014): `uv run python scripts/build_public_mirror.py <new folder>` builds and scans the public tree locally (secrets, the off-repo personal denylist, broken links). It never creates a repo or remote; publishing waits for Abbas.
- README demo: `uv run python scripts/make_demo.py` replays the newest dry run into `docs/media/demo.mp4` and `demo.gif`.

## Traps

- Piping a test run through `tail` hides its exit code; use `set -o pipefail` before chaining a commit onto it.
- The secret guard denies any Bash or PowerShell call whose text names a `.env` file other than `.env.example`. Tests build such paths at runtime; write fixture files with the Write tool.
- Test fixtures build fake keys at runtime (`"sk-ant-" + "x" * 40`) so the source passes the scan. ALL_CAPS values read as env-var names and are not flagged.
- Postiz runs only through `engine/publish/postiz.py` (pinned `postiz@2.0.27` via npx, posts as `--json` files, ADR 0010). YouTube avatar posts go up private and Abbas ticks the AI label in Studio (ADR 0002).
- HyperFrames runs only through `engine/produce/overlay.py` (pinned version, telemetry and skill installs off, ADR 0001). By hand, set `HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_NO_UPDATE_CHECK=1 HYPERFRAMES_SKIP_SKILLS=1` and use `npx --yes hyperframes@0.9.40`; never its `auth`, `cloud`, `publish`, `tts` or `feedback` commands. Compositions load GSAP from their own `assets/`, not a CDN.

## Rules (SRD §3.4, §15)

- Secrets live only in `.env`, which Abbas fills from `.env.example`. Vendor keys load into `engine.settings.Settings`; only `ANTHROPIC_API_KEY` ever reaches the agent subprocess (FR-AGT-1).
- Every external call (Anthropic, ElevenLabs, HeyGen, Postiz, yt-dlp, Pexels) sits behind an interface with a fake (NFR-8); live calls carry the `live` marker.
- Footage enters only through `config/sources.yaml` with a rights tier; Abbas is the only one who adds sources (FR-SRC-3).
- Agents act only through the `mcp__engine__*` tools and hooks of SRD §5.4–§5.6: no shell, web or delete tools (GR-9). New tools are `ToolSpec`s registered in `engine/agents/tools.py`, action handlers in `actions.py` (plain Python, tested without Claude); the SDK appears only in `engine/agents/options.py` and `hooks.py` (ADR 0011). Guards live once in `hooks.guard_table()`; code that calls tools goes through `GuardedTools` (ADR 0012).
- Posting is platform-native through Postiz, drafts only at L0 (GR-4, §10).
- Commit on `feat/<slice-id>-<slug>` with Conventional Commits, staging files by name; fast-forward `main` locally. Pushing, GitHub repos, Task Scheduler jobs, accounts and purchases wait for Abbas's yes in chat.

## Never

- Commit `.env*` (other than `.env.example`), keys or real footage; NFR-9 keeps the repo publishable.
- Copy the Public Fact Sheet, its never list, employers or other personal facts into the repo, tests included: the engine reads the sheet at run time (ADR 0014).
- Build engagement automation, account farms, proxies or antidetect tooling (GR-4).
- Use film or TV outside format A breakdowns under §8.2 (GR-8).
