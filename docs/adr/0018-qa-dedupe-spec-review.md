# ADR 0018: Self-dedupe, the spec check and the reviewer's checklist

- Status: accepted, 2026-11-13 (slice P1.3)
- Requirements: SRD §6.7 (FR-QA-3, FR-QA-4, FR-QA-7), C12, GR-3, §11 `renders.script_embedding`
- Amends: ADR 0015 (caption width bound), ADR 0013 (checks run once each)

## Context

FR-QA-3 fails a script whose cosine is above 0.90 against the identity's last 50 posts. FR-QA-4 checks resolution, duration, fps, loudness, caption safe zones and caption coverage. FR-QA-7's reviewer returns pass, hold or fail, and GR-3 names its checklist. The SRD leaves open:

- how the embedding model runs;
- which posts count;
- where the safe zones come from and how captions are measured against them;
- what "pass" must show.

## Decision

1. **Embeddings: `BAAI/bge-small-en-v1.5` (C12) on onnxruntime with its own tokenizer**, at pinned revision `5c38ec7c`.
   - No torch and no new packages: numpy, onnxruntime, tokenizers and huggingface-hub were already installed and are now declared.
   - It runs on the CPU from the local Hugging Face cache with no network at run time, using the [CLS] vector, normalised.
   - The model was downloaded on 2026-11-13 with Abbas's OK. On another machine, `ce models fetch` downloads it, and `ce doctor` warns while it is missing.
   - Measured on self-help scripts:
     - distinct topics score 0.58–0.68;
     - a light rewording of the same script scores 0.95;
     - so 0.90 separates them with room on both sides.
   - Unit tests and dry runs use `FakeEmbedder` (hashed words). Real-model tests carry the `model` marker.
2. **Self-dedupe compares against counted posts only.** A post counts once QA held or passed it, or it went further (drafted, uploaded, scheduled, published).
   - Failed and rejected posts never went out, so their ideas stay free.
   - The vector is the voiced script's, stored in `renders.script_embedding` when the check runs. It is compared with the render each counted post carries, one per brief, newest 50.
   - A vector from another model (a different length) is skipped rather than compared.
3. **Safe zones are config, not code.** `qa.safe_zones` holds, per platform, the pixels of a 1080×1920 frame the UI covers on each side.
   - There is no official spec. Third-party guides give ranges: top 120–200, bottom 250–370, right 120–164, left 50–60.
   - The starting values are the upper ends, the same for all three platforms: top 200, bottom 370, left 60, right 164. Abbas can tune each platform after checking it in the app.
4. **The caption box is measured, not assumed.**
   - `render_post` runs ffmpeg `fps=4,alphaextract,bbox` over the caption overlay and stores the union box in `render_report.json`.
   - The spec check fails a box that reaches into any target platform's UI.
   - Captions that should exist but draw nothing also fail.
   - The first measurement found P0.8's 900 px captions reaching x=980, under the button rail. Captions and looks now cap at 740 px (amends ADR 0015's 600–980 bound).
5. **The other spec limits:**
   - resolution and fps exactly as `render:`;
   - duration inside the format and within 0.2 s of the plan;
   - integrated loudness within `qa.loudness_tolerance_lu` (1.0 LU) of −14 LUFS;
   - true peak at most −1 dBTP;
   - captions covering at least 0.95 of the speech when there is any.
   - A mix that measures as silence fails. The one exception is a voice provider that is silent by design (`FakeVoice.silent`), so the dry run still reaches a draft.
6. **The reviewer's checklist travels with every verdict.**
   - `adds` lists what the post adds of its own (GR-3: `voiceover`, `script`, `visuals`, `education`), and `fits_identity` says whether it fits the playbook.
   - A pass needs two additions and a fit. A refused pass stores nothing, so the reviewer can still hold or fail.
   - The checklist is stored with the verdict, and `get_render_report` shows the brief's difference note so the reviewer can compare intent with the render.
7. **Each deterministic check runs once per render on its own** (amends ADR 0013's all-or-nothing rerun). A render checked before this slice gets only the new checks; nothing already stored is rerun or duplicated.

## Consequences

- FR-QA-2 (cross-identity, P2.3) reuses the stored script vectors and adds frame hashes.
- Title cards and lower thirds, once rendered, need the same box measurement as captions.
- The safe-zone numbers are estimates; the first real posts (P0.9, P1.5) are the moment to check them in each app.
