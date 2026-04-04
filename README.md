# Limitless Pendant → Omi Sync (macOS)

A background Mac service that connects directly to your Limitless Pendant over Bluetooth,
downloads stored audio, and syncs it to your Omi timeline — automatically, every hour.

Three approaches are supported depending on how much you want running locally and how much
you want Omi's cloud to handle. See [Choose Your Approach](#choose-your-approach) below.

---

## Why I Built This

When Limitless sold to Meta, Pendant users were left without a path forward — no data export,
no transition plan, and no source code released for the hardware we already owned. The open
source Omi team stepped in, reverse-engineered the pendant's BLE protocol without any help
from Limitless, and gave the community a way to keep using the devices we had invested in.
That effort deserves real credit. None of this would be possible without the hard work and
rapid response from the team at https://www.omi.me

With that said, the Omi integration with the Limitless Pendant is still maturing. Through no
fault of the Omi team — who are working blind without firmware source code or official
documentation — there are rough edges. Syncing can be inconsistent, the native app's offline
sync often stalls, and conversations can go missing if the stars don't align.

That's what this project is for. I spend most of my workday at my MacBook, so I built a
dedicated Mac-based sync service that reliably pulls audio from the pendant and gets it into
Omi — every hour, automatically, without depending on a phone or the native app.

---

## Choose Your Approach

| Approach | Transcription | Speaker ID | Internet | Subscription | Setup |
|---|---|---|---|---|---|
| [Omi Cloud Sync](docs/approach-omi-cloud.md) | Deepgram (Omi cloud) | Yes — biometric via Omi profile | Yes | Unlimited plan recommended¹ | Low |
| [Local → Omi](docs/approach-local-to-omi.md) | Whisper (on your Mac) | Voice clustering only | Upload only | Free tier sufficient | Medium |
| [Fully Offline](docs/approach-offline.md) *(partial)* | Whisper (on your Mac) | Voice clustering only | No | None (No Omi Integration)| Medium |

¹ Omi's free plan includes 1,200 cloud transcription minutes/month. The omi_cloud approach consumes these minutes because Omi runs Deepgram on your audio server-side. A typical day of pendant recording (5–8 hours) would exhaust the free tier within days. The local→omi approach sends only pre-transcribed text, so it doesn't touch the quota at all.

**Omi Cloud Sync** (`TRANSCRIPTION_ENGINE=omi_cloud`) — pulls raw audio off the pendant and
sends it directly to Omi's backend. Omi handles everything: Deepgram transcription, speaker
identification matched against your voice profiles, memory extraction, and timeline
integration. No local ML dependencies. Best results for speaker identification since it uses
the same biometric profiles the Omi app builds from your pendant usage.

**Local → Omi** (`TRANSCRIPTION_ENGINE=macwhisper|faster-whisper|whisperx`) — transcribes
audio locally using Whisper-based models, filters out hallucinations, then uploads clean
transcript segments to Omi. Audio never leaves your Mac; only the transcript text is sent.
Omi handles summaries, memory extraction, and timeline integration using its standard
pipeline. Three local engine options: MacWhisper (GUI, speaker diarization), faster-whisper
(CLI, fully automated), or WhisperX (CLI, automated, with speaker diarization).

**Fully Offline** — collect and transcribe locally with no upload step. *Partially
implemented:* download, convert, and transcribe work fully. Local summarization and
searchable query capability have not been built yet. This completely removes Omi from the 
equation, and you are basically running your own transcription and summarization service.
While a great solution for fully offline processing, you do lose the benefit of the services
Omi has to offer. With that said, this option gives you complete control over all of the
data, where it sits, and how it is processed. 

---

## How It Works

All approaches share Phase 1 (BLE download). What happens after depends on your engine:

```
Limitless Pendant
       │ BLE
       ▼
┌──────────────┐
│ download.py  │  Phase 1 — BLE download → raw Opus .bin files   (all approaches)
└──────┬───────┘
       │
       ├─── omi_cloud ──────────────────────────────────────────────────────────┐
       │                                                                        │
       │    ┌──────────────────────┐                                            │
       │    │ sync_omi_cloud.py    │  Upload → Omi: transcribe + speaker ID +   │
       │    │                      │  memory creation (server-side)             │
       │    └──────────────────────┘                                            │
       │                                                                        │
       ├─── macwhisper / faster-whisper / whisperx ─────────────────────────────┤
       │                                                                        │
       │    ┌──────────────┐   ┌─────────────────────┐   ┌──────────────────┐  │
       │    │  convert.py  │ → │  transcribe.py / MW  │ → │ send_to_omi.py   │  │
       │    │  (Opus→WAV)  │   │  (local Whisper STT) │   │ (filter+upload)  │  │
       │    └──────────────┘   └─────────────────────┘   └──────────────────┘  │
       │                                                                        │
       └────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                                   Omi Timeline
```

