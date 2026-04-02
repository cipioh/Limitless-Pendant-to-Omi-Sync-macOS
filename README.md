# Limitless to Omi Sync (macOS)

A background Mac service that connects directly to your Limitless Pendant over Bluetooth,
downloads stored audio, transcribes it locally, filters out AI hallucinations, and uploads
clean conversations to your Omi timeline — automatically, every hour.

Supports **MacWhisper** (GUI, with speaker diarization) and **faster-whisper** (CLI,
fully automated, no GUI dependency) as transcription engines.

---

## Why I Built This

When Limitless sold to Meta, Pendant users were left without a path forward — no data export,
no transition plan, and no source code released for the hardware we already owned. The open
source Omi team stepped in, reverse-engineered the pendant's BLE protocol without any help
from Limitless, and gave the community a way to keep using the devices we had invested in.
That effort deserves real credit.

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
  recording boundaries (start/stop markers embedded in the pendant's data stream). Each
  distinct conversation becomes its own file, transcript, and Omi timeline entry rather than
  one long undifferentiated audio blob.
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

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS 13.0+ | Earlier versions may work but are untested |
| Python 3.11+ | `python3 --version` to check |
| [blueutil](https://github.com/toy/blueutil) | Bluetooth circuit breaker: `brew install blueutil` |
| Omi Developer API Key | From **Settings → Developer** in the Omi app |
| **Either** MacWhisper **or** faster-whisper | See [Transcription Engines](#transcription-engines) |

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

### MacWhisper (default — `TRANSCRIPTION_ENGINE=macwhisper`)

[MacWhisper](https://goodsnooze.gumroad.com/l/macwhisper) is a macOS app that runs Whisper
models locally. It supports speaker diarization, meaning it labels each speaker separately
(e.g., "Speaker 1", "Speaker 2") — useful for multi-person conversations.

**Setup:**
1. Install MacWhisper and open it.
2. Go to **Settings → Watch Folder**.
3. Click **Add Folder** and select: `limitless_data/downloads/wav_exports`
4. Set **Output Format** to `.dote`
5. Enable **Automatically Start Transcription**
6. Set **Destination Folder** to the same `wav_exports` directory.

MacWhisper must be **running** (not necessarily in the foreground) for the watch folder to
work. The sync service will wait up to 30 minutes for transcripts to appear.

> **Speaker identification note:** MacWhisper labels speakers as "Speaker 1", "Speaker 2",
> etc. based on voice clustering — it groups voices together but does not identify *who* they
> belong to. There is currently no offline tool that can automatically identify which speaker
> is you without a separate voice enrollment step. If you consistently appear as the same
> speaker label, you can configure `USER_SPEAKER_LABEL=Speaker 1` (or whichever label is you)
> in `.env` so that Omi correctly marks your segments as `is_user: true` and others as
> `is_user: false`. If left blank, all segments are marked as `is_user: true` (the default).

---

### faster-whisper (`TRANSCRIPTION_ENGINE=faster-whisper`)

[faster-whisper](https://github.com/SYSTRAN/faster-whisper) is a Python library that runs
Whisper models locally with no GUI. The pipeline calls `transcribe.py` directly after
conversion — no watch folder, no polling, fully automated end-to-end.

**Note:** faster-whisper does not include speaker diarization. All segments will be attributed
to a single speaker (`SPEAKER_00`). If you need speaker separation, use MacWhisper instead.

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

## Configuration Reference

All settings live in `.env` at the project root. Copy `.env.example` as your starting point.

| Variable | Default | Description |
|---|---|---|
| `OMI_API_KEY` | *(required)* | Your Omi Developer API key |
| `PENDANT_MAC_ADDRESS` | *(auto-detected)* | BLE address of your pendant. Leave blank on first run. |
| `LIMITLESS_BASE_DIR` | *(project root)* | Override data storage location (e.g., an external drive) |
| `TRANSCRIPTION_ENGINE` | `macwhisper` | `macwhisper` or `faster-whisper` |
| `WHISPER_MODEL` | `base` | faster-whisper model size: `tiny` / `base` / `small` / `medium` / `large-v2` / `large-v3` |
| `WHISPER_DEVICE` | `cpu` | faster-whisper device: `cpu` or `cuda` |
| `WHISPER_COMPUTE` | `int8` | faster-whisper compute type: `int8` / `float16` / `float32` |
| `USER_SPEAKER_LABEL` | *(empty)* | Speaker label to mark as `is_user: true` (e.g. `Speaker 1`). Leave blank to mark all speakers as user. |
| `DISCARD_ACTION` | `keep` | What to do with rejected transcripts: `keep` (move to `discarded_audio/`) or `delete` |
| `DISCARD_RETENTION_DAYS` | `7` | Days before `discarded_audio/` files are auto-deleted. `0` = keep forever. |
| `SYNCED_WAV_ACTION` | `keep` | What to do with WAV files after a successful upload: `keep` (move to `synced_to_omi/`) or `delete` |
| `SYNCED_WAV_RETENTION_DAYS` | `7` | Days before WAV files in `synced_to_omi/` are auto-deleted. `0` = keep forever. |
| `SYNCED_JSON_RETENTION_DAYS` | `7` | Days before JSON payload archives in `synced_to_omi/` are auto-deleted. `0` = keep forever. |

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
│   └── send_to_omi.py      Phase 4: quality filter + Omi API upload
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
button for two seconds to stop recording, then hold again for two seconds to restart. The sync
log's `Session Health` line is the early warning signal — a healthy pendant advances 30–60
sessions per hour during active use; a degraded pendant will show a near-zero rate. If
`Session ID unchanged for X minutes` appears in the log, press the button.

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

**Large backlog taking a very long time**
This is expected. The script downloads, converts, and transcribes in 2,000-page chunks (~47
minutes of audio per chunk). For a very large backlog, let it run overnight. You can monitor
progress with `tail -f limitless_data/logs/automation.log`.

---

## License

MIT — see [LICENSE](LICENSE).
