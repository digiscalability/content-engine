# ADR 0015: Style analyser and looks

- Status: accepted, 2026-11-13 (slice P0.8.1); the caption width bound amended by ADR 0018 (at most 740 px, inside every platform's safe zone)
- Requirements: SRD §9.1, §9.2, C10, D7, D8, §7.1, §18.2 P0.8

## Context

P0.8 is a live slice and its Gate 0b items were missing: no reference shorts (G0.8) and no Anthropic key (G0.3). On Abbas's choice it was split. P0.8.1 (this) builds and tests everything that needs neither. P0.8.2 (live) runs it on his references with Claude as the proposer and ends with his approval. The SRD names what to measure and what a look holds. It leaves open how each measure is taken, what a look file looks like, how a look reaches a render, and how proposals are shown and approved.

## Decision

1. **Measurements** (`engine/style/measure.py`) are pure functions over ffmpeg output and numpy arrays:
   - cuts come from ffmpeg's scene score (threshold 0.3), giving shot-length statistics;
   - speech timing (time to first word, words a second) comes from the transcriber;
   - loudness and loudness range come from ebur128, with silence as none;
   - tempo is the fastest strong beat of a smoothed onset envelope (60–180 bpm);
   - the palette is a deterministic 5-swatch k-means over frames sampled at 2 fps, which also give brightness and saturation;
   - text statistics come from the boxes OCR finds on the same frames. Captions are the boxes whose centre is below 40% of the height. Words per caption page are counted by spaces, or estimated at five letters a word when OCR drops them, which PP-OCR does on all-caps over colour.
2. **OCR is RapidOCR** (`rapidocr-onnxruntime` 1.4.4: PP-OCR models through onnxruntime on the CPU), behind a `TextDetector` with a fake. Its models ship inside the wheel, so nothing is downloaded at run time; the newer `rapidocr` 3.x fetches models over the network and was not used.
3. **A profile is the per-field median of the references' measures,** with a palette of their palettes. It is stored in `style_profiles` without file names, and the reference files are deleted only after it is stored (C10); `--keep` keeps them.
4. **A look is a versioned YAML file,** `config/looks/<identity>-v<n>.yaml`, holding:
   - a grade (temperature, tint, contrast, saturation, lift, gamma), which the engine turns into a 33-point 3D LUT itself, so no third-party LUT file is ever loaded;
   - a caption style, limited to fonts HyperFrames bundles, with the bottom offset bounded to the platform safe zone and 2–6 words a page;
   - a title-card style;
   - pacing, as the cut range the director follows;
   - a music feel (energy and tempo range; tracks still come only from the licensed library).

   Saving refuses to overwrite a version (§9.2).
5. **Renders apply the plan's look.** `render_post` resolves `plan.look` in the proposal folder first, then `config/looks`, falling back to the engine default; the render report names the look applied and its file. The dry-run director follows the identity's `active_look`.
6. **Proposals are rendered, not described.** A `LookProposer` turns the profile into up to three looks; P0.8.1 ships `ScriptedProposer` (as measured, warmer and slower, cooler and quicker). Each look renders the same sample post through the dry-run pipeline, every vendor faked, into `media/looks/<identity>/proposal-<time>/` with a side-by-side contact sheet. `ce look approve <identity> <n>` then:
   - saves the chosen look as the identity's next version;
   - rewrites only the `active_look` line of its YAML, keeping the comment;
   - records it in `looks`.
7. **Title cards are styled but not yet rendered.** Rendering them needs a title-block composition and overlay text in the edit plan; that waits for P0.8.2 or later, so plans stay captions-only.

## Consequences

- P0.8.2 adds a Claude proposer behind `LookProposer` and runs `ce refs analyze selfhelp` on Abbas's 5–10 shorts. Those files are deleted after measuring (C10), as the command says before it runs.
- The tempo and word-count estimates are rough by design. The profile is a starting point Claude reads, not a spec.
