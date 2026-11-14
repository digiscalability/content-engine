# ADR 0022: The own-footage library

- Status: accepted, 2026-11-14 (slice P0.6b.1)
- Requirements: SRD §6.2 (FR-ING-1, FR-ING-4), §6.3 (FR-UND-2, 3, 5), FR-SRC-2, FR-SRC-3, §5.4 `search_library`, §8.1, §8.3, §11, §13 (02:00 ingest), C12, C13, NFR-5, NFR-8, NFR-9

## Context

P0.6b.1 builds the starter library on generated fixtures: own-footage ingest, scenes and keyframes, text and CLIP embeddings in sqlite-vec, the people-rule review list, and `search_library`. The SRD leaves open:

- how CLIP runs without PyTorch, which no earlier slice needed;
- which face detector, on which frames, and what happens without a reference set of Abbas's face;
- how Abbas marks a clip;
- how an unchanged file avoids being hashed every night;
- what the vector tables are keyed to, and how search filters and ranks;
- whether stills count.

## Decision

1. **CLIP is the ONNX export of OpenAI's ViT-B/32** (`Xenova/clip-vit-base-patch32`, pinned revision `6b7c523`), run on onnxruntime on the CPU.
   - These are the weights C12 names (open_clip's `ViT-B-32` with OpenAI's weights), without the PyTorch install open_clip needs.
   - Preprocessing follows the model: the shortest side goes to 224 (bicubic), then a centre crop, scaling to 0–1 and CLIP's mean and std. Text is cut at 77 tokens.
   - The download is about 608 MB: the text model, the vision model and the tokenizer. Abbas approved it in the session.
   - Every pinned model (bge-small, CLIP, YuNet) is a `PinnedModel` in `engine/library/models.py`. `ce models fetch [names]` downloads them, and `ce doctor` checks each one.
2. **Faces are found by OpenCV's YuNet** (`opencv/face_detection_yunet` 2023mar, 0.2 MB, pinned). OpenCV 5 no longer ships the Haar cascades.
   - The check looks at a 640 px frame every second from the start, plus every scene keyframe.
   - A check that examined no frame fails the step, so the clip stays held back rather than reading as clean.
   - Leftover frames from a killed run are cleared first.
   - No face: the asset's `people_release` becomes `none`. Any face: it stays `unknown`, and `asset_index` records how many frames had a face and when the first one appeared.
   - **No face is matched to Abbas yet.** FR-UND-5 matches against "Abbas's reference set", which needs a face-recognition model and photos of him that must stay out of the repo. Until then every face goes to review, Abbas's own included, and he marks his clips `yes`. This errs toward holding clips back, never toward letting one through.
   - Faces are the SRD's test. A person seen from behind or far away is not caught, and that risk is accepted with the SRD's wording.
3. **Marks go through the CLI with an audit table.**
   - `ce status --people` lists clips that were checked, showed a face and have no mark, each with a keyframe to look at.
   - `ce people mark <id> yes|exclude [--note]` adds a `people_marks` row and sets the asset's state, in one transaction.
   - A later people check may change only an unmarked `unknown` asset, so a mark is never undone. A test proves it, and failed with the guard removed.
   - Plain `ce status` prints only the count for now; the rest of NFR-3's screen comes later.
4. **Ingest works in place, on video only.**
   - Every folder source that is not a film drive is scanned recursively. Nothing is written beside the footage (a test snapshots the folder), and nothing is copied, so FR-ING-3's purge has nothing to do yet.
   - Files are hashed whole (SHA-256), and the same bytes under two paths are one asset. `asset_paths` remembers each path's size and mtime, so an unchanged file is not read again.
   - Assets carry their source's tier, licence URL, attribution and expiry (FR-SRC-2).
   - Each asset goes through six steps, each committed with its time in `asset_index`, so a run resumes: probe, transcript (only clips with sound; Whisper under the GPU lock), scenes, chunks, images, people.
   - Any failed step, a model error included, is recorded, and the asset is skipped until `--retry-failed`. The night goes on with the next file, and ingest stops below the disk stop line (FR-ING-4).
   - Each scan copies the source's licence fields onto its assets again, so a renewed licence reaches clips already ingested.
   - Stills are left out because the renderer takes no image shots yet.
   - Folder paths may use `%VARS%`, so the first source, `%USERPROFILE%\Videos` (named by Abbas, G0.9c), keeps his user name out of the repo.
5. **Scenes come from ffmpeg's scene score, as for films** (ADR 0020), not from PySceneDetect.
   - The scene, chunk, keyframe-vector and face steps live once, in `engine/library/understand.py`, and both the ingest and the film index call them.
   - Films gain a fifth step, `images`. It waits while CLIP is not cached, so a film indexed before this slice gets only that step on the next run.
6. **Vectors live in sqlite-vec** (`sqlite-vec` 0.1.9, loaded by `engine.db.connect()`), as migration 0005 sets out.
   - `text_vec` (384 floats) shares its rowid with `transcript_chunks`, and `image_vec` (512 floats) with `scenes`. FR-UND-3 names chunks and keyframes; moments get their vectors when the moment scorer arrives.
   - The model behind each vector is `transcript_chunks.model` or `scenes.image_model`, and search compares only vectors from the model in use.
   - Rewriting an asset's chunks or scenes drops its vectors first, so no vector points at a row that is gone.
   - The migration copies the bge chunks that P1.6 stored as blobs.
   - The fake text embedder moved to 384 dimensions with signed hashing, so fake vectors fit the table and unrelated texts still score low.
7. **Search filters first, then ranks** (`engine/library/search.py`).
   - SQL keeps only clips the format's tiers allow: videos with people `none` or `yes`, whose licence and source have not expired. Ranking is `vec_distance_cosine` over what is left, so a forbidden clip can neither slip in nor push a clean one out of the top `limit`.
   - `results` holds the best scene of each clip by CLIP, as cutaway candidates.
   - `spoken` holds transcript passages by bge.
   - An empty query lists the eligible clips, as before. Without models the search falls back to the old text match and says so in `note`.
   - The models reach the read tool through `ToolContext.search_models`, not `Providers`, because search calls no vendor.

## Consequences

- The done-when holds twice: on fakes in the unit suite, and on the real bge, CLIP and YuNet under `-m model`.
  - Each of the five sample beats returns its clip first.
  - The clip with a person, the expired stock clip and the film never appear for format C.
  - Real CLIP's margins over the runner-up are 0.049 to 0.110 on the drawn stills.
- Abbas's `Videos` folder (42 clips) is in the sources config but not ingested. P0.6b.3 runs `ce ingest` on it with the real Whisper, and Abbas then marks the faces.
- `ContentEngine-Ingest` (02:00, `scripts/run_ingest.ps1`) exists, and its registration stays a dry run (ADR 0016).
- Matching faces against Abbas's reference set, a digest line for clips waiting on a mark, and stills are left for later slices.
- Film assets are registered with `people_release = unknown`, so format A search and the rights check exclude them until Phase 2 decides how §8.3 applies to film excerpts. P2.5 has to settle that.
