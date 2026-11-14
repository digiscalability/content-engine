# ADR 0020: The film drive index

- Status: accepted, 2026-11-13 (slice P1.6)
- Requirements: SRD §8.2, §6.3 (FR-UND-1, 2, 3), §13 (00:00–06:00 when a drive is connected), FR-SRC-1..3, NFR-5, NFR-6

## Context

§8.2 registers film drives by volume serial and label. They are indexed in place and read-only: transcripts (or existing subtitles), scene cuts, keyframe thumbnails and embeddings go into the DB, and no film is copied. Indexing runs overnight under the GPU lock and is resumable. The SRD leaves open:

- how a multi-gigabyte film is identified without reading it whole;
- which subtitles count;
- what "resumable" records;
- where keyframes live;
- how embeddings are stored before sqlite-vec;
- how the overnight window is enforced.

## Decision

1. **Volumes are matched on serial and label together**: the serial as eight hex digits, the label without case. A drive on a new letter is found, and a volume that matches only one of the two is not. `ce index-films --volumes` lists the connected volumes so Abbas can write the source entry.
2. **Sources are mirrored from the config into the `sources` table** on each run. A tier change on a source with assets is refused by the schema and named.
3. **A film is known by a sampled fingerprint**: sha256 of its size and three 4 MiB samples (start, middle, end), in the same `sha256` column.
   - A full hash would read every film from a USB drive each night just to recognise it.
   - The samples tell films apart, and a moved or renamed file is the same asset (its path is updated).
   - Paths are stored relative to the volume, because letters change.
   - The attribution starts as "Title (Year)" parsed from the file name, the base of the §8.2 on-screen credit. The studio is added when format A arrives.
4. **The transcript prefers the film's own subtitles**:
   - first, a sidecar `.srt`/`.vtt` with the same stem (English first);
   - then an embedded text subtitle stream (English first, extracted to a temp file);
   - else Whisper large-v3 on 16 kHz mono audio.
   - The `transcripts.model` names the route (`subtitles:sidecar`, `subtitles:embedded`, `whisper:large-v3`). Subtitle cues are stored as timed spans, not words.
5. **Steps commit one at a time**: probe, transcript, scenes, chunks.
   - `film_index` (migration 0004) records each step's time and the last error, so a stopped run resumes at the next step.
   - A failed film is skipped on later nights until `--retry-failed`.
   - Re-running a finished film adds nothing.
6. **Scenes come from ffmpeg's scene score** on a 320 px proxy (the style analyser's method, ADR 0015, rather than PySceneDetect). Scenes are at least 1 s long.
   - Each scene gets one 320 px JPEG keyframe at its midpoint, under `media/index/films/<asset_id>/` (ignored by git). These are thumbnails, not footage.
7. **Text embeddings are stored as float32 blobs** in `transcript_chunks`, one per ~30 s of transcript, from the configured text model. sqlite-vec tables and CLIP keyframe embeddings stay with P0.6b, which also decides the CLIP model download.
8. **The night is a window, not a start time.**
   - `--until HH:MM` starts no new step after the deadline.
   - A deadline more than 8 h away means the window is closed, so a run Task Scheduler catches up at 07:00 does nothing.
   - Whisper takes the GPU lock per film, not for the whole night, so the 04:00 render can take its turn (NFR-6).
   - Free disk below the stop line ends the run (NFR-5).
9. **Read-only by test.** A test snapshots the fixture drive (every path, size and modification time) before and after a run. The reviewed behaviours were each broken on purpose and shown to fail their tests:
   - the drive left untouched;
   - resuming without duplicates;
   - skipping a broken film;
   - the deadline;
   - the serial-and-label match.
10. **The nightly task is registered as a dry run**: `ContentEngine-IndexFilms` at 00:00 runs `scripts/run_index_films.ps1` (`--until 06:00`). Registering waits for Abbas, as ADR 0016 says.

## Consequences

- Nothing indexes until Abbas plugs in the film drive, runs `ce index-films --volumes`, and adds it to the sources config with its serial and label.
- Format A breakdowns (Phase 2) search `transcript_chunks` by cosine. Moving to sqlite-vec is a copy of these blobs.
- Films stay where they are. A render cuts its excerpt into `media/work/` and deletes it afterwards (§8.2), in Phase 2.
