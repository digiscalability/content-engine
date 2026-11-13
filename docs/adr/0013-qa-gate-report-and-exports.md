# ADR 0013: QA gate, render report and hand-polish exports

- Status: accepted, 2026-11-13 (slice P0.7.3); decision 2 amended by ADR 0014 (the never list is read from the Fact Sheet)
- Requirements: SRD §6.7 (FR-QA-1, 5, 6, 7, 8), FR-PRD-7, FR-PUB-3, §7.4, §8.1–§8.3, §10, C7, D9, D12, NFR-9

## Context

P0.7.3 adds the deterministic checks FR-QA-1, 5, 6 and 8, the render report and the FCPXML and OTIO exports, and takes the dry run through QA to a draft. The SRD names what each check enforces, not how text checks decide what a "claim about Abbas" is, where the Fact Sheet and term lists live, how the gate moves posts, or how the exports are built.

## Decision

1. **The Public Fact Sheet is read at run time, never copied.** `engine.yaml` `qa.fact_sheet` holds its vault path (D9). It holds a phone number and an ABN, so it stays out of the repo (NFR-9). Without it, a sentence about Abbas holds; a post that says nothing about him passes.
2. **Text checks are deterministic floors; the qa-reviewer judges meaning (FR-QA-7).** They read the script, on-screen text and captions in `run_qa_checks`, and titles, captions and hashtags in `create_draft` (FR-QA-8 covers them).
   - FR-QA-6: whole-word terms per banned category; a hashtag also matches a term written without spaces.
   - FR-QA-8: any first-person word (I, me, my, we, our, us and their forms) fails, since the avatar is Abbas. So does any term on the Fact Sheet's never list.
   - FR-QA-8 also covers sentences about Abbas. A sentence with a self-reference term (his name or a role word such as chef or engineer) must have its role phrase (the role word and up to two words before it), its proper nouns and its numbers in the Fact Sheet. "A former chef" passes; "a head chef in Sydney" fails.
   - The term lists live in the identity YAML as proposals Abbas confirms, like the playbook. Words that misfire in everyday self-help ("cook", "agency", a bare "partner") are left to the reviewer.
3. **FR-QA-1 runs `rights_violations()` again on the rendered plan,** since a release or licence can change after planning. **FR-QA-5** needs every post of a render with an avatar shot or AI-made picture to be marked AI-disclosed, on a platform with a label route (YouTube private upload, TikTok `video_made_with_ai`, ADR 0002). Instagram fails until a route exists.
4. **The gate's mechanics.** The four checks run once per render and are stored in `qa_results`. A fail moves the posts to `qa_failed` and a hold to `qa_held`. The reviewer judges only a render whose checks all passed, once. QA outcomes are final: posts move strictly forward, never from held or failed to passed. A verdict moves only the posts on its render. `create_draft` re-reads the stored verdicts, not only the post state (security review finding). A new render does not reopen a failed post; the revision loop comes later. FR-QA-2, 3 and 4 arrive with their slices; the report already carries caption coverage for FR-QA-4.
   *Amended 2026-11-13 (security review):* the text checks read the scripts the render's voice came from, as `render_post` records them in the report, not the brief's newest script version. `render_post` refuses a voice asset that is not a voiced script of the same brief, and QA refuses a render with no such record.
5. **Render report (FR-PRD-7).** Each render gets its own folder, `media/renders/<identity>-<brief>-<plan>/`, holding the MP4, `words.json`, `render_report.json` (also stored in `renders.report`) and both exports. The report records:
   - seconds and shares per rights tier, film seconds, share and longest excerpt against the §8.2 limits;
   - people flags by release, and whether AI-made pictures are on screen;
   - ebur128 loudness against the target (silence, the fake voice, reads as none);
   - the probed format and the tags left after stripping;
   - caption words, groups and coverage.
6. **Exports (C7, §7.4).** Both are built from the edit plan in whole frames at the render's rate.
   - OTIO via `opentimelineio` 0.18 is the exact one. V1 holds the shots: speed as `LinearTimeWarp`, a freeze as a `FreezeFrame` clip between its played parts, zoom, crop and annotations as metadata plus a marker. V2 holds captions, V3 overlays, A1 voice, A2 music, A3 SFX.
   - FCPXML 1.8 is written by hand with `xml.etree`. The OTIO FCPXML adapter's own output lacked the frame size and the library and event wrappers, and put audio inside gaps. The shots form the spine; the other layers are connected clips (captions lane 1, overlays 2+, voice −1, music −2, SFX −3 and down). Retimes are not written: a speed or freeze piece plays at 1x for its timeline length, with a marker naming the op.
   - The grade (the look's LUT) is not exported; Resolve applies it as a node.
   - `otio-fcpx-xml-adapter` is a dev dependency only, used to read our FCPXML back in tests.
7. **The dry run goes through the gate:** checks, `get_render_report`, a scripted `pass` review (said in the run report's warnings), scripted copy, and `create_draft` on the fake Postiz.

## Consequences

- Opening an export in DaVinci Resolve is not machine-tested; Abbas's first import (or the P0.7b showcase) confirms it.
- P0.9 runs the real qa-reviewer in place of the scripted verdict.
