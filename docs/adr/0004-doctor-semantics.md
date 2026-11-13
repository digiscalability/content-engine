# ADR 0004: What ce doctor checks and when it fails

- Status: accepted, 2026-11-13 (slice P0.1); amended 2026-11-13 (slice P0.2: config check)
- Requirements: SRD §5.2, NFR-5, NFR-8, build prompt gate 0b

## Context

SRD §5.2 says `ce doctor` checks tools on PATH, GPU, disk, and which `.env` keys are set or missing, never printing a value. It does not say which checks are fatal or what the exit code means. The build prompt uses doctor for the gate 0b check, and later Task Scheduler wrappers will want a preflight.

## Decision

- **Fail (exit 1):** a required tool missing or its version probe failing (`git`, `uv`, `ffmpeg`, `ffprobe`, `node`); Node older than 22 (HyperFrames); free disk below `disk.stop_free_gb` (40 GB), where ingest and indexing stop; `config/` failing to load (ADR 0007), with every problem listed under the table. When config cannot load, the disk check uses the NFR-5 values (60 / 40 GB).
- **Warn:** optional tools missing (`yt-dlp` for FR-ING-2; the Postiz CLI is not a PATH tool, it runs pinned through `npx`, ADR 0010); no `nvidia-smi` or a failing GPU probe; no `h264_nvenc` encoder; disk below `disk.warn_free_gb` (60 GB); no `.env` file; any key missing.
- Missing keys never fail doctor: which keys matter depends on the slice, and the gate 0b check reads the per-key lines, each tagged with the §19 item that produces the key.
- Disk is measured on the repo's drive in GiB (what Explorer shows as GB). Output is ASCII only, so a PowerShell 5.1 console can always print it.
- Every probe runs through an injectable `Probes` object; tests use fakes.

## Consequences

- A slice that needs a key checks doctor's line for it rather than the exit code.
- When a later slice makes a tool required, it moves that tool from warn to fail here and updates this ADR.
