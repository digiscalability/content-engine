# Architecture decision records

One file per decision the SRD left open (build prompt step 6). Format: context, decision, consequences. A superseded ADR stays and gains a "Superseded by" line.

| ADR | Decision | Slice | Status |
|---|---|---|---|
| [0001](0001-hyperframes-on-windows.md) | HyperFrames renders overlays on Windows (ProRes 4444 alpha, pinned 0.9.40, no metered calls); Remotion not needed (C9) | P0.3 | Accepted |
| [0002](0002-postiz-disclosure-and-status-routes.md) | AI disclosure and status per channel: YouTube private route, TikTok `video_made_with_ai`, Instagram none yet; status from `posts:list` | P0.6 | Accepted |
| [0003](0003-secret-scan-hook.md) | Secret scan as a stdlib Python script behind a `core.hooksPath` hook | P0.1 | Accepted |
| [0004](0004-doctor-semantics.md) | What `ce doctor` checks and when it fails | P0.1 | Accepted |
| [0005](0005-uv-build-flat-layout.md) | `uv_build` backend with the package at the repo root | P0.1 | Accepted |
| [0006](0006-schema-choices.md) | Schema choices SRD §11 left open: migrations, ids, rights constraints, deferred sqlite-vec | P0.2 | Accepted |
| [0007](0007-config-validation-rules.md) | Config validation rules beyond FR-SRC-1 | P0.2 | Accepted |
| [0008](0008-edit-plan-and-ffmpeg-renderer.md) | Edit plan semantics; ffmpeg renderer with graph files, sidechain ducking to an exact depth, two-pass loudnorm | P0.4 | Accepted |
| [0009](0009-whisper-on-blackwell-and-captions.md) | Whisper large-v3 on the RTX 5070 with pip cuBLAS (no fallback); word starts snapped to VO speech onsets; captions composition | P0.5 | Accepted |
| [0010](0010-vendor-adapter-design.md) | Vendor adapters: protocols with fakes and contract tests, voice preflight and ledger, HeyGen v3 and drop folder, Postiz CLI through npx with `--json` | P0.6 | Accepted |
| [0011](0011-agent-layer-skeleton.md) | Agent layer: tools as plain Python, one rights check, guards that fail closed, H2 on real paths, FR-AGT-1 against the SDK's env merge; P0.7 split | P0.7a | Accepted |
| [0012](0012-action-tools-and-dry-run.md) | Action tools and the dry run: edit plan after voice and avatar, generated media as `engine` assets, post rows from the brief, one guard table, a scripted director on fakes; P0.7 sub-slices renamed P0.7.1–3 | P0.7.2 | Accepted |
| [0013](0013-qa-gate-report-and-exports.md) | QA gate: text checks as floors against the Fact Sheet read at run time, final and render-scoped verdicts; render report per render folder; OTIO exact, FCPXML 1.8 by hand | P0.7.3 | Accepted |
| [0014](0014-showcase-and-public-mirror.md) | Showcase cut: never list read from the Fact Sheet (amends 0013), made-up fixtures, a local public-mirror builder with an off-repo denylist, history secret scan, the demo as a labelled replay | P0.7b | Accepted |
| [0015](0015-style-analyser-and-looks.md) | Style analyser (scene cuts, OCR by bundled-model RapidOCR, tempo, palette) and looks as versioned YAML with a generated LUT, applied by every render; proposals rendered as samples; P0.8 split | P0.8.1 | Accepted |
| [0016](0016-ops-wrappers-lock-digest.md) | Job wrappers (ventures-ops log convention), an OS-level GPU file lock, a digest rebuilt from the database with escaped model text, toasts that open it, registration as a dry run until Abbas says yes | P1.1 | Accepted |
| [0017](0017-topic-planning.md) | Topic planning: the bank in the playbook's Rules, exploration counted per week and rounded half up, briefs matched to topics by text, candidates by recency and pillar, banned topics checked before money is spent | P1.2 | Accepted |
| [0018](0018-qa-dedupe-spec-review.md) | Self-dedupe on bge-small (onnxruntime, pinned, cached) against counted posts; spec check with safe zones from config and the caption box measured from the overlay (captions cap at 740 px); the reviewer's GR-3 checklist; checks run once each | P1.3 | Accepted |
| [0019](0019-budgets-and-kill-switch.md) | One budget module: Claude run budget capped by the month, HeyGen credits estimated from the voiced audio, a disk preflight for renders, hard stops as digest alerts; the kill switch in the database with identity_events, Postiz left to Abbas | P1.4 | Accepted |
| [0020](0020-film-drive-index.md) | Film drive index: volumes by serial and label, a sampled fingerprint, subtitles before Whisper, one committed step at a time with a status row, ffmpeg scenes and 320 px keyframes, text embeddings as blobs until sqlite-vec, the night as a window | P1.6 | Accepted |
| [0021](0021-metrics-and-reconciliation.md) | Metrics and reconciliation: `uploaded_private → published` by YouTube's key-free oEmbed, one snapshot per run at the latest due slot, missing release ids connected only when the counts agree, no rejection on a partial Postiz view, `--dry-run` that calls and writes nothing | P1.7 | Accepted |
| [0022](0022-own-footage-library.md) | Own-footage library: CLIP ViT-B/32 as a pinned ONNX export on onnxruntime, YuNet faces on a frame every second and each keyframe with every face sent to review, marks by CLI with an audit table, in-place ingest with a path cache, shared understanding steps, sqlite-vec tables keyed to chunks and scenes, search that filters before it ranks | P0.6b.1 | Accepted |
| [0023](0023-stock-and-music.md) | Stock fetched nightly only for the scriptwriter's weak beat queries (queued by `search_library` with a brief id, CLIP under 0.23), capped per day, full HD portrait only; one licence record per library asset, required by search and the rights check; the music pool imported in place with tempo, energy and source terms, found by `search_library` kind music | P0.6b.2 | Accepted |
| [0024](0024-ci-and-walkthrough.md) | CI on the pure modules: one windows-latest job, a `media` marker for the ffmpeg tests, a network tripwire, actions pinned by SHA, the same file in the mirror; a 2–3 minute walkthrough rendered by the engine's own HyperFrames from one dry run's data (amends 0014's demo) | P1.8 | Accepted |
