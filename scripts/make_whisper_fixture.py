"""Record faster-whisper large-v3's refined word timings for the speech fixture.

Caption tests run on these real transcription results without a GPU. Re-run after
changing the transcriber or the refinement (needs CUDA and the cached model):

    uv run python scripts/make_whisper_fixture.py
"""

import json
from pathlib import Path

from engine.library.transcribe import FasterWhisperTranscriber, refine_starts, speech_onsets

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "speech"


def main() -> None:
    audio = FIXTURE / "discipline.wav"
    transcript = FasterWhisperTranscriber().transcribe(audio)
    words = refine_starts(transcript.words, speech_onsets(audio))
    doc = {
        "source": "discipline.wav",
        "model": transcript.model,
        "language": transcript.language,
        "refined_with": "speech_onsets (ffmpeg silencedetect -40 dB, 0.12 s)",
        "words": [
            {"text": w.text, "start": w.start, "end": w.end, "probability": round(w.probability, 3)}
            for w in words
        ],
    }
    out = FIXTURE / "discipline.whisper.json"
    out.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(words)} words)")


if __name__ == "__main__":
    main()
