# Limitless to Omi Sync (macOS)

A background Mac service that connects directly to your Limitless Pendant over Bluetooth,
downloads stored audio, transcribes it locally, filters out AI hallucinations, and uploads
clean conversations to your Omi timeline — automatically, every hour.

Supports **MacWhisper** (GUI, speaker diarization), **faster-whisper** (CLI, fully
automated, no GUI dependency), and **WhisperX** (CLI, fully automated, with speaker
diarization) as transcription engines.

---

## Why I Built This

When Limitless sold to Meta, Pendant users were left without a path forward — no data export,
no transition plan, and no source code released for the hardware we already owned. The open
source Omi team stepped in, reverse-engineered the pendant's BLE protocol without any help
from Limitless, and gave the community a way to keep using the devices we had invested in.
That effort deserves real credit. None of this would be possible with the hard work and rapid
response from the team at https://www.omi.me

With that said, the Omi integration with the Limitless Pendant is still maturing. Through no
fault of the Omi team — who are working blind without firmware source code or official
documentation — there are rough edges. Syncing can be inconsistent, the native app's offline
sync often stalls, and conversations can go missing if the stars don't align.

That's what this project is for. I spend most of my workday at my MacBook, so I built a
dedicated Mac-based sync service that takes the pendant's raw audio, transcribes it locally,
and uploads it to Omi reliably — every hour, automatically, without depending on a phone or
the native app. If that matches your situation, this project is for you.

---

## How It Works

```
   Limitless Pendant
          │ BLE
          ▼
   ┌──────────────┐
   │ download.py  │  Phase 1 — BLE download → raw Opus .bin files
   └──────┬───────┘
          │ .bin (raw Opus)
          ▼
   ┌──────────────┐
   │  convert.py  │  Phase 2 — Opus decode → 16 kHz mono .wav
   └──────┬───────┘
          │ .wav
          ▼
   ┌─────────────────────┐
   │   transcribe.py     │  Phase 3 — Speech-to-text → .dote / .json transcript
   │   or MacWhisper     │
   └──────┬──────────────┘
          │ .dote / .json
          ▼
   ┌──────────────────┐
   │ send_to_omi.py   │  Phase 4 — Quality filter + Omi API upload
   └──────┬───────────┘
          │
          ▼
     Omi Timeline
```

**Phase 1 — Download:** Connects to the pendant via BLE and reads raw audio pages from flash
memory in chunks of up to 2,000 pages. If more audio exists, the current chunk is converted
and the script immediately loops back for the next chunk — draining the pendant completely
before moving on.

The pendant communicates via a custom binary protocol layered on standard BLE GATT. All
encoding and decoding is handled internally; no `.proto` schema files are required.

Audio is written to disk continuously as it arrives, so a Bluetooth drop mid-download loses
only the last few seconds of audio rather than the entire session.

**Phase 2 — Convert:** Decodes the raw Opus-encoded `.bin` files into standard 16 kHz mono
`.wav` files. Source `.bin` files are only deleted after a confirmed successful conversion.

**Phase 3 — Transcribe:** Depending on `TRANSCRIPTION_ENGINE`:
- **`macwhisper`** (default) — MacWhisper watches the `wav_exports/` folder and automatically
  produces `.dote` transcript files. The orchestrator polls every 30 seconds until all `.wav`
  files have matching transcripts, or until the 30-minute timeout.
- **`faster-whisper`** — `transcribe.py` runs as a subprocess immediately after conversion.
  No GUI, no watch folder, fully automated.
- **`whisperx`** — `transcribe_whisperx.py` runs as a subprocess immediately after conversion.
  Like faster-whisper but adds speaker diarization: each segment is labeled with who was
  speaking (`SPEAKER_00`, `SPEAKER_01`, etc.).

**Phase 4 — Filter & Upload:** `send_to_omi.py` runs each transcript through a quality filter
before uploading to the Omi API:
1. **Empty check** — skip transcripts with no text at all.
2. **1-word kill switch** — skip single-word transcripts (almost always a hallucination).
3. **Hallucination filter** — skip transcripts containing *only* action tags (`*like this*`),
   known ghost phrases Whisper hallucinates onto silence, isolated short utterances preceded by
   long silences, or sub-100 ms glitch segments.