**Phase 1 — Download:** Connects to the pendant via BLE and reads raw audio pages from flash
memory in chunks of up to 2,000 pages. If more audio exists, the current chunk is processed
and the script immediately loops back for the next chunk — draining the pendant completely.
Audio is written to disk continuously as it arrives, so a Bluetooth drop mid-download loses
only the last few seconds rather than the entire session.

See the approach-specific docs for what happens after Phase 1.

---

## Key Features

- **Set-and-forget background service** — runs every hour via macOS `launchctl`
- **"Welcome Back" auto-sync** — detects when you return to your Mac after being away and triggers an immediate catch-up sync, without waiting for the next scheduled hour
- **Conversation-aware segmentation** — audio is automatically split at natural 60-second silence gaps; each distinct conversation becomes its own file and Omi entry
- **Drain-and-convert chunking** — safely processes large backlogs in stable 2,000-page chunks
- **Natural gap chunking** — looks for a natural silence boundary in the 1,500–2,000 page zone rather than always hard-cutting at 2,000 pages
- **Bluetooth circuit breaker** — power-cycles the BT chip via `blueutil` and retries up to 15×  if the radio stalls mid-download
- **Streaming crash recovery** — audio written to `.part` file continuously; a crash never loses more than the last few seconds
- **Recording health monitoring** *(opt-in)* — two-tier alert: Warning logged after 5 min of no new sessions, persistent macOS alert after 15 min; based on reverse-engineered VAD session counter protocol

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS 13.0+ | Earlier versions may work but are untested |
| Python 3.11+ | `python3 --version` to check |
| [blueutil](https://github.com/toy/blueutil) | Bluetooth circuit breaker: `brew install blueutil` |
| Approach-specific | See your chosen approach doc below |

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

Creates the directory structure, a Python virtual environment, installs dependencies, and
copies `.env.example` to `.env`.

### 3. Configure for your chosen approach

Follow the setup guide for your approach:

- [Omi Cloud Sync setup](docs/approach-omi-cloud.md#setup)
- [Local → Omi setup](docs/approach-local-to-omi.md#setup)
- [Fully Offline setup](docs/approach-offline.md#setup)

Leave `PENDANT_MAC_ADDRESS` blank — the script auto-discovers your pendant on first run.

---

## Running

### Manual run (for testing / first use)

```bash
./.venv/bin/python3 scripts/pendant_sync.py
```

To watch the log in real time:
```bash
tail -f limitless_data/logs/automation.log
```

### Background daemon (recommended)

Install as a macOS launch agent so it starts automatically at login:

1. Edit `com.limitless.omisync.plist` — replace `YOUR_USERNAME` and the placeholder paths
   with your actual username and project path
2. Copy to your launch agents folder:
   ```bash
   cp com.limitless.omisync.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.limitless.omisync.plist
   ```

To stop the service:
```bash
launchctl unload ~/Library/LaunchAgents/com.limitless.omisync.plist
```

---

## Directory Structure

```
.
├── scripts/
│   ├── pendant_sync.py         Top-level orchestrator (run this)
│   ├── download.py             Phase 1: BLE download from pendant
│   ├── convert.py              Phase 2: Opus .bin → 16kHz WAV
│   ├── transcribe.py           Phase 3 (faster-whisper): WAV → JSON transcript
│   ├── transcribe_whisperx.py  Phase 3 (whisperx): WAV → JSON with speaker labels
│   ├── send_to_omi.py          Phase 4: quality filter + Omi API upload
│   ├── sync_omi_cloud.py       Phase 2–4 replacement for omi_cloud engine
│   └── set_brightness.py       Independent script to set LED brightness
├── docs/
│   ├── approach-omi-cloud.md       Omi Cloud Sync — full setup and reference
│   ├── approach-local-to-omi.md    Local transcription → Omi — full setup and reference
│   └── approach-offline.md         Fully offline — setup, status, and roadmap
├── limitless_data/
│   ├── downloads/
│   │   ├── *.bin               Raw Opus audio from the pendant
│   │   └── wav_exports/        WAV files and transcripts (local engines only)
│   ├── discarded_audio/        Rejected transcripts (local engines only)
│   ├── synced_to_omi/          Successfully uploaded conversations + source audio
│   └── logs/
│       ├── automation.log              Main timestamped sync log
│       └── limitless_download.log      Low-level BLE events
├── .env                        Your configuration (not committed to git)
├── .env.example                Configuration template with all options documented
├── install.sh                  One-command setup script
├── requirements.txt            Python dependencies
└── com.limitless.omisync.plist launchd service definition
```

---

## Sample Log

The transcription engine is logged at the start of every cycle so the rest of the log makes
sense in context:

```
==========================================
[04-03-2026 05:30:00PM] Starting Sync Cycle...
[04-03-2026 05:30:00PM] Transcription engine: omi_cloud
[04-03-2026 05:30:01PM] Attempting connection to Pendant...
...
```

For a full annotated log walkthrough covering all download/transcription states, see the
approach doc for your engine.

---

## Exit Codes

`download.py` communicates its result to `pendant_sync.py` via exit codes:

| Code | Meaning | Action taken |
|------|---------|--------------|
| 0 | Full success — pendant fully drained | Proceed to next phase |
| 2 | Bluetooth hardware error | Circuit breaker: rest + power-cycle radio, retry up to 15× |
| 3 | Chunk limit reached — more data exists | Process current chunk, rest pendant 30s, reconnect |
| 4 | Pendant not found (out of range) | Abort cycle, arm "welcome back" idle-detection |

---

## Data Safety & Acknowledgements

By default, the script **acknowledges** downloaded pages back to the pendant, which tells it
to free that flash memory for new recordings. Acknowledgement is cumulative and destructive —
once acknowledged, those pages cannot be re-downloaded.

The script is designed to be safe:
- Audio is written to disk before any acknowledgement is sent
- Acknowledgements are clamped to the page range that existed at the start of the run
- If the download stalls, the final acknowledgement is skipped entirely

To run in non-destructive read-only mode: `python scripts/download.py --no-ack`

---

## Conversation Segmentation

The pendant embeds three potential signals for splitting audio into conversations. All three
were evaluated:

- **`did_start/stop_recording` flags** — fires on every Opus encoding boundary (~every few
  seconds). Not a conversation signal; reflects the audio encoder lifecycle.
- **Session ID** — increments on every VAD event (every pause in speech). Advances 30–200+
  times per hour during active conversation. Would fragment every recording into dozens of
  tiny files. Only useful as a recording health proxy — `PENDANT_HEALTH_MONITORING` uses a
  two-tier threshold: Warning after 5 min of no new sessions, Unhealthy alert after 15 min.
- **Timestamp gap (current)** — a 60-second gap between consecutive page timestamps means the
  pendant was genuinely silent. This correctly captures transitions between meetings, lunch
  breaks, and end of day without false splits during normal conversational pauses.

---

## Known Issues & Quirks

**Erroneous battery level reports**
The pendant occasionally reports 100% regardless of actual charge. Treat battery readings as
approximate.

**"First Page Sent" vs oldest flash page discrepancy**
The pendant's streaming cursor and its oldest-stored-page counter are tracked independently.
On reconnect the pendant streams from its cursor, not the oldest page. Any gap between the
two values is normal. On multi-chunk downloads the cursor can regress slightly — the sync
script silently skips already-downloaded pages.

**Pendant BLE stall during download**
The pendant's BLE stack occasionally stops sending data mid-download without disconnecting.
The circuit breaker handles this automatically.

**Randomly degraded recording rate**
The pendant occasionally records at ~2–3% of normal rate despite appearing on. Root cause
unknown (firmware issue). Fix: hold the button 2 seconds to stop, hold again to restart. With
`PENDANT_HEALTH_MONITORING=enabled`, a persistent macOS alert fires automatically when this
is detected.

**Offline sync creates a new conversation per upload** *(local → Omi approach)*
The `omi_cloud` engine is significantly better at conversation continuity — even when audio
is uploaded across multiple sync cycles, Omi correctly appends to existing conversations
rather than creating a new one each time. The local → Omi approach (`send_to_omi.py`) does
not yet replicate this behavior; each upload tends to start a fresh conversation. This is a
known limitation to be addressed.

---

## Troubleshooting

**Pendant not found on first run**
Ensure Bluetooth is enabled and your terminal has Bluetooth permission: **System Settings →
Privacy & Security → Bluetooth**.

**"Encryption is insufficient" / Bluetooth encryption error**
The pendant requires a bonded BLE link. Accept the pairing prompt when it appears. If it
recurs, unpair in **System Settings → Bluetooth** and re-run — the script re-pairs
automatically.

**Download stops early even though more audio exists**
Check `limitless_download.log` for "stall" events. The circuit breaker handles it
automatically. For persistent issues try a manual BT radio restart:
```bash
/opt/homebrew/bin/blueutil -p 0 && sleep 5 && /opt/homebrew/bin/blueutil -p 1
```

**Omi API returns 401 Unauthorized** *(local → Omi approach)*
Your `OMI_API_KEY` is missing or incorrect. Keys start with `omi_dev_` and are in the Omi
app under **Settings → Developer**.

**Firebase auth fails** *(omi_cloud approach)*
See [Omi Cloud Sync — Setup](docs/approach-omi-cloud.md#setup) for token extraction steps.
If the refresh token has expired (rare), grab a new one from browser IndexedDB.

**"No module named faster_whisper"**
`./.venv/bin/pip install faster-whisper`

**WhisperX install fails with "requires a different Python"**
WhisperX requires Python 3.10–3.13. Create a dedicated venv:
```bash
python3.13 -m venv .venv-whisperx
./.venv-whisperx/bin/pip install whisperx python-dotenv
```

**Setting LED Brightness**
```bash
./.venv/bin/python scripts/set_brightness.py 50   # 0–100
```
