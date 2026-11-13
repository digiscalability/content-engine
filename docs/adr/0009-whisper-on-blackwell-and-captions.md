# ADR 0009: Whisper on Blackwell, word-start refinement, and the captions composition

- Status: accepted, 2026-11-13 (slice P0.5)
- Requirements: FR-UND-1, FR-PRD-6, FR-QA-4, NFR-6, NFR-8, NFR-9

## Context

FR-UND-1 asks for faster-whisper large-v3 on CUDA with word timestamps, a fallback to openai-whisper if CTranslate2 cannot run on the RTX 5070 (Blackwell), and word timings within 0.1 s of a reference. FR-PRD-6 asks for captions from Whisper on the final mix, rendered by HyperFrames. How to get the CUDA libraries, how to measure "within 0.1 s", and how captions are grouped and laid out were open.

## Outcome: no fallback

faster-whisper 1.2.1 on CTranslate2 4.9.0 runs large-v3 in float16 on the RTX 5070 (compute capability 12.0, driver 610.88). openai-whisper and PyTorch are not needed.

| Measure | Result |
|---|---|
| VRAM after load and one transcription | 4,406 MiB (NFR-6 limit 11 GiB) |
| Transcribing the 7.4 s fixture, model warm | 0.7–1.3 s |
| Words recognised | 16 of 16, with punctuation ("practise" heard as "practice") |
| Start error, Whisper alone | mean 0.167 s, median 0.115 s, max 0.430 s |
| Start error, refined, clean speech | mean 0.091 s, median 0.083 s, max 0.210 s |
| Start error, refined, final mix with music under the VO | mean 0.088 s, median 0.083 s, max 0.190 s |

## Decision

1. **CUDA libraries.** CTranslate2's Windows wheel ships cuDNN 9 but not cuBLAS, so the first CUDA call failed with `cublas64_12.dll is not found`. The engine depends on `nvidia-cublas-cu12` (pinned `>=12.8,<=12.9.1.4`, the last release with a Windows wheel; cuBLAS supports Blackwell from 12.8) and puts its `bin` folder on the DLL search path before the model loads (`cuda_dll_dirs`). Everything lives in the project venv; no CUDA toolkit install.
2. **Model.** `Systran/faster-whisper-large-v3` (3.09 GB) in the Hugging Face cache, downloaded on 13 Nov 2026 with Abbas's OK; `HF_HUB_DISABLE_TELEMETRY=1`. Transcription uses `word_timestamps=True`, `vad_filter=True`, `beam_size=5`, language detected.
3. **Word-start refinement.** Whisper's alignment gives the word after a pause the pause's start, so a caption would appear up to 0.43 s early (all large errors were words after a pause). Tighter Silero VAD settings made no difference or made it worse (max error up to 0.93 s). `refine_starts()` moves a word's start to a speech onset that falls inside the word or up to 50 ms after it. Onsets come from ffmpeg `silencedetect` (−40 dB, 0.12 s) on clean voice: for captions, the plan's VO sources placed on the timeline (`voice_onsets`), which are accurate to about 15 ms. Silero on the mix itself ran up to 0.17 s late under music. Whisper still transcribes the **final mix** (FR-PRD-6); only the start times use the VO sources.
4. **Reading "within 0.1 s".** The test requires a mean start error of 0.1 s or less, at least 90% of reference words matched, and no word off by more than 0.25 s. The mean is met (0.091 s and 0.088 s); not every word is within 0.1 s (max 0.21 s), because inside continuous speech Whisper's boundaries run about 0.1 s early. If word-by-word highlights ever look early, the next step is forced alignment (wav2vec2, as WhisperX does), which needs PyTorch and another model.
5. **Reference fixture.** `scripts/make_speech_fixture.ps1` speaks a line with Windows' built-in voice and records each word start from the synthesiser (NFR-9). `ffmpeg silencedetect` on the same audio agrees with it to within 15 ms where speech resumes. `scripts/make_whisper_fixture.py` records the refined large-v3 output, so caption tests run without a GPU.
6. **Caption groups** (`engine/produce/captions.py`), following the HyperFrames caption rules: at most 4 words, a new group at a sentence end, a pause of 150 ms or more, or the limit; one group on screen at a time (a group ends no later than the next starts); nothing before 0.1 s; tokens with no letters or digits dropped. `caption_coverage()` measures FR-QA-4 (the fixture reaches ≥ 95%); the QA check itself lands in P1.3.
7. **Composition.** A transparent root hosts one `captions` sub-composition on a `data-track-kind="captions"` track with one timed clip per group, so Abbas can edit them in the HyperFrames desktop app. The HTML lives in `hyperframes/captions/*.template.html`, filled with `string.Template`; words are HTML-escaped and the timing JSON is safe inside `<script>`. Each group's size comes from HyperFrames' `fitTextFontSize` (max 92 px, 900 px wide), the spoken word turns accent yellow, and the line sits 650 px from the bottom. `hyperframes check` reports 0 errors and one advisory warning, `timeline_track_too_dense` (all groups on one track), which is the layout the HyperFrames caption guide asks for. The style is a stand-in until a look supplies it (P0.8).

## Consequences

- `uv run pytest -m gpu` needs CUDA, the cached model and the cuBLAS wheel; the default and golden runs need none of them.
- P0.7 builds captions in this order: `mix_audio()` → transcribe the mix → `refine_starts(…, voice_onsets(plan, sources))` → `render_captions()` → `render(…, mix=)` with the captions overlay.
- The GPU lock (NFR-6) arrives with P1.1; until then nothing else uses the GPU during a run.
