# Approach: Fully Offline (Local Collection + Transcription)

**Best for:** Maximum privacy, air-gapped environments, or building a local audio archive independent of any cloud service.

> **Status: Partially complete.** Audio collection and local transcription are fully functional. Conversation summarization and local query capability have not yet been built. If you need to search or summarize your recordings offline, this work remains to be done.

In this mode, audio is pulled from the pendant, converted to WAV, and transcribed locally — but never uploaded anywhere. The result is a collection of timestamped transcript files (`.json`) on disk. What is missing is a layer on top to summarize, index, and query those transcripts locally.

```
Limitless Pendant
       │ BLE
       ▼
┌──────────────┐
│ download.py  │  Phase 1 — BLE download → raw Opus .bin files
└──────┬───────┘
       │ .bin
       ▼
┌──────────────┐
│  convert.py  │  Phase 2 — Opus decode → 16 kHz mono .wav
└──────┬───────┘
       │ .wav
       ▼
┌─────────────────────┐
│  transcribe.py      │  Phase 3 — Local speech-to-text → .json transcript
│  transcribe_        │
│  whisperx.py        │
└──────┬──────────────┘
       │ .json (local only — not uploaded)
       ▼
  Local archive
  (summaries and querying not yet implemented)
```

---

## What Works Today

| Capability | Status |
|---|---|
| BLE audio download from pendant | Complete |
| Opus → WAV conversion | Complete |
| Local transcription (faster-whisper) | Complete |
| Local transcription with speaker diarization (WhisperX) | Complete |
| Timestamped `.json` transcript archive | Complete |
| Conversation summarization (local LLM) | Not implemented |
| Local search / query interface | Not implemented |

---

## Prerequisites

- No Omi account required
- No API keys required
- Python + one of the transcription engines below

---

## Setup

### 1. Choose a transcription engine

For offline use, faster-whisper or WhisperX are the practical options (MacWhisper produces `.dote` files which are less portable, and requires a paid license).

**faster-whisper** (no speaker diarization):
```bash
./.venv/bin/pip install faster-whisper
```

**WhisperX** (with speaker diarization — requires Python 3.10–3.13 and a free HuggingFace account):
```bash
python3.13 -m venv .venv-whisperx
./.venv-whisperx/bin/pip install whisperx python-dotenv
```

See [Local Transcription → Omi](approach-local-to-omi.md#whisperx-transcription_enginewhisperx) for the full WhisperX setup, including the required HuggingFace model licenses.

### 2. Configure `.env`

```env
TRANSCRIPTION_ENGINE=faster-whisper   # or whisperx
WHISPER_MODEL=base

# OMI_API_KEY is not required — leave it blank or omit it
# But PENDANT_MAC_ADDRESS is still needed (auto-discovered on first run)
```

> **Note:** The startup check in `pendant_sync.py` currently requires `OMI_API_KEY` for all non-`omi_cloud` engines. For a fully offline setup you will need to comment out or bypass that validation until a proper `offline` engine mode is added.

### 3. Run transcription only (without uploading)

Run the orchestrator normally — it will download, convert, and transcribe. Phase 4 (Omi upload) will attempt to run but will fail gracefully if no API key is set. Transcripts are left in `wav_exports/` rather than being archived.

Alternatively, run the transcription scripts directly:

```bash
# faster-whisper only
./.venv/bin/python scripts/transcribe.py limitless_data/downloads/wav_exports/

# WhisperX with speaker diarization
./.venv-whisperx/bin/python scripts/transcribe_whisperx.py limitless_data/downloads/wav_exports/
```

Transcripts are written as `.json` files alongside the `.wav` files.

---

## Transcript Format

Each `.json` transcript contains:

```json
{
  "segments": [
    {
      "start": 0.0,
      "end": 4.2,
      "text": "Hey, what time is dinner?",
      "speaker": "SPEAKER_00"
    },
    ...
  ],
  "language": "en",
  "model": "base"
}
```

The filename encodes the recording time: `04-03-2026 05.30PM to 05.45PM.json`

---

## What Would Complete This Approach

To make fully offline useful beyond raw transcript storage, the following would need to be built:

1. **Local summarization** — run a local LLM (e.g., Ollama + Llama 3 / Mistral) over each transcript to generate a title, summary, and action items — equivalent to what Omi's cloud pipeline produces.

2. **Local index / search** — build a searchable index of transcripts (e.g., a simple SQLite FTS index, or a vector store with a local embedding model) so you can query "what did we discuss about the project last Tuesday?"

3. **`offline` engine mode** — add a `TRANSCRIPTION_ENGINE=offline` option to `pendant_sync.py` that explicitly skips Phase 4 and removes the `OMI_API_KEY` requirement.

If you build any of these, contributions are welcome.
