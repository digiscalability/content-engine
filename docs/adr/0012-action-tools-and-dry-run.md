# ADR 0012: Action tools and the dry run

- Status: accepted, 2026-11-13 (slice P0.7.2)
- Requirements: SRD §5.2, §5.4, §5.5, §8.3, §10, FR-PRD-2, FR-PRD-3, FR-PRD-5, FR-PRD-6, FR-PUB-1–4, FR-QA-5, GR-4, NFR-1, NFR-8, NFR-9

## Context

P0.7.2 adds the four action tools and `ce daily --dry-run`. The SRD names what each tool does, not how generated media enter an edit plan, where post rows come from, what "dry run" means for Claude itself, or in which order the voice, avatar and edit plan happen. The P0.7 sub-slices were first named P0.7a/b/c, which collided with the SRD's own P0.7b (the showcase cut).

## Decision

1. **Sub-slices are P0.7.1–P0.7.3** in PROGRESS; P0.7.1 is the slice commits and ADR 0011 call "P0.7a". The SRD's P0.7b keeps its name.
2. **The edit plan comes after the voice and avatar.** It names assets by id, and the presenter's lips must follow the voice, so the flow is brief → script → voice → avatar → edit plan → render → QA → draft (orchestrator and scriptwriter prompts updated). Avatar shots use source time equal to timeline time.
3. **Generated media are assets of a built-in `engine` source** (migration 0002; tier `ai_generated`; people `none`, since the only face is Abbas's avatar, §8.3). The id `engine` is reserved in `sources.yaml`, so FR-SRC-3 still holds for footage. The same bytes map to the same asset.
4. **Post rows start with the brief:** one `planned` row per channel; each tool moves them forward along §10 (`advance_posts`, never backwards or out of an ended state). Format C posts are always AI-disclosed (FR-QA-5). An expired or failed avatar request fails only posts not yet rendered (security review finding).
5. **Tools, as built.** `synthesize_voice` uses the P0.6 batch preflight and ledger, so a script is charged once (NFR-1); identities carry `voice_id` and `avatar_id`, empty until G0.4/G0.5, and the tools refuse without them. The pronunciation dictionary waits for G0.4. `request_avatar` answers `ready` (the video becomes an asset) or `waiting` (no asset; the post waits for Abbas). `render_post` mixes, transcribes the mix, snaps word starts to the voice onsets, renders captions with HyperFrames, encodes, and keeps `words.json` beside the work files for P0.7.3; plans with overlay blocks are refused until a look exists (P0.8). The GPU lock waits for the first concurrent job (P1, Task Scheduler). `create_draft` checks every channel before drafting any: posts must be `qa_passed` for that render; YouTube goes up private (ADR 0002), TikTok with `video_made_with_ai`, Instagram is refused; draft dates sit `min_gap_hours` after now and after the channel's last post date (FR-PUB-4), and Abbas picks the real time.
6. **One guard table.** `guard_table()` lists H1–H4; `build_hooks()` registers it with the SDK and `GuardedTools` applies it, with the H5 audit, to calls made by code. A guard that raises blocks, as `on_failure="block"` does.
7. **The dry run fakes every vendor, Claude included.** A scripted director plays the orchestrator and scriptwriter with one fixed, rule-abiding post and calls the tools through `GuardedTools`. Each run gets its own folder `media/dry-run/<run_id>/` with its own DB, a cutaway library drawn by ffmpeg (NFR-9), fake vendor ids, `FakeVoice` (silent WAV), `FakeAvatar` (a card video of the voice's length), `FakePostiz` and `ScriptAlignedTranscriber` (the script's words spread over the mix, since silence has nothing to hear). ffmpeg and HyperFrames run for real: they are local, and the MP4 is the point. The run stops at the render until QA exists (P0.7.3) and says so in the report.
8. **`ce daily` takes `--dry-run` and `--identity` after the command too** (the SRD's `ce daily --dry-run`), as well as before it. Without `--dry-run` it exits 2 and points at P0.9.

## Consequences

- P0.7.3 adds `run_qa_checks`, `get_render_report` and the render report and exports; the dry run then continues through QA to a fake draft.
- The real `daily` (P0.9) builds the same `ToolContext` with live providers and runs the SDK agent instead of the director.
