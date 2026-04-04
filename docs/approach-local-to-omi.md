# Approach: Local Transcription → Omi

**Best for:** Privacy-conscious users, offline transcription with cloud Omi integration, or those who want control over transcription quality/model.

In this mode, audio is pulled off the pendant, converted to WAV, and transcribed locally using Whisper-based models. The resulting transcripts are quality-filtered and uploaded to Omi as structured conversation segments. Omi handles summaries, memory extraction, and timeline integration using its standard LLM pipeline — but the raw audio never leaves your machine.

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
│  transcribe.py      │  Phase 3 — Local speech-to-text → .json / .dote transcript
│  or MacWhisper      │
└──────┬──────────────┘
       │ .json / .dote
       ▼
┌──────────────────┐
│ send_to_omi.py   │  Phase 4 — Quality filter + Omi API upload
└──────┬───────────┘
       ▼
  Omi Timeline
```

---

## Transcription Engine Options

Three local engines are supported. Choose based on your priorities:

| | MacWhisper | faster-whisper | WhisperX |
|---|---|---|---|
| Speaker diarization | Yes | No | Yes |
| Fully automated (no GUI) | No | Yes | Yes |
| Runs offline | Yes | Yes | Yes* |
| Cost | Paid (Pro license) | Free | Free |
| Extra accounts | None | None | Free HuggingFace account |
| Setup complexity | Medium | Low | Medium |

\* Model weights download once on first run, then run fully offline.

> **Speaker identification note:** MacWhisper and WhisperX label speakers by voice clustering — they group voices together but cannot identify *who* each speaker is by name. Check your transcript output to find which label consistently appears for your own voice, then set `USER_SPEAKER_LABEL` in `.env` to that value so Omi marks your segments as `is_user: true`.

---

## Phase 4 — Quality Filter

Before uploading, `send_to_omi.py` runs each transcript through a filter to catch Whisper hallucinations:

1. **Empty check** — skip transcripts with no text
2. **1-word kill switch** — skip single-word transcripts (almost always a hallucination)
3. **Hallucination filter** — skip transcripts containing only action tags (`*like this*`), known Whisper ghost phrases ("thank you", "subscribe", etc.), isolated short utterances after long silences, or sub-100ms glitch segments
4. **Speaker merge** — consecutive segments from the same speaker are merged for a clean Omi timeline UI
5. **Upload** — POST to Omi with accurate `started_at` / `finished_at` timestamps. On success, the payload is archived as `.json`. On failure, the transcript stays in place for the next retry.

---

## Prerequisites

- Omi Developer API Key (from **Settings → Developer** in the Omi app)
- One of the transcription engines below installed

### Subscription requirement

**This approach does not consume Omi cloud transcription minutes.** Because transcription happens locally and only the text is sent to Omi, you don't touch the cloud minute quota at all. The free Omi plan is sufficient — you're only subject to the developer API rate limits (100 requests/minute, 10,000 requests/day), which are unlikely to be hit by a normal sync cycle.

A paid subscription is not required, though Omi's summarization, memory extraction, and other pipeline features work the same regardless of plan tier.

---

## Setup

### 1. Add your Omi API key to `.env`

```env
OMI_API_KEY=omi_dev_your_key_here
```

### 2. Choose and configure a transcription engine

---

### MacWhisper (`TRANSCRIPTION_ENGINE=macwhisper`)

[MacWhisper](https://goodsnooze.gumroad.com/l/macwhisper) is a macOS GUI app running Whisper locally with speaker diarization.

> **Note:** Watch-folder automation is a **Pro** feature requiring a paid license.

**Setup:**
1. Install MacWhisper and open it
2. **Settings → Watch Folder → Add Folder** → select `limitless_data/downloads/wav_exports`
3. Set **Output Format** to `.dote`
4. Enable **Automatically Start Transcription**
5. Set **Destination Folder** to the same `wav_exports` directory

In `.env`:
```env
TRANSCRIPTION_ENGINE=macwhisper
```

MacWhisper must be **running** (not necessarily in the foreground) for the watch folder to work. The sync service polls every 30 seconds, up to 30 minutes. If MacWhisper isn't running, no transcripts are produced and nothing uploads — untranscribed `.wav` files remain in `wav_exports/` until the next cycle when MacWhisper is running.

---

### faster-whisper (`TRANSCRIPTION_ENGINE=faster-whisper`)

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) runs Whisper models in Python with no GUI. Fully automated — no watch folder, no polling.

**Note:** No speaker diarization. All segments are attributed to a single speaker (`SPEAKER_00`). Use MacWhisper or WhisperX if you need speaker separation.

**Setup:**
```bash
./.venv/bin/pip install faster-whisper
```

In `.env`:
```env
TRANSCRIPTION_ENGINE=faster-whisper
WHISPER_MODEL=base         # tiny | base | small | medium | large-v2 | large-v3
WHISPER_DEVICE=cpu         # cpu | cuda
WHISPER_COMPUTE=int8       # int8 | float16 | float32
```

First run downloads the model weights (~150 MB for `base`). Subsequent runs use the cached model.

---

### WhisperX (`TRANSCRIPTION_ENGINE=whisperx`)

[WhisperX](https://github.com/m-bain/whisperX) adds speaker diarization on top of faster-whisper. Three passes per file:
1. **Transcription** — faster-whisper
2. **Alignment** — word-level timestamps
3. **Diarization** — pyannote.audio assigns `SPEAKER_00`, `SPEAKER_01`, etc.

**WhisperX requires Python 3.10–3.13.** Create a separate venv:

```bash
# Use whichever version you have (check: ls /opt/homebrew/bin/python3.*)
python3.13 -m venv .venv-whisperx
./.venv-whisperx/bin/pip install whisperx python-dotenv
```

The pipeline automatically uses `.venv-whisperx` for WhisperX — no other config needed.

**HuggingFace setup** (required for diarization models, free):
1. Create an account at [huggingface.co](https://huggingface.co)
2. Accept the license for each model:
   - [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
3. Generate a READ token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)

In `.env`:
```env
TRANSCRIPTION_ENGINE=whisperx
WHISPER_MODEL=base
WHISPERX_HF_TOKEN=hf_your_token_here
```

After first transcription, check the output to find which speaker label is consistently you, then set:
```env
USER_SPEAKER_LABEL=SPEAKER_00   # or SPEAKER_01, etc.
```

Model weights download once on first run (~1 GB for `base` + pyannote models). All subsequent runs are fully offline.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `OMI_API_KEY` | *(required)* | Your Omi Developer API key |
| `TRANSCRIPTION_ENGINE` | `macwhisper` | `macwhisper`, `faster-whisper`, or `whisperx` |
| `WHISPER_MODEL` | `base` | Model size: `tiny` / `base` / `small` / `medium` / `large-v2` / `large-v3` |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE` | `int8` | `int8` / `float16` / `float32` |
| `WHISPERX_HF_TOKEN` | *(empty)* | Required when `TRANSCRIPTION_ENGINE=whisperx` |
| `USER_SPEAKER_LABEL` | *(empty)* | Speaker label to mark as `is_user: true`. Leave blank to mark all as user. |
| `DISCARD_ACTION` | `keep` | `keep` (move rejected files to `discarded_audio/`) or `delete` |
| `DISCARD_RETENTION_DAYS` | `7` | Days to keep discarded files. `0` = forever. |
| `SYNCED_WAV_ACTION` | `keep` | `keep` (move WAVs to `synced_to_omi/`) or `delete` after upload |
| `SYNCED_WAV_RETENTION_DAYS` | `7` | Days to keep WAV files in `synced_to_omi/`. `0` = forever. |
| `SYNCED_JSON_RETENTION_DAYS` | `7` | Days to keep JSON payload archives. `0` = forever. |

---

## Trade-offs

| | Local Transcription → Omi |
|---|---|
| Transcription quality | High (Whisper-based, model-dependent) |
| Speaker identification | Voice clustering only — no biometric identity matching |
| Requires internet | Only for Omi upload (Phase 4) |
| Privacy | Audio stays local; only transcript text is sent to Omi |
| Local ML dependencies | Yes (faster-whisper or WhisperX) |
| Setup complexity | Medium |
| Processing location | Local (transcription) + Omi cloud (summaries, memory, timeline) |
| **Subscription** | **Free tier sufficient** — no cloud transcription minutes consumed |
