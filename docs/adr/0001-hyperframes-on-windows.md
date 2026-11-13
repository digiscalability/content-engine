# ADR 0001: HyperFrames renders overlays on Windows; Remotion not needed

- Status: accepted, 2026-11-13 (slice P0.3)
- Requirements: SRD §7.2, C9, FR-PRD-5, NFR-8, NFR-9

## Context

C9 opens Phase 0 with a spike: prove HyperFrames renders on this Windows PC, or put Remotion in its slot. §7.2 also asks the builder to confirm that local renders make no metered calls, because HyperFrames is a HeyGen project with paid cloud features.

## Outcome

HyperFrames works. `hyperframes@0.9.40` rendered `hyperframes/captions-spike/` (1080×1920, 5 s, three caption groups with a per-word highlight, transparent root) on Windows 11 with Node 24.14 and FFmpeg 8.0.1. ffmpeg composited it over a generated test clip with NVENC, and the MP4 passed the ffprobe check (`tests/golden/test_overlay_spike.py`, 4 passed).

| Measure | Result |
|---|---|
| First render (downloads its Chrome) | 33.2 s |
| Warm render, 5 s overlay | 12.9–13.0 s, `screenshot capture · hardware gpu`, 5 Chrome workers |
| Whole golden test (clip, overlay, composite, probes) | 31 s |
| Overlay file | ProRes 4444, `yuva444p12le`, 13.7 MB for 5 s |
| Caption band diff vs base clip | 48.2 (test needs > 20) |
| Clear band diff vs base clip | 0.08 (test needs < 4; an opaque overlay measures 127.9) |

## Decision

1. **HyperFrames fills the `OverlayRenderer` slot** (`engine/produce/overlay.py`). Remotion is not built; C9's fallback stays on paper.
2. **Overlays are ProRes 4444 MOV** (`--format mov`). HyperFrames' WebM is VP9 with an `ALPHA_MODE` side channel that ffmpeg's native VP9 decoder drops (every pixel decodes opaque); only a forced `-c:v libvpx-vp9` keeps it. ProRes 4444 carries alpha in the main stream, so `overlay=format=auto` works with no decoder flags. The cost is size (13.7 MB vs 0.25 MB for 5 s), which is fine for files that live in `media/work/` only until the render ends.
3. **One pinned version, `0.9.40`**, in `HYPERFRAMES_VERSION`; upgrading is a deliberate change that re-runs `-m golden`.
4. **Every call sets** `HYPERFRAMES_NO_TELEMETRY=1`, `HYPERFRAMES_NO_UPDATE_CHECK=1`, `HYPERFRAMES_SKIP_SKILLS=1` (the CLI would otherwise install skills into `~/.claude/skills`, which only Abbas changes, by hand) and `npm_config_prefer_offline=true`. The engine never runs `auth`, `cloud`, `lambda`, `cloudrun`, `publish`, `tts`, `usage` or `feedback`.
5. **GSAP 3.15.0 is vendored** into each composition's `assets/` (GreenSock standard no-charge licence, header kept) instead of loading from jsDelivr, so a render needs no CDN and cannot pick up a changed script.
6. **Composite settings** (`engine/produce/ffmpeg.py`): `overlay=0:0:format=auto,format=yuv420p`, `h264_nvenc`, 30 fps, AAC 48 kHz, `-map_metadata -1 -map_chapters -1 -metadata:s:v encoder= -fflags +bitexact`. `-map_metadata -1` alone leaves the muxer's `encoder` tags, so the last two flags are needed for FR-PRD-5's "metadata stripped".

## No metered calls: evidence

- Not signed in to HeyGen (`hyperframes auth status`: "Not signed in"), and renders succeed, so nothing can be billed to an account.
- Each render's own process tree was sampled every 150 ms for remote TCP connections (`Get-NetTCPConnection` filtered to descendants of the `npx` process):
  - First render: `registry.npmjs.org` (npx resolving the package), `fonts.googleapis.com` and `fonts.gstatic.com` (Inter, cached in `~/.cache/hyperframes/fonts`), and `storage.googleapis.com`, where HyperFrames fetched its pinned `chrome-headless-shell` 152.0.7977.30 (270 MB, Google's Chrome for Testing, cached in `~/.cache/hyperframes/chrome`).
  - Warm render: only `registry.npmjs.org`.
  - Warm render with `npm_config_prefer_offline=true`: **no remote connections at all**.
- No connection reached HeyGen's API (`api.heygen.com`, `api2.heygen.com`) or PostHog telemetry (`us.i.posthog.com`), whose addresses were resolved and compared.

## Consequences

- A new machine's first render needs the network once (CLI, Chrome, fonts); after that, rendering is offline.
- `hyperframes check` warns `nested_structure_needs_subcomposition` on the spike. P0.5, which generates real caption compositions from Whisper word timings, puts the caption groups in one captions sub-composition (`data-track-kind="captions"`) as the HyperFrames skill recommends, so Abbas can edit them in the HyperFrames desktop app.
- The golden test needs `npx`, which comes with Node (already a required tool in `ce doctor`).
