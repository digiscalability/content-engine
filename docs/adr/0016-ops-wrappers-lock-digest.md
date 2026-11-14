# ADR 0016: Job wrappers, GPU lock, digest and notification

- Status: accepted, 2026-11-13 (slice P1.1)
- Requirements: SRD §13, §2.3, D11, C17, NFR-1, NFR-2, NFR-6, NFR-7, FR-PRD-3, FR-PUB-3

## Context

§13 fixes the schedule, the wrapper names, the GPU lock, the digest's contents and the notification. It leaves open how the lock is built, where digests live in git, how the toast opens the digest after the engine has exited, and what registration does before the live jobs exist.

## Decision

1. **The GPU lock is an OS byte-range lock** on `paths.gpu_lock` (`%LOCALAPPDATA%\content-engine\gpu.lock`): `msvcrt` on Windows, `fcntl` elsewhere.
   - The OS drops it when the holder exits or crashes, so a dead job never leaves the GPU locked.
   - A busy lock is retried every 0.5 s for up to an hour; the holder's job and pid sit in `gpu.lock.owner`, so the error names it.
   - `render_post` holds it around the mix, transcription, captions and encode; dry runs and `ce refs analyze` (Whisper) take it too.
   - Unit tests leave it off (`ToolContext.gpu_lock = None`); the lock's own tests run two real processes.
2. **The digest is rebuilt from the database each run** (`ce digest`), so a rerun the same day neither duplicates nor loses an item; the morning repeat is just another run.
   - Waiting on Abbas: drafts, QA holds with their reasons, avatar request cards (one per brief, naming the voiced WAV and the inbox file), YouTube label ticks with a Studio link, and the day's run-report `needs_abbas`.
   - Avatar requests and holds older than 48 h are failed with a reason and reported. Neither ever published, so nothing live is touched.
   - Generated digests stay out of git (`reports/` is ignored); D11's "stays in the repo" is read as "in the repo folder".
3. **Model-written text is escaped** (security review finding). Titles, QA reasons and run-report items become plain one-line Markdown. A run-report link survives only as https on Postiz, YouTube Studio or HeyGen, and only links the engine builds itself (a render, the avatar inbox) may be `file:///`.
4. **The notification is a windows-toasts toast** with a `file:///` launch action, so a click opens the digest even after the engine has exited. It shows only when something waits.
   - Its text names what waits ("1 draft to approve, 1 avatar request"); the morning task repeats it while items wait.
   - windows-toasts' winrt packages bundle an old C++ runtime (`msvcp140.dll` 14.29). The first copy loaded serves the whole process, and opentimelineio needs 14.40 or newer. So `notify.py` loads the system's copy before winrt and imports winrt only when a toast is made; a fresh-process test pins this.
5. **Wrappers follow ventures-ops** (§2.3): `scripts/JobCommon.psm1`, one transcript per job per day, logs older than 30 days pruned, native exit codes trusted over stderr.
   - `run_daily.ps1` runs `ce daily`, then `ce digest` even when the pass fails, and exits with the first failure's code.
   - `run_digest.ps1` is the morning repeat.
6. **Registration waits.** `register-tasks.ps1` is a dry run unless `-Apply`, and `-Describe` prints the task definitions as JSON. Each task runs `conhost --headless powershell ...` with wake-to-run, start-when-available, no overlapping instances and a 3 h limit.
   - Jobs whose wrapper does not exist yet (ingest, metrics, weekly) are skipped. Nothing is registered until Abbas says yes in chat, and the live `ce daily` arrives in P0.9.
   - The schedule: 04:00 daily, 08:00 digest repeat, 02:00 ingest, metrics every 6 h, Sundays 20:00 weekly.

## Consequences

- Registering makes sense once P0.9's live `ce daily` exists; until then a 04:00 task would run the live command and fail.
- `ce reconcile` joins `run_daily.ps1` when it is built (P0.9 or P1.7).
