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