4. **Speaker merge** — consecutive segments from the same speaker are merged into single blocks
   for a clean Omi timeline UI.
5. **Upload** — POST to the Omi conversations API with accurate `started_at` and `finished_at`
   timestamps derived from the filename and last segment's end time. On success, the payload is
   archived as a `.json` file. On failure, the transcript stays in place for the next retry.

---

## Key Features

- **Set-and-forget background service** — runs every hour via macOS `launchctl`.
- **"Welcome Back" auto-sync** — detects when you return to your Mac after being away for a
  while (missing a scheduled sync) and triggers an immediate catch-up sync, without waiting
  for the next scheduled hour.
- **Conversation-aware segmentation** — audio is automatically split into individual files at
  natural silence gaps (60+ seconds between timestamps). Each distinct conversation becomes
  its own file, transcript, and Omi timeline entry rather than one long undifferentiated
  audio blob. See [Conversation Segmentation](#conversation-segmentation) for why this
  approach was chosen over the pendant's own session and start/stop markers.
- **Drain-and-convert chunking** — safely processes large backlogs in stable 2,000-page chunks,
  converting each before pulling the next. Prevents Bluetooth timeouts on large downloads.
- **Natural gap chunking** — when a chunk is large, the script looks for a natural 60-second
  recording gap in the 1,500–2,000 page zone as a clean audio boundary, rather than always
  cutting at the hard 2,000-page limit.
- **Bluetooth circuit breaker** — if the radio stalls mid-download, the script power-cycles
  the Bluetooth chip using `blueutil` and retries, up to 15 times per cycle.
- **Streaming crash recovery** — audio is written to a `.part` file continuously. A crash or
  disconnect never loses more than the most recent unacknowledged page.
- **Configurable file retention** — control what happens to discarded and synced files via
  `.env` flags (delete immediately, keep for N days, or keep forever), independently for WAV
  and JSON files.
- **Recording health monitoring** *(opt-in, for continuous recording mode)* — every sync
  checks whether the pendant's internal session counter has advanced since the last sync.
  If the counter is stuck for 30+ minutes, a **persistent macOS alert dialog** pops up and
  stays on screen until dismissed — you don't need to watch any logs. Based on
  reverse-engineering the pendant's VAD (voice activity detection) protocol — each speech
  pause creates a new session, making the session counter a reliable proxy for recording
  health without any proprietary firmware access. Enable via
  `PENDANT_HEALTH_MONITORING=enabled` in `.env` (recommended only for always-on recording).
- **Multi-format transcript support** — accepts `.dote` (MacWhisper), `.json` (faster-whisper
  / whisper.cpp / standard Whisper output), and `.srt` (SubRip subtitles).
- **LED brightness control** — a separate `set_brightness.py` utility lets you adjust the
  pendant's LED brightness (0–100) from the command line without touching the main sync
  workflow. See [Setting LED Brightness](#setting-led-brightness).

---

## Sample Log Walkthrough

The log below is from a real sync session after returning home from band rehearsal. It covers
nearly every state the system can reach in a single run. Annotations explain what each line means.

```
# ── MISSED CYCLE ─────────────────────────────────────────────────────────────
# Pendant was out of range (still at rehearsal) when the hourly sync fired.

[08:13:30PM] Starting Sync Cycle...
[08:13:30PM] Attempting connection to Pendant...
[08:13:46PM]      | Pendant not found (out of range or off). Aborting...  ← BLE scan timed out — pendant not found
[08:13:46PM] Cycle complete.
[08:13:46PM] Will check again in 1 hour, or sooner if you return...       ← idle-detection now armed


# ── WELCOME BACK (early sync) ────────────────────────────────────────────────
# Mouse/keyboard activity detected ~4 minutes later — immediate catch-up sync triggered.

[08:17:47PM] Activity detected after being away. Triggering early sync...  ← no waiting for the next scheduled hour


# ── PENDANT STATUS ───────────────────────────────────────────────────────────
# Connected successfully. Pendant reports its state before any download begins.

[08:17:52PM]      | Battery Level: 32%                                           ← firmware quirk: it will either be correct, or falsely read 100% randomly
[08:17:53PM]      | Oldest Flash Page: 90787                                     ← earliest page still stored in pendant flash
[08:17:53PM]      | Newest Flash Page: 97574                                     ← most recently recorded page (6787 pages ≈ 2.6 hrs)
[08:17:53PM]      | Pendant Status: Healthy (+138 sessions in 187min, 44/hr)     ← health check: session counter advanced normally over 3+ hour period
[08:17:53PM]      | Pendant reported 6788 unread flash pages (02:38:23 of audio) ← total queued for this sync


# ── CHUNK 1 OF 5: DOWNLOAD ───────────────────────────────────────────────────
# Large backlog — processed in 2000-page chunks to avoid BLE timeouts.
# Each "File created" line is one conversation, split at 60-second silence gaps.

[08:17:54PM]      | First Page Sent: 90787                         ← pendant's streaming cursor; authoritative start of this chunk
[08:17:54PM]      | File created from pages 90787 - 90798 (00:11)  ← short clip — likely noise burst at rehearsal start
[08:18:01PM]      | File created from pages 90799 - 90862 (01:31)  ↑
[08:18:23PM]      | File created from pages 90863 - 91081 (05:18)  │ multiple conversations split by 60-second gaps
[08:18:41PM]      | File created from pages 91082 - 91245 (04:00)  │
[08:21:20PM]      | File created from pages 92186 - 92787 (15:17)  ← longest segment in this chunk
[08:21:22PM]      | Downloaded 2001 of 6788 pages - 9.58 p/s - 12 files.  ← chunk limit hit; 4787 pages remain


# ── MID-CYCLE: REST + CONVERT ────────────────────────────────────────────────
# Pendant gets a 30s rest. While it rests, this chunk is converted to WAV.
# Trying to pull much more data at once almost always results in bluetooth errors.
# Conversion runs in parallel with the rest — no time wasted.

[08:21:22PM] More data exists. Giving pendant a short rest... (30s)
[08:21:22PM] Starting WAV Conversion (mid-cycle chunk)...
[08:21:22PM]      | Found 12 .bin files. Converting to .wav...
[08:21:31PM]      | Done! Successfully converted 12 files.
[08:21:31PM]      | Cleaned up 12 raw .bin files.


# ── CHUNK 3 OF 5: ACK CURSOR REGRESSION ──────────────────────────────────────
# The pendant's streaming cursor regressed slightly from last chunk's ACK point.
# Pages already downloaded are silently skipped — no data is lost or duplicated.

[08:26:31PM]      | Battery Level: 100%                             ← erroneous reading — known firmware quirk
[08:26:31PM]      | Oldest Flash Page: 92788                        ← pendant has already recycled earlier flash sectors
[08:26:34PM]      | First Page Sent: 93492                          ← cursor started behind last ACK point...
[08:26:34PM]      | Skipping pages already ACKed (up to 94787)...   ← ...so already-downloaded pages are skipped automatically
[08:29:26PM]      | File created from pages 94788 - 95491 (18:06)   ← download picks up cleanly from the right place


# ── FINAL CHUNK: COMPLETE DRAIN ──────────────────────────────────────────────
# Last chunk — fewer pages than the 2000-page limit, so no rest needed after.
# Lots of short files = band rehearsal winding down (intermittent sound, not conversation).

[08:33:42PM]      | Pendant Status: Healthy (+1 sessions in 4min, 17/hr)  ← end of rehearsal: room going quiet, sparse sound, few VAD events
[08:33:42PM]      | Pendant reported 280 unread flash pages (06:32)
[08:33:52PM]      | File created from pages 97492 - 97588 (02:18)   ↑
[08:33:54PM]      | File created from pages 97589 - 97606 (00:19)   │ short bursts of sound separated by 60-second gaps
[08:33:57PM]      | File created from pages 97640 - 97646 (00:03)   │ (packing up, walking out)
[08:34:13PM]      | File created from pages 97672 - 97771 (02:26)   ↓
[08:34:15PM]      | Downloaded 280 of 280 pages - 8.61 p/s - 10 files.
[08:34:15PM] Download complete. Moving to conversion.


# ── TRANSCRIPTION ─────────────────────────────────────────────────────────────
# MacWhisper had already processed mid-cycle chunks in the background.
# Only the final chunk's files are new — 10 files + 15 already done = 25 total.

[08:34:16PM] Waiting for 25 transcripts... (15/25)  ← MacWhisper already finished 15 during download
[08:34:46PM] All 25 transcripts are ready.          ← last 10 completed


# ── OMI UPLOAD: QUALITY FILTER ────────────────────────────────────────────────
# Every transcript goes through filtering before upload.
# (all 25 not shown — a sample of each filter type)

[08:34:46PM]      | Skipped: 05.11PM to 05.11PM.dote is empty.                     ← nothing transcribed (silence / pure noise)
[08:34:49PM]      | Skipped: 05.15PM to 05.20PM.dote contains only hallucinations. ← Whisper ghost phrases on ambient sound
[08:35:02PM]      | Skipped: 05.56PM to 05.57PM.dote contains only one word.       ← single-word transcript, almost certainly a false positive
[08:34:49PM]      | Uploading 05.12PM to 05.14PM.dote (1 compressed segments)...   ← passed all filters; uploading
[08:34:49PM]      | Success: Uploaded and archived as 05.12PM to 05.14PM.json.     ← payload archived locally for reference


# ── CYCLE COMPLETE ────────────────────────────────────────────────────────────

[08:36:31PM] Cycle complete.
[08:36:31PM] Sleeping. Next scheduled check at 09:36 PM
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS 13.0+ | Earlier versions may work but are untested |
| Python 3.11+ | `python3 --version` to check |
| [blueutil](https://github.com/toy/blueutil) | Bluetooth circuit breaker: `brew install blueutil` |
| Omi Developer API Key | From **Settings → Developer** in the Omi app |
| **Either** MacWhisper **or** faster-whisper/WhisperX | MacWhisper requires a paid Pro license for watch-folder automation. faster-whisper and WhisperX are free. See [Transcription Engines](#transcription-engines) |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/Limitless-Pendant-to-Omi-Sync-macOS.git
cd Limitless-Pendant-to-Omi-Sync-macOS
```

### 2. Run the installer

```bash
bash install.sh
```

This creates the directory structure, a Python virtual environment, installs dependencies,
and copies `.env.example` to `.env`.

### 3. Add your API key

Open `.env` in any text editor and set your Omi API key:

```env
OMI_API_KEY=your_omi_api_key_here
```

Leave `PENDANT_MAC_ADDRESS` blank — the script discovers your pendant automatically on the
first run and saves the address for future sessions.

### 4. Configure your transcription engine

See [Transcription Engines](#transcription-engines) below and set `TRANSCRIPTION_ENGINE` in
`.env` accordingly. MacWhisper requires additional setup; faster-whisper is ready after one
`pip` command.

---

## Transcription Engines

### Comparison

| | MacWhisper | faster-whisper | WhisperX |
|---|---|---|---|
| Speaker diarization | Yes | No | Yes |
| Fully automated (no GUI) | No | Yes | Yes |
| Runs locally / offline | Yes | Yes | Yes* |
| GPU required | No | No | No (but faster with one) |
| Cost | Paid (Pro license) | Free | Free |
| Extra accounts needed | None | None | Free HuggingFace account |
| Setup complexity | Medium (GUI config) | Low (one pip install) | Medium (pip + HF token) |
| Transcription speed | Fast | Fast | Slower (3 passes) |
| Accuracy | High | High | High |

\* Model weights download once on first run, then run fully offline.

> **Speaker identification note:** MacWhisper and WhisperX label speakers by voice clustering —
> they group voices together but cannot identify *who* each speaker is. Check your transcript
> output to find which label consistently appears for your own voice, then set
> `USER_SPEAKER_LABEL` in `.env` to that label so Omi marks your segments as `is_user: true`
> and others as `is_user: false`. If left blank, all segments are marked as `is_user: true`.
> faster-whisper has no diarization and does not produce speaker labels.

---

### MacWhisper (default — `TRANSCRIPTION_ENGINE=macwhisper`)

[MacWhisper](https://goodsnooze.gumroad.com/l/macwhisper) is a macOS app that runs Whisper
models locally. It supports speaker diarization and labels each speaker separately
(e.g., "Speaker 1", "Speaker 2") — useful for multi-person conversations.

> **Note:** Watch-folder automation (required for this pipeline) is a **Pro** feature.
> MacWhisper Pro is a one-time purchase from the link above.

**Setup:**
1. Install MacWhisper and open it.
2. Go to **Settings → Watch Folder**.
3. Click **Add Folder** and select: `limitless_data/downloads/wav_exports`
4. Set **Output Format** to `.dote`
5. Enable **Automatically Start Transcription**
6. Set **Destination Folder** to the same `wav_exports` directory.

MacWhisper must be **running** (not necessarily in the foreground) for the watch folder to
work. The sync service will wait up to 30 minutes for transcripts to appear.

---

### faster-whisper (`TRANSCRIPTION_ENGINE=faster-whisper`)

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) is a Python library that runs
Whisper models locally with no GUI. The pipeline calls `transcribe.py` directly after
conversion — no watch folder, no polling, fully automated end-to-end.

**Note:** faster-whisper does not include speaker diarization. All segments are attributed
to a single speaker (`SPEAKER_00`). If you need speaker separation, use MacWhisper or
WhisperX instead.

**Setup:**
```bash
./.venv/bin/pip install faster-whisper
```

In `.env`, set:
```env
TRANSCRIPTION_ENGINE=faster-whisper
WHISPER_MODEL=base         # tiny | base | small | medium | large-v2 | large-v3
```

First run will download the model weights (~150 MB for `base`). Subsequent runs use the
cached model. A `base` model is a good starting point; use `small` or `medium` for better
accuracy at the cost of speed.

---

### WhisperX (`TRANSCRIPTION_ENGINE=whisperx`)

[WhisperX](https://github.com/m-bain/whisperX) is a Python library built on faster-whisper
that adds speaker diarization and word-level alignment. The pipeline calls
`transcribe_whisperx.py` directly after conversion — no GUI, no watch folder, fully
automated end-to-end.

WhisperX runs three passes over each audio file:
1. **Transcription** — faster-whisper (same models, same accuracy)
2. **Alignment** — pins each word to a precise timestamp so speaker changes mid-sentence
   are handled correctly
3. **Diarization** — [pyannote.audio](https://github.com/pyannote/pyannote-audio) identifies
   who is speaking when, and labels each segment `SPEAKER_00`, `SPEAKER_01`, etc.

The extra passes make it slower than bare faster-whisper, but the tradeoff is structured,
speaker-attributed output rather than one undifferentiated block of text.

**Setup:**

```bash
./.venv/bin/pip install whisperx
```

The diarization models require a free HuggingFace account and a one-time license
acceptance:

1. Create a free account at [huggingface.co](https://huggingface.co)
2. Accept the license for each pyannote model (click **Agree and access repository**):
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
3. Generate a read token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
4. Add it to `.env`:

```env
TRANSCRIPTION_ENGINE=whisperx
WHISPER_MODEL=base         # tiny | base | small | medium | large-v2 | large-v3
WHISPERX_HF_TOKEN=hf_your_token_here
```

Model weights download once on first run (whisper model + pyannote diarization models,
~1 GB total for `base`). All subsequent runs are fully offline.

After transcription, set `USER_SPEAKER_LABEL` in `.env` to whichever speaker label is
consistently you (check a transcript to find out — it will be `SPEAKER_00` or `SPEAKER_01`,
etc.) so Omi correctly attributes your segments.

---

## Configuration Reference

All settings live in `.env` at the project root. Copy `.env.example` as your starting point.

| Variable | Default | Description |
|---|---|---|
| `OMI_API_KEY` | *(required)* | Your Omi Developer API key |
| `PENDANT_MAC_ADDRESS` | *(auto-detected)* | BLE address of your pendant. Leave blank on first run. |
| `LIMITLESS_BASE_DIR` | *(project root)* | Override data storage location (e.g., an external drive) |
| `TRANSCRIPTION_ENGINE` | `macwhisper` | `macwhisper`, `faster-whisper`, or `whisperx` |
| `WHISPER_MODEL` | `base` | Model size (faster-whisper / whisperx): `tiny` / `base` / `small` / `medium` / `large-v2` / `large-v3` |
| `WHISPER_DEVICE` | `cpu` | Device (faster-whisper / whisperx): `cpu` or `cuda` |
| `WHISPER_COMPUTE` | `int8` | Compute type (faster-whisper / whisperx): `int8` / `float16` / `float32` |
| `WHISPERX_HF_TOKEN` | *(empty)* | HuggingFace token for WhisperX diarization models. Required when `TRANSCRIPTION_ENGINE=whisperx`. |
| `USER_SPEAKER_LABEL` | *(empty)* | Speaker label to mark as `is_user: true` (e.g. `Speaker 1`). Leave blank to mark all speakers as user. |
| `DISCARD_ACTION` | `keep` | What to do with rejected transcripts: `keep` (move to `discarded_audio/`) or `delete` |
| `DISCARD_RETENTION_DAYS` | `7` | Days before `discarded_audio/` files are auto-deleted. `0` = keep forever. |
| `SYNCED_WAV_ACTION` | `keep` | What to do with WAV files after a successful upload: `keep` (move to `synced_to_omi/`) or `delete` |
| `SYNCED_WAV_RETENTION_DAYS` | `7` | Days before WAV files in `synced_to_omi/` are auto-deleted. `0` = keep forever. |
| `SYNCED_JSON_RETENTION_DAYS` | `7` | Days before JSON payload archives in `synced_to_omi/` are auto-deleted. `0` = keep forever. |
| `PENDANT_HEALTH_MONITORING` | `disabled` | Set to `enabled` if you use always-on continuous recording. Fires a persistent alert if the pendant silently stops recording. |

---

## Running

### Manual run (for testing / first use)

```bash
./.venv/bin/python3 scripts/pendant_sync.py
```

On first run, the script scans for your pendant and saves its address to `.env`. If you have
a large backlog, expect it to loop through several download chunks — this is normal.

To watch the log in real time:
```bash
tail -f limitless_data/logs/automation.log
```

### Background daemon (recommended)

Install as a macOS launch agent so it starts automatically at login:

1. Edit `com.limitless.omisync.plist` and replace all instances of `YOUR_USERNAME` and
   the placeholder paths with your actual username and project path.
2. Copy it to your launch agents folder:
   ```bash
   cp com.limitless.omisync.plist ~/Library/LaunchAgents/
   ```
3. Load it:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.limitless.omisync.plist
   ```

To stop the service:
```bash
launchctl unload ~/Library/LaunchAgents/com.limitless.omisync.plist
```

macOS notifications are sent at the start and end of each cycle, and on errors — so you
know what's happening without needing to watch the log.

---

## Directory Structure

```
.
├── scripts/
│   ├── pendant_sync.py     Top-level orchestrator (run this)
│   ├── download.py         Phase 1: BLE download from pendant
│   ├── convert.py          Phase 2: Opus .bin → 16kHz WAV
│   ├── transcribe.py       Phase 3 (faster-whisper): WAV → JSON transcript
│   ├── transcribe_whisperx.py  Phase 3 (whisperx): WAV → JSON transcript with speaker labels
│   ├── send_to_omi.py      Phase 4: quality filter + Omi API upload
│   └── set_brightness.py   Independent script to set LED brightness
├── limitless_data/
│   ├── downloads/
│   │   ├── *.bin           Raw Opus audio from the pendant
│   │   └── wav_exports/    Active transcription folder
│   │       ├── *.wav       Decoded audio waiting to be transcribed
│   │       ├── *.dote      MacWhisper transcripts (ready to upload)
│   │       └── *.json      faster-whisper transcripts (ready to upload)
│   ├── discarded_audio/    Rejected files (empty, hallucinations, static)
│   ├── synced_to_omi/      Successfully uploaded conversations
│   │   ├── *.json          Exact Omi API payload archives
│   │   └── *.wav           Source audio (if SYNCED_WAV_ACTION=keep)
│   └── logs/
│       ├── automation.log          Main timestamped sync log
│       └── limitless_download.log  Low-level BLE events
├── .env                    Your configuration (not committed to git)
├── .env.example            Configuration template
├── install.sh              One-command setup script
├── requirements.txt        Python dependencies
└── com.limitless.omisync.plist   launchd service definition
```

---

## Exit Codes

`download.py` communicates its result to `pendant_sync.py` via exit codes:

| Code | Meaning | Action taken |
|------|---------|--------------|
| 0 | Full success — pendant fully drained | Proceed to convert |
| 2 | Bluetooth hardware error | Engage circuit breaker: rest + power-cycle radio, retry up to 15×  |
| 3 | Chunk limit reached — more data exists | Convert current chunk, rest pendant 30 s, reconnect |
| 4 | Pendant not found (out of range) | Abort cycle, arm "welcome back" idle-detection |

---

## Data Safety & Acknowledgements

By default, the script **acknowledges** downloaded pages back to the pendant after a
successful download. This tells the pendant it can free that flash memory for new recordings.
Acknowledgement is **cumulative and destructive** — once acknowledged, those pages cannot be
re-downloaded.

The script is designed to be safe:
- Audio is written to disk before any acknowledgement is sent.
- Acknowledgements are clamped to the page range that existed at the start of the run —
  pages recorded *during* the download are never accidentally acknowledged.
- If the download stalls, the final acknowledgement is skipped entirely.

To run in non-destructive read-only mode, add `--no-ack` when calling `download.py` directly.

---

## Conversation Segmentation

The Limitless Pendant embeds three potential signals that could be used to split audio into
separate conversations. All three were evaluated before settling on the current approach.

**Signal 1 — `did_start_recording` / `did_stop_recording` flags**
Each flash page contains per-chunk Opus encoding markers embedded in the binary stream.
These flags change on every Opus encoding boundary — roughly every 2–3 pages (a few seconds
of audio). They fire constantly throughout a recording and are not meaningful as conversation
boundaries; they reflect the internal audio encoder lifecycle, not user-level sessions.

**Signal 2 — Session ID**
The pendant increments its internal session counter on every VAD (Voice Activity Detection)
event — i.e., every time you pause speaking, even briefly. During an active conversation, the
session ID advances 30–200+ times per hour. Splitting on session ID changes would produce
dozens of tiny files per hour and fragment conversations at every breath.

The session ID *does* have one genuine use: as a recording health proxy. Because it advances
predictably during active speech, a counter that hasn't moved in 30+ minutes is a reliable
signal that the pendant has silently stopped recording. This is what
`PENDANT_HEALTH_MONITORING` uses.

**Signal 3 — Timestamp gap (current implementation)**
Each flash page carries a millisecond-precision timestamp. A gap of 60+ seconds between
consecutive page timestamps means the pendant was genuinely silent (or not recording) for
that period — a natural conversation boundary. This correctly captures the transition between
meetings, lunch breaks, end of day, and so on, without generating false splits during normal
conversational pauses.

All three strategies were tested against the same recorded dataset. The 60-second gap
approach produced clean, human-meaningful conversation boundaries. The other two produced
fragmented, overlapping, or meaningless splits.

---

## Known Issues & Quirks

**Erroneous battery level reports**
The pendant occasionally reports incorrect battery levels — it can report 100% regardless of
actual charge level. This appears to be a firmware quirk and is not specific to any particular
charge range. Battery readings in the log should be treated as approximate. The sync process
itself is not affected by incorrect battery readings.

**"First Page Sent" vs oldest flash page discrepancy**
The pendant tracks two things independently: `oldest_flash_page` (the oldest page physically
present in flash — updated lazily as storage sectors are recycled) and its streaming cursor
(where it left off sending pages in the last session). On reconnect, the pendant streams from
its cursor, not from the oldest stored page. Any gap between these values is normal — it tends
to be larger after a big backlog recovery and smaller in steady-state hourly syncing.
`First Page Sent` shows where the pendant's streaming cursor began. On multi-chunk downloads,
the cursor can regress slightly from the previously ACKed position — the sync script detects
this and silently skips any already-downloaded pages, so the first file's actual starting
page may be slightly higher than `First Page Sent`.

**Pendant BLE stall during download**
The pendant's BLE stack occasionally stops sending data mid-download without disconnecting.
The Bluetooth circuit breaker handles this automatically: it power-cycles the radio and retries
up to 15 times per sync cycle. If it consistently hits the retry limit, check for interference
or try moving closer to the Mac during sync.

**Randomly degraded recording rate**
The pendant occasionally enters a state where it records at a severely reduced rate (~2–3%
of normal) despite appearing to be on and connected. The root cause is unknown — it is a
firmware issue and BLE commands cannot fix it. The symptom is very little new audio appearing
between syncs even during active conversation. The fix is a physical button press: hold the
button for two seconds to stop recording, then hold again for two seconds to restart. If
`PENDANT_HEALTH_MONITORING=enabled` is set in `.env`, a persistent macOS alert dialog will
appear automatically when this condition is detected — you don't need to watch the logs.

---

## Troubleshooting

**Pendant not found on first run**
- Make sure Bluetooth is enabled and your terminal has Bluetooth permission:
  **System Settings → Privacy & Security → Bluetooth**.
- The pendant may advertise as "Pendant" — the script handles both "Pendant" and "Limitless".

**"Encryption is insufficient" / Bluetooth encryption error**
The pendant requires a bonded (encrypted) BLE link. Do not use `--no-pair`. Accept the system
pairing prompt when it appears. If the error recurs, try unpairing in **System Settings →
Bluetooth**, then re-running — the script will re-pair automatically.

**Download stops early even though more audio exists**
Check `limitless_download.log` for "stall" events. Usually Bluetooth congestion — the circuit
breaker handles it automatically on the next cycle. If it happens consistently, try a manual
Bluetooth radio restart:
```bash
/opt/homebrew/bin/blueutil -p 0 && sleep 5 && /opt/homebrew/bin/blueutil -p 1
```

**MacWhisper isn't producing transcript files**
- Verify the Watch Folder path points to `limitless_data/downloads/wav_exports`.
- Confirm **Automatically Start Transcription** is enabled and the Output Format is `.dote`.
- MacWhisper must be running (not just installed). It does not need to be in the foreground.

**Omi API returns 401 Unauthorized**
Your `OMI_API_KEY` is missing or incorrect. Verify it in `.env` — keys start with `omi_dev_`
and are found in the Omi app under **Settings → Developer**.

**"No module named faster_whisper"**
Run: `./.venv/bin/pip install faster-whisper`

**"No module named whisperx"**
Run: `./.venv/bin/pip install whisperx`

**WhisperX diarization fails with "WHISPERX_HF_TOKEN is not set"**
Follow the WhisperX setup steps in [Transcription Engines](#transcription-engines): create a
HuggingFace account, accept the pyannote model licenses, generate a token, and add
`WHISPERX_HF_TOKEN=<token>` to `.env`.

**WhisperX diarization fails with a 401 or access error**
The HuggingFace token is present but the pyannote model licenses haven't been accepted yet.
Visit both model pages and click **Agree and access repository**:
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

**Large backlog taking a very long time**
This is expected. The script downloads, converts, and transcribes in 2,000-page chunks (~47
minutes of audio per chunk). For a very large backlog, let it run overnight. You can monitor
progress with `tail -f limitless_data/logs/automation.log`.

---

## Setting LED Brightness

The pendant's LED brightness can be adjusted using `set_brightness.py` — a standalone
utility that is separate from the main sync pipeline. Run it when you want
to dim or brighten the LED; it has no effect on sync behavior.

If `PENDANT_MAC_ADDRESS` is already set in your `.env` file (populated automatically after
the first successful sync), the script connects directly without scanning — a few seconds
instead of the usual 8-second BLE discovery scan.

**Usage:**

```bash
# Use address from .env automatically, or fall back to a scan
python scripts/set_brightness.py 75

# Connect to a specific device by address
python scripts/set_brightness.py --address AA:BB:CC:DD:EE:FF 50

# See all options
python scripts/set_brightness.py --help
```

**Brightness levels:**

| Value | Effect |
|-------|--------|
| `0`   | Off |
| `1-99`  |Variable range of brightness |
| `100` | Full brightness |

Values outside 0–100 are clamped automatically with a printed warning.

> **Note:** Brightness is write-only. The pendant's firmware does not expose a readable
> BLE characteristic for the current brightness level, so there is no way to query what
> it is currently set to. The script prints the value it sent, not a confirmed readback.

---

## License

MIT — see [LICENSE](LICENSE).
