"""
WhisperX Transcription Wrapper  (transcribe_whisperx.py)
---------------------------------------------------------
Role in the pipeline: PHASE 3 of 4 (alternative to MacWhisper and faster-whisper)

This script is called by pendant_sync.py when TRANSCRIPTION_ENGINE=whisperx.
It finds all .wav files in the given directory that don't already have a matching
.json transcript, transcribes them using WhisperX, and writes a standard JSON
file alongside each .wav — in the same format as transcribe.py, but with an
added "speaker" field on each segment.

WhisperX runs three passes over the audio:
  1. Transcription  — faster-whisper under the hood, same model sizes
  2. Alignment      — pins each word to a precise timestamp using a phoneme model
  3. Diarization    — pyannote.audio identifies who is speaking when

The resulting segments carry a "speaker" field (e.g. "SPEAKER_00", "SPEAKER_01")
that send_to_omi.py already knows how to use: set USER_SPEAKER_LABEL=SPEAKER_00
(or whichever label is consistently yours) so Omi marks your segments as
is_user: true and everyone else's as is_user: false.

REQUIREMENTS:
    pip install whisperx
    A free HuggingFace account + token to accept the pyannote model licenses:
      https://huggingface.co/pyannote/speaker-diarization-3.1
      https://huggingface.co/pyannote/segmentation-3.0

CONFIGURATION (via .env):
    WHISPER_MODEL       — Model size. Larger = more accurate but slower.
                          Options: tiny, base, small, medium, large-v2, large-v3
                          Default: base
    WHISPER_DEVICE      — "cpu" or "cuda" (NVIDIA GPU).
                          Default: cpu
    WHISPER_COMPUTE     — "int8" (fast, low RAM) or "float16" / "float32".
                          Default: int8
    WHISPERX_HF_TOKEN   — Your HuggingFace access token. Required for diarization.
                          Get one at https://huggingface.co/settings/tokens

OUTPUT FORMAT:
    For each "04-01-2026 02.30PM to 02.45PM.wav", produces:
    "04-01-2026 02.30PM to 02.45PM.json" in the same directory.

    JSON structure:
    {
      "segments": [
        {"start": 0.0, "end": 2.5, "text": " Hello there.", "speaker": "SPEAKER_00"},
        {"start": 2.5, "end": 5.1, "text": " Hi, how are you?", "speaker": "SPEAKER_01"},
        ...
      ],
      "language": "en",
      "model": "base"
    }

EXIT CODES:
    0 — All .wav files transcribed successfully (or nothing to do).
    1 — One or more files failed to transcribe.
"""

import argparse
import json
import os
import sys

# Ensure Homebrew's bin is on the PATH so ffmpeg (required by whisperx) is
# found even when the script is launched by launchd, which inherits a minimal PATH.
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

WHISPER_MODEL   = os.getenv("WHISPER_MODEL",     "base")
WHISPER_DEVICE  = os.getenv("WHISPER_DEVICE",    "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE",   "int8")
HF_TOKEN        = os.getenv("WHISPERX_HF_TOKEN", "").strip()


def transcribe_directory(wav_dir: Path) -> int:
    """
    Transcribes all .wav files in `wav_dir` that don't have a matching .json yet.

    Returns 0 if all files succeed, 1 if any file fails.
    """
    wav_files = sorted(wav_dir.glob("*.wav"))
    pending = [f for f in wav_files if not (wav_dir / (f.stem + ".json")).exists()]

    if not pending:
        print("No .wav files need transcription.", flush=True)
        return 0

    if not HF_TOKEN:
        print(
            "[!] WHISPERX_HF_TOKEN is not set in .env.\n"
            "    WhisperX requires a HuggingFace token to run the pyannote diarization models.\n"
            "    1. Create a free account at https://huggingface.co\n"
            "    2. Accept the model licenses at:\n"
            "         https://huggingface.co/pyannote/speaker-diarization-3.1\n"
            "         https://huggingface.co/pyannote/segmentation-3.0\n"
            "    3. Generate a token at https://huggingface.co/settings/tokens\n"
            "    4. Add WHISPERX_HF_TOKEN=<your_token> to .env",
            flush=True,
        )
        return 1

    print(f"Loading WhisperX model '{WHISPER_MODEL}' on {WHISPER_DEVICE} ({WHISPER_COMPUTE})...", flush=True)

    try:
        import whisperx
    except ImportError:
        print("[!] whisperx is not installed. Run: pip install whisperx", flush=True)
        return 1

    try:
        model = whisperx.load_model(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    except Exception as e:
        print(f"[!] Failed to load WhisperX model '{WHISPER_MODEL}': {e}", flush=True)
        return 1

    any_failed = False

    for wav_path in pending:
        json_path = wav_dir / (wav_path.stem + ".json")
        print(f"Transcribing: {wav_path.name}", flush=True)

        try:
            # --- Pass 1: Transcription ---
            audio = whisperx.load_audio(str(wav_path))
            result = model.transcribe(audio, language="en", batch_size=16)
            detected_language = result.get("language", "en")

            # --- Pass 2: Word-level alignment ---
            # Aligns each word to a precise timestamp so speaker boundaries can be
            # assigned at the word level rather than at the coarser segment level.
            try:
                align_model, align_metadata = whisperx.load_align_model(
                    language_code=detected_language,
                    device=WHISPER_DEVICE,
                )
                result = whisperx.align(
                    result["segments"],
                    align_model,
                    align_metadata,
                    audio,
                    WHISPER_DEVICE,
                    return_char_alignments=False,
                )
            except Exception as e:
                print(f"  [!] Alignment failed ({e}). Proceeding with unaligned segments — speaker boundaries may be less accurate.", flush=True)

            # --- Pass 3: Diarization ---
            try:
                from whisperx.diarize import DiarizationPipeline
                diarize_model = DiarizationPipeline(
                    use_auth_token=HF_TOKEN,
                    device=WHISPER_DEVICE,
                )
                diarize_segments = diarize_model(audio)
                result = whisperx.assign_word_speakers(diarize_segments, result)
            except Exception as e:
                print(f"  [!] Diarization failed ({e}). Saving transcript without speaker labels.", flush=True)

            # --- Build output segments ---
            segments = []
            for seg in result.get("segments", []):
                # Speaker may be missing on a segment if diarization didn't cover it.
                speaker = seg.get("speaker", "SPEAKER_00")
                segments.append({
                    "start":   round(float(seg["start"]), 3),
                    "end":     round(float(seg["end"]),   3),
                    "text":    seg["text"],
                    "speaker": speaker,
                })

            output = {
                "segments": segments,
                "language": detected_language,
                "model":    WHISPER_MODEL,
            }

            # Write atomically — use a .tmp file and rename so a crash mid-write
            # doesn't leave a partial JSON that looks like a completed transcript.
            tmp_path = json_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2)
            tmp_path.rename(json_path)

            speaker_set = {s["speaker"] for s in segments}
            print(f"  Done: {len(segments)} segments, {len(speaker_set)} speaker(s) → {json_path.name}", flush=True)

        except Exception as e:
            print(f"  [!] Failed to transcribe {wav_path.name}: {e}", flush=True)
            any_failed = True

    return 1 if any_failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transcribe .wav files using WhisperX with speaker diarization.")
    parser.add_argument("wav_dir", help="Directory containing .wav files to transcribe.")
    args = parser.parse_args()

    wav_dir = Path(args.wav_dir).expanduser()
    if not wav_dir.is_dir():
        print(f"[!] Not a directory: {wav_dir}", flush=True)
        sys.exit(1)

    sys.exit(transcribe_directory(wav_dir))
