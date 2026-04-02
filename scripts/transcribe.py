"""
faster-whisper Transcription Wrapper  (transcribe.py)
------------------------------------------------------
Role in the pipeline: PHASE 3 of 4 (alternative to MacWhisper)

This script is called by pendant_sync.py when TRANSCRIPTION_ENGINE=faster-whisper.
It finds all .wav files in the given directory that don't already have a matching
.json transcript, transcribes them using faster-whisper, and writes a standard
Whisper JSON file alongside each .wav.

This is a drop-in replacement for the MacWhisper watch-folder approach. It requires
no GUI, runs synchronously as part of the pipeline, and produces output that
send_to_omi.py already knows how to consume.

REQUIREMENTS:
    pip install faster-whisper

CONFIGURATION (via .env):
    WHISPER_MODEL   — Model size to use. Larger = more accurate but slower.
                      Options: tiny, base, small, medium, large-v2, large-v3
                      Default: base
    WHISPER_DEVICE  — "cpu" or "cuda" (if you have a compatible NVIDIA GPU).
                      Default: cpu
    WHISPER_COMPUTE — "int8" (fast, low RAM) or "float16" / "float32" (more accurate).
                      Default: int8

OUTPUT FORMAT:
    For each "04-01-2026 02.30PM to 02.45PM.wav", produces:
    "04-01-2026 02.30PM to 02.45PM.json" in the same directory.

    JSON structure:
    {
      "segments": [
        {"start": 0.0, "end": 2.5, "text": " Hello there."},
        ...
      ],
      "language": "en",
      "model": "base"
    }

    Note: faster-whisper does not produce speaker diarization. All segments will
    be treated as SPEAKER_00 in send_to_omi.py. Set USER_SPEAKER_LABEL= (empty)
    to keep all segments marked as is_user: true (the default).

EXIT CODES:
    0 — All .wav files transcribed successfully (or nothing to do).
    1 — One or more files failed to transcribe.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

WHISPER_MODEL   = os.getenv("WHISPER_MODEL",   "base")
WHISPER_DEVICE  = os.getenv("WHISPER_DEVICE",  "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")


def transcribe_directory(wav_dir: Path) -> int:
    """
    Transcribes all .wav files in `wav_dir` that don't have a matching .json yet.

    Returns 0 if all files succeed, 1 if any file fails.
    """
    wav_files = sorted(wav_dir.glob("*.wav"))

    # Filter to only those without an existing transcript.
    pending = [f for f in wav_files if not (wav_dir / (f.stem + ".json")).exists()]

    if not pending:
        print("No .wav files need transcription.", flush=True)
        return 0

    print(f"Loading faster-whisper model '{WHISPER_MODEL}' on {WHISPER_DEVICE} ({WHISPER_COMPUTE})...", flush=True)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[!] faster-whisper is not installed. Run: pip install faster-whisper", flush=True)
        return 1

    try:
        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    except Exception as e:
        print(f"[!] Failed to load model '{WHISPER_MODEL}': {e}", flush=True)
        return 1

    any_failed = False

    for wav_path in pending:
        json_path = wav_dir / (wav_path.stem + ".json")
        print(f"Transcribing: {wav_path.name}", flush=True)

        try:
            segments_iter, info = model.transcribe(
                str(wav_path),
                language="en",          # Pendant records in one language — skip auto-detect overhead
                beam_size=5,
                vad_filter=True,        # Skip silent sections — speeds up transcription
                vad_parameters={"min_silence_duration_ms": 500},
            )

            segments = []
            for seg in segments_iter:
                segments.append({
                    "start": round(seg.start, 3),
                    "end":   round(seg.end,   3),
                    "text":  seg.text,
                })

            output = {
                "segments": segments,
                "language": info.language,
                "model":    WHISPER_MODEL,
            }

            # Write atomically — use a .tmp file and rename so a crash mid-write
            # doesn't leave a partial JSON that looks like a completed transcript.
            tmp_path = json_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2)
            tmp_path.rename(json_path)

            print(f"  Done: {len(segments)} segments → {json_path.name}", flush=True)

        except Exception as e:
            print(f"  [!] Failed to transcribe {wav_path.name}: {e}", flush=True)
            any_failed = True

    return 1 if any_failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transcribe .wav files using faster-whisper.")
    parser.add_argument("wav_dir", help="Directory containing .wav files to transcribe.")
    args = parser.parse_args()

    wav_dir = Path(args.wav_dir).expanduser()
    if not wav_dir.is_dir():
        print(f"[!] Not a directory: {wav_dir}", flush=True)
        sys.exit(1)

    sys.exit(transcribe_directory(wav_dir))
