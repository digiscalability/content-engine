# ADR 0010: Vendor adapter design for ElevenLabs, HeyGen and Postiz

- Status: accepted, 2026-11-13 (slice P0.6)
- Requirements: FR-PRD-2, FR-PRD-3, FR-PUB-1, FR-PUB-2, D10, C16, NFR-1, NFR-4, NFR-8

## Context

P0.6 builds the three paid or account-bound adapters before any account exists, so they must be testable on fakes, cheap to resume after a crash, and safe with keys. The SRD fixes what they do; how is open.

## Decision

**Shared**

- Each vendor sits behind a protocol (`VoiceProvider`, `AvatarProvider`, `Publisher`) with a fake (`FakeVoice`, `DropFolderAvatar` doubles as the no-API route, `FakePostiz`). Contract tests in `tests/contract/` run the same assertions on the real adapter (on `httpx.MockTransport` or a fake CLI runner that prints what the CLI prints) and on the fake. Live tests arrive with the live slices (P0.6b, P0.9) under the `live` marker.
- Keys come from `engine.settings.Settings` into one HTTP client header or the Postiz subprocess env, never `os.environ`; error messages carry the vendor's status and text, never a key.

**Voice (ElevenLabs)**

- `POST /v1/text-to-speech/{voice_id}?output_format=wav_48000`, `eleven_multilingual_v2`, up to three pronunciation dictionaries. Output is checked to be WAV and written atomically.
- `synthesize_batch()` preflights the whole batch once, before any call: the engine's characters this calendar month from `budget_ledger` plus the batch must stay within 78,600 (D10), and the live balance (`character_limit − character_count`) minus the batch must stay at or above 20,000 (C16). The ledger uses calendar months while ElevenLabs resets around the 2nd; the live-balance floor covers the gap.
- ElevenLabs documents no charge header, so the ledger records the characters sent (one credit per character on multilingual v2). Each script's row is keyed `voice:script:<id>`; a WAV already on disk is never voiced again, and a missing row for it is backfilled without a call (NFR-1).

**Avatar (HeyGen and drop folder)**

- HeyGen's v2 reference pages are retired; the adapter uses v3: `POST /v3/assets` (multipart audio) → `POST /v3/videos` (`type: avatar`, `avatar_id`, `audio_asset_id`, `aspect_ratio: "9:16"`, `resolution: "1080p"`) → `GET /v3/videos/{id}` until `completed`, then downloads the presigned `video_url` with no API key on that request.
- Both routes write a job file `media/inbox/avatar/<brief_id>.job.json`; a second `request()` resumes instead of resubmitting, and a request older than 48 h is `expired` (FR-PRD-3). The drop-folder route's waiting status carries the request card the digest shows Abbas.

**Publisher (Postiz)**

- The CLI runs pinned (`npx --yes postiz@2.0.27`, `npm_config_prefer_offline=true`), like HyperFrames, so nothing is installed globally; `ce doctor` no longer looks for a `postiz` binary (ADR 0004).
- Posts go to `posts:create --json <file>`: on Windows `postiz` is a `.cmd` shim and JSON with quotes or `&` would not survive the batch parser. Uploads pass forward-slash paths because the CLI names the file by the text after the last `/`. Drafts set `shortLink: false` so links keep their UTM tags.
- Output parsing skips the CLI's emoji status line and reads the JSON after it.

## Consequences

- P0.7 wires these into the agent tools (`synthesize_voice`, `request_avatar`, `create_draft`) with the fakes for `--dry-run`, and H3 reuses `preflight()`.
- HeyGen credit checks (`budget_status`, H3) need a quota endpoint; P1.4 adds it with the rest of the budget ledger.
