# ADR 0014: Showcase cut and the public mirror

- Status: accepted, 2026-11-13 (slice P0.7b)
- Requirements: SRD C11, NFR-7, NFR-9, D1, D9, P0.7b
- Amends: ADR 0013 decision 2 (where the never list lives)

## Context

P0.7b turns the repo into job-hunt proof: a README with the architecture, a short recording of `ce daily --dry-run`, and a tree that could go public as a mirror with fresh history (C11) once Abbas says so. An audit of the tree against the Public Fact Sheet found personal data that must never be public. The sheet's own rule is that its never list stays out of public files, "test files and eval rules" included, yet the list sat in the identity QA config, the playbook and test fixtures. Fixtures also named real employers, and example paths carried a Windows user name.

## Decision

1. **The never list is read from the Fact Sheet at run time,** from the "Never" sentences of its *Who* section: quoted words, and the items of a "Never mention a, b or c." list, with plural items matching their singular. The identity config keeps only extra terms (empty for selfhelp). The playbook points at the sheet instead of listing the items. `get_playbook` hands the terms to the scriptwriter, so it avoids them before voice and avatar credits are spent. This amends ADR 0013 decision 2.
2. **Fixtures are made up.** The text-check tests use an invented sheet; example paths use `C:\Users\me`.
3. **The mirror is built, never published, by code.** `scripts/build_public_mirror.py <out>` exports `HEAD` (committed files only) without the private planning docs (the SRD, the build prompt and PROGRESS, which hold account names, vault paths and personal context). It fails on three kinds of finding:
   - secrets (the pre-commit scanner);
   - personal terms from a denylist kept outside the repo, at `%LOCALAPPDATA%\content-engine\mirror-denylist.txt`, because the list is itself personal; findings name a term's number, never the term;
   - relative links to files the mirror lacks.

   It creates no repository, commit or remote: a fresh one-commit history and the GitHub repo wait for Abbas's OK. The docs tests that read the SRD skip where it is absent, so the mirror's own test suite passes.
4. **The history is scanned for secrets,** not only the tree: `check_secrets.py --history` reads every blob reachable from any ref once and flags any secret-bearing file type ever committed. The private history still holds personal data, which is why the mirror starts fresh.
5. **The recording is a replay, said to be one.** `scripts/make_demo.py` builds a HyperFrames composition from a real dry run's own audit log (every tool call, its status and duration), the files it wrote, and the Short it made.
   - The terminal side replays the calls with their real durations; only the minute-long render is sped up, and it says by how much.
   - A live screen capture was not used: it would show whatever else is on the machine's screen.
   - The output is `docs/media/demo.mp4` and a smaller `demo.gif` for the README. They contain no real footage: the avatar is a placeholder card and the B-roll is drawn by ffmpeg.
6. **The README is written for a public reader.** It links the ADRs, not the private docs, and says the requirement ids point to a private document. The byline uses the Fact Sheet's name and its primary hiring link.

## Consequences

- The README has been rendered locally; GitHub's own preview, the licence and the mirror itself wait for Abbas.
- A Fact Sheet edit can change the parsed never list. `get_playbook` shows the current terms.
- If the planning docs are ever published, they need their own scrub first.
