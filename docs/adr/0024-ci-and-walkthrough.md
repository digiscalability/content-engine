# ADR 0024: CI on the pure modules and the engine-made walkthrough

- Status: accepted, 2026-11-14 (slice P1.8)
- Requirements: SRD §18.3 P1.8, §1.2 (job-hunt proof), NFR-7, NFR-8, NFR-9, C11, D9
- Amends: ADR 0014 decision 5 (the README demo)

## Context

P1.8 is the job-hunt pack: a README with diagrams, the ADRs, GitHub Actions CI on the pure modules, a demo video made by the engine, and a write-up draft. §1.2 measures it as "CI green, tests on every pure module, a 2–3 minute demo video produced by the engine itself". The SRD leaves open which runner CI uses, what counts as pure, how CI is kept off the network, what the video shows and how it is made, and where the write-up lives.

Measured on 2026-11-14 in a fresh clone with ffmpeg and Node off PATH: 789 of the 854 default tests passed, and 65 failed because they draw, probe or index clips with ffmpeg. None needed Node, a model, a key or the network.

## Decision

1. **One job on `windows-latest`.** The engine is Windows-only (junctions and 8.3 names in guard H2, volume serials, the PowerShell wrappers, the toast runtime), so its tests run where it runs. The job installs with `uv sync --locked` on Python 3.11, then runs `ruff check`, `ruff format --check` and `pytest -m "not golden and not gpu and not live and not model and not media"`.
2. **Pure means no keys, GPU, models, ffmpeg or network.** A new `media` marker holds the 65 tests that need ffmpeg. It is on by default, so `uv run pytest` on the build machine still runs them, and CI leaves it out. Where a module also holds pure tests (the film index and stock fetch), the marker sits on each ffmpeg test, so CI keeps the 28 pure ones there. A test checks that the CI expression excludes every registered marker, so a new heavy marker cannot slip into CI unnoticed.
3. **A network tripwire.** With `CE_OFFLINE=1`, `tests/network_guard.py` makes name lookups, connects and UDP sends to anything but loopback raise, so "no network" is checked rather than assumed. That includes asyncio's connects, which skip the lookup for a literal address and on Windows bypass `socket.connect`. It does not cover child processes. `HF_HUB_OFFLINE=1` keeps the Hugging Face client off the network too.
4. **Supply chain.** Both actions are pinned to commit SHAs (`actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.3.3), with the tag in a comment. The workflow has read-only `contents` permission, keeps no checkout credentials and uses no secrets. Superseded runs on the same ref are cancelled.
5. **The mirror runs the same file.** The public tree has no SRD, build prompt or PROGRESS. The doc tests that read them skip, so the workflow needs no change there.
6. **Proved locally before any push.** The CI commands ran in a fresh shallow clone and in the mirror tree. Each had ffmpeg and Node off PATH, an empty Hugging Face cache and the tripwire on, and the Fact Sheet path pointed at nothing, as on a runner. Seeing the run on GitHub needs a push, which waits for Abbas.
7. **The walkthrough is made by the engine, from one dry run.** `scripts/make_demo.py` reads a dry run's database and files and lays them out as ten timed chapters: the database rows are the brief, script, edit plan, assets, QA results, post, metrics snapshot and audit log, and the files are the render report and the Short. The chapters are intro, replay, brief, script, edit plan, the Short in full, QA gate, after the draft, guards, and testing.
   - Every frame is rendered by the engine's pinned HyperFrames and ffmpeg. There is no screen capture and no hand editing, and every number on screen is read from the run.
   - Model-written text (briefs, scripts) is escaped before it enters the HTML.
   - Each card stays up until its longest line can be read, at about 0.5 s plus 0.3 s a word.
   - The result is `docs/media/walkthrough.mp4`, 2:44 at 1280×720 (1.7 MB). The README links it and embeds `demo.gif`, the replay chapter alone. §1.2 asks for 2–3 minutes, and a GIF that long would be far too heavy for a README, so it gets both.
   - The replay chapter keeps ADR 0014's rules: real timings, and only the render sped up, labelled as such. The walkthrough replaces P0.7b's `demo.mp4`, which amends ADR 0014 decision 5.
8. **The walkthrough is silent.** The dry run's voice is a silent fake and the repo holds no licensed music. HyperFrames' craft check flags the silence; it is accepted.
9. **The write-up lives outside the repo,** in the vault's Career area (Abbas, 2026-11-14), because it is about Abbas. Its claims about him come only from the Public Fact Sheet, read when it is written.

## Consequences

- CI is not green until Abbas approves a push to the private backup or the mirror. The badge in the README points at the mirror's workflow.
- A new test that shells out to ffmpeg needs the `media` marker. A runner without ffmpeg turns CI red if the marker is missing. A runner that has ffmpeg would hide it, so the local proof with ffmpeg off PATH is the real check.
- The walkthrough goes stale when the pipeline changes. Run `ce daily --dry-run`, then `make_demo.py`.
