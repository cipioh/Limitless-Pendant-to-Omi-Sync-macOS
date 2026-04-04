"""
Limitless Pendant Sync Orchestrator  (pendant_sync.py)
-------------------------------------------------------
Role in the pipeline: TOP-LEVEL CONTROLLER

This is the main script you run (or install as a background daemon).
It drives the complete 4-phase pipeline on a recurring schedule:

    Phase 1 — DOWNLOAD:    Connect to the Limitless Pendant via BLE and pull
                            raw audio pages. If total pages >= 2,000, download
                            looks for a natural 60-second gap in the 1,500-2,000
                            page zone as a clean break point; otherwise hard-stops
                            at 2,000. On exit code 3 (more data exists), the
                            orchestrator converts the chunk, rests the pendant
                            for 30 seconds, then reconnects for the next chunk.
                            Includes a Bluetooth circuit-breaker that power-cycles
                            the radio on hardware stalls (up to 15 times per cycle).

    Phase 2 — CONVERT:     Call convert.py to decode the raw Opus `.bin` files
                            into standard `.wav` audio files. Only deletes the
                            source `.bin` files if conversion exits cleanly (code 0).

    Phase 3 — TRANSCRIBE:  If TRANSCRIPTION_ENGINE=faster-whisper, runs
                            transcribe.py directly as a subprocess (no polling
                            needed). If TRANSCRIPTION_ENGINE=whisperx, runs
                            transcribe_whisperx.py (same, but with speaker
                            diarization). If TRANSCRIPTION_ENGINE=macwhisper
                            (default), waits for MacWhisper's watch-folder
                            automation to produce `.dote` or `.json` transcript
                            files for every `.wav`. Polls every 30 seconds, up
                            to 30 minutes. If a backlog of 5+ ready transcripts
                            builds up while waiting, triggers a background upload
                            pass so the pipeline doesn't stall entirely.
                            If TRANSCRIPTION_ENGINE=omi_cloud, Phases 2, 3, and
                            4 are replaced entirely: the raw `.bin` files are
                            uploaded directly to Omi's /v2/sync-local-files API,
                            which runs Deepgram transcription + speaker ID
                            server-side (same as the Omi mobile app's offline
                            batch sync). No local whisper or pyannote needed.

    Phase 4 — OMI IMPORT:  Call send_to_omi.py to quality-filter the transcripts
                            and POST them to the Omi API. Skipped when
                            TRANSCRIPTION_ENGINE=omi_cloud (handled in Phase 3).

SERVICE LOOP:
    After each cycle, the orchestrator sleeps for CHECK_INTERVAL_SECONDS (1 hour)
    before running again. It also monitors Mac idle time. If you walked away
    (idle > 5 minutes), the pendant likely dropped its BLE connection and missed
    its scheduled sync. When you return to the keyboard (idle drops below 1 minute),
    the service detects the activity transition and triggers an early "welcome back"
    sync immediately, without waiting for the next scheduled hour.

EXIT CODES (from download.py — interpreted here):
    0 — Complete success (all pages downloaded, pendant fully drained)
    2 — Bluetooth hardware error (circuit breaker engages)
    3 — Partial success (safe chunk limit reached, more data remains)
    4 — Pendant not found (out of range or powered off)
"""

import asyncio
import subprocess
import os
import sys
import time
import traceback
import threading
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv, set_key

# ==========================================
# CONFIGURATION
# ==========================================

# Resolve paths relative to this script's location so the project can live
# anywhere on the filesystem. The .env file is always at the project root,
# one level above the scripts/ directory.
CURRENT_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_DIR = CURRENT_SCRIPT_DIR.parent
ENV_PATH = DEFAULT_BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# BASE_DIR can be overridden via LIMITLESS_BASE_DIR in .env to store data on a
# different volume. Everything else is derived from it.
BASE_DIR = Path(os.getenv("LIMITLESS_BASE_DIR", DEFAULT_BASE_DIR)).expanduser()
SCRIPTS_DIR = BASE_DIR / "scripts"
DOWNLOAD_DIR = BASE_DIR / "limitless_data/downloads"
TRANSCRIPT_DIR = DOWNLOAD_DIR / "wav_exports"   # MacWhisper watch folder
LOG_FILE = BASE_DIR / "limitless_data/logs/automation.log"

OMI_KEY = os.getenv("OMI_API_KEY")
PENDANT_MAC_ADDRESS = os.getenv("PENDANT_MAC_ADDRESS")

# omi_cloud engine credentials (only needed when TRANSCRIPTION_ENGINE=omi_cloud).
# sync_omi_cloud.py reads these directly from env; pendant_sync.py just validates
# that at least one is present so the cycle fails fast rather than mid-sync.
OMI_FIREBASE_TOKEN         = os.getenv("OMI_FIREBASE_TOKEN", "")
OMI_FIREBASE_REFRESH_TOKEN = os.getenv("OMI_FIREBASE_REFRESH_TOKEN", "")
OMI_FIREBASE_WEB_API_KEY   = os.getenv("OMI_FIREBASE_WEB_API_KEY", "")

# How long to sleep between sync cycle attempts. 3600 = 1 hour.
CHECK_INTERVAL_SECONDS = 3600

# How long to rest the pendant between download chunks (exit code 3).
# Gives the BLE radio and pendant hardware time to recover before reconnecting.
BT_CHUNK_REST_SECONDS = 30

# Hard timeout for a single download.py subprocess invocation.
# If the script hangs (BLE stuck, never stalls or exits), kill it and treat
# it as a Bluetooth error so the circuit breaker can reset the radio.
DOWNLOAD_TIMEOUT_SECONDS = 1200  # 20 minutes

# Idle time thresholds for the "welcome back" early-sync trigger.
IDLE_AWAY_SECONDS = 300   # >5 min idle = user walked away
IDLE_BACK_SECONDS = 60    # <1 min idle = user has returned

# Always use the project's own virtual environment Python, not the system Python,
# to ensure all dependencies (bleak, opuslib, etc.) are available.
VENV_PYTHON = BASE_DIR / ".venv/bin/python3"

# WhisperX requires Python <3.14 and lives in a separate venv (.venv-whisperx).
# If that venv exists, use it for transcribe_whisperx.py. If not, fall back to
# the main venv — which will produce a clear ImportError explaining the problem.
_whisperx_venv = BASE_DIR / ".venv-whisperx/bin/python3"
WHISPERX_PYTHON = _whisperx_venv if _whisperx_venv.exists() else VENV_PYTHON

# TRANSCRIPTION_ENGINE controls which tool handles Phase 3.
#   macwhisper    — default. MacWhisper watches the wav_exports/ folder and
#                   produces .dote files automatically. This script polls for them.
#   faster-whisper — runs transcribe.py as a subprocess immediately after convert.py.
#                   No GUI app or watch-folder needed. Requires: pip install faster-whisper.
#   whisperx      — runs transcribe_whisperx.py. Like faster-whisper but adds speaker
#                   diarization (who said what). Requires: pip install whisperx and a
#                   HuggingFace token set in WHISPERX_HF_TOKEN.
#   omi_cloud     — skips convert + transcribe entirely. Uploads the raw .bin files
#                   directly to Omi's /v2/sync-local-files endpoint (same path as the
#                   Omi mobile app's offline batch sync). Omi runs Deepgram Nova-3
#                   transcription + speaker identification server-side. Requires
#                   OMI_EMAIL and OMI_PASSWORD in .env. No local ML dependencies needed.
TRANSCRIPTION_ENGINE = os.getenv("TRANSCRIPTION_ENGINE", "macwhisper").lower()

# ==========================================
# STARTUP VALIDATION
# ==========================================
# Fail fast with a clear message rather than crashing cryptically later.
if not PENDANT_MAC_ADDRESS:
    sys.exit("ERROR: PENDANT_MAC_ADDRESS is not set in .env — cannot connect to pendant.")
if TRANSCRIPTION_ENGINE == "omi_cloud":
    if not OMI_FIREBASE_TOKEN and not OMI_FIREBASE_REFRESH_TOKEN:
        sys.exit(
            "ERROR: TRANSCRIPTION_ENGINE=omi_cloud requires Firebase credentials in .env.\n"
            "Set OMI_FIREBASE_TOKEN (Bearer token from browser) and/or\n"
            "OMI_FIREBASE_REFRESH_TOKEN (from browser IndexedDB → stsTokenManager.refreshToken)."
        )
elif not OMI_KEY:
    sys.exit("ERROR: OMI_API_KEY is not set in .env — cannot upload to Omi.")


# ==========================================
# SYSTEM UTILITIES
# ==========================================

def notify(title, message):
    """
    Sends a macOS system notification banner via AppleScript.
    Used for cycle-start, completion, and error events so you're aware
    without needing to watch the log file.

    Uses a list-form subprocess call (no shell=True) to avoid shell
    injection if title or message ever contain quotes or special characters.
    """
    subprocess.run(
        ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
        capture_output=True  # Suppress osascript output from cluttering the log
    )

def notify_alert(title, message):
    """
    Sends a persistent macOS alert dialog via AppleScript (display alert).
    Unlike display notification, this stays on screen until the user clicks OK —
    use for critical conditions that require user action (e.g. pendant not recording).

    Uses a flag file to prevent stacking duplicate alerts. If an alert is already
    on screen (flag exists), this call is silently skipped. Once the user dismisses
    the alert, the flag is cleared so future alerts can fire again.
    """
    flag_file = BASE_DIR / "limitless_data" / "logs" / "alert_active.flag"
    if flag_file.exists():
        return

    def _show():
        flag_file.touch()
        try:
            subprocess.run(
                ["osascript", "-e", f'display alert "{title}" message "{message}" as warning'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            flag_file.unlink(missing_ok=True)

    threading.Thread(target=_show, daemon=True).start()

def log(message, separator=False):
    """
    Appends a timestamped message to the log file and prints it to stdout.

    The `separator` flag writes a visual divider line before the message,
    used to mark the beginning of a new sync cycle in the log so they're
    easy to scan.

    All log timestamps use the format: MM-DD-YYYY HH:MM:SSam/pm
    """
    if separator:
        with open(LOG_FILE, "a") as f:
            f.write("==========================================\n")
            print("==========================================")

    timestamp = datetime.now().strftime('%m-%d-%Y %I:%M:%S%p')
    formatted_msg = f"[{timestamp}] {message}"

    with open(LOG_FILE, "a") as f:
        f.write(formatted_msg + "\n")
    print(formatted_msg, flush=True)

def get_mac_idle_seconds():
    """
    Returns the number of seconds since the last mouse or keyboard input.

    Uses macOS's IOKit HID system (via `ioreg`) to read the hardware idle
    counter. This is the same value the screen saver uses to decide when
    to activate. We use it to detect "user walked away" / "user returned"
    transitions for the early-sync trigger in the service loop.

    Returns 0 on any error so the caller's logic defaults to "user is active."
    """
    try:
        cmd = "ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}'"
        output = subprocess.check_output(cmd, shell=True, text=True).strip()
        return int(output)
    except Exception:
        return 0

async def run_step_with_logging(cmd_list, step_name):
    """
    Runs a subprocess and streams every line of its output into the log file.

    Used for convert.py and send_to_omi.py invocations, where we want the
    child process's print() output to appear in the main log rather than
    being discarded. The 1MB buffer limit (vs the default 64KB) prevents
    asyncio from crashing on long verbose output from a large conversion batch.

    Returns the subprocess exit code so the caller can act on success/failure.
    """
    log(f"Starting {step_name}...")

    process = await asyncio.create_subprocess_exec(
        *cmd_list,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=1024 * 1024  # 1MB buffer instead of default 64KB
    )

    while True:
        line = await process.stdout.readline()
        if not line:
            break

        decoded_line = line.decode('utf-8').rstrip()
        if decoded_line:
            log(f"     | {decoded_line}")

    await process.wait()
    return process.returncode


# ==========================================
# ORCHESTRATOR
# ==========================================

async def sync_cycle():
    """
    Runs one complete download → convert → transcribe → upload pipeline pass.

    Returns a status string that the service loop uses to decide what to do next:
        "PROCESSED_DATA" — At least one file was fully uploaded to Omi.
        "CAUGHT_UP"      — Cycle ran cleanly but there was nothing new to upload.
        "ERROR"          — A non-recoverable error occurred (BT reset limit hit).
    """
    # Check for orphaned WAVs left over from a previous cycle — no transcript
    # was produced, so they were never uploaded. Could be a crashed transcription
    # pass, a mid-cycle engine switch, or MacWhisper not running.
    orphaned_wavs = [
        f for f in TRANSCRIPT_DIR.glob("*.wav")
        if not (f.with_suffix(".dote").exists() or f.with_suffix(".json").exists())
    ]
    if orphaned_wavs:
        log(f"[!] {len(orphaned_wavs)} WAV file(s) in wav_exports have no transcript and were never uploaded.")
        notify_alert(
            "⚠️ Unprocessed Audio Files",
            f"{len(orphaned_wavs)} WAV file(s) in wav_exports were never transcribed or uploaded. "
            "Please review and manually process or remove them."
        )

    log("Starting Sync Cycle...", separator=True)
    log(f"Transcription engine: {TRANSCRIPTION_ENGINE}")
    notify("Limitless Sync", "Starting Pendant Sync...")

    any_data_downloaded = False
    pendant_not_found = False
    bt_reset_attempts = 0
    max_bt_resets = 15  # Hard ceiling on BT radio power-cycles per cycle

    # ==========================================
    # PHASE 1: DOWNLOAD
    # ==========================================
    # This is a loop, not a single call, because the pendant may have more
    # audio than fits in one safe chunk (2,000 pages). download.py signals
    # "more data remains" with exit code 3, and we loop back immediately.
    # Exit code 0 means the pendant is fully drained. Exit code 4 means
    # the pendant is out of range and we should abort for now.
    while True:
        log("Attempting connection to Pendant...")

        cmd = [str(VENV_PYTHON), "-u", str(SCRIPTS_DIR / "download.py"), "--address", PENDANT_MAC_ADDRESS]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024  # 1MB buffer instead of default 64KB
        )

        # Read the download script's output line-by-line in real-time.
        # We selectively log lines rather than forwarding everything, because
        # download.py also writes an animated progress bar that would create
        # thousands of log entries per download.
        while True:
            line = await process.stdout.readline()
            if not line:
                break

            decoded_line = line.decode('utf-8').strip()
            if not decoded_line:
                continue

            # --- THE SILENCER ---
            # The progress bar in download.py emits lines containing "ETA",
            # "%", and "p/s" simultaneously. These are terminal UI updates,
            # not log-worthy events. Drop them.
            if "ETA" in decoded_line and "%" in decoded_line and "p/s" in decoded_line:
                continue
            # --------------------

            # Selectively format log output: errors get full-line treatment,
            # key events get the indented pipe prefix, everything else is
            # caught as a raw fallback so nothing is silently lost.
            # Suppress download.py's internal rest message — pendant_sync.py logs its own consolidated version.
            if "Giving pendant a short rest" in decoded_line:
                continue

            if "[!]" in decoded_line:
                log(decoded_line)
                if "Pendant Status: Unhealthy" in decoded_line:
                    notify_alert("⚠️ Pendant Not Recording", "Hold button 2s to stop, hold again to restart.")
            elif "[~]" in decoded_line:
                log(f"     | {decoded_line}")
            elif any(x in decoded_line for x in ["Downloaded", "Found", "Battery", "Oldest", "Newest", "First", "Skipping", "Session", "Health", "File created", "Phantom", "Aborting", "Pendant"]):
                log(f"     | {decoded_line}")
            elif any(x in decoded_line for x in ["Connected", "Downloading", "Starting"]):
                log(decoded_line)
            else:
                # Catch-all for any other unexpected errors or messages
                log(f"     | [RAW] {decoded_line}")

        try:
            await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
            returncode = process.returncode
        except asyncio.TimeoutError:
            process.kill()
            log(f"[!] Download subprocess timed out after {DOWNLOAD_TIMEOUT_SECONDS // 60} minutes. Treating as Bluetooth error.")
            notify("Limitless Sync", "Download timed out — resetting Bluetooth")
            returncode = 2  # Treat timeout as a BT hardware error → circuit breaker

        if returncode == 0:
            # EXIT 0: Full success — pendant is fully drained.
            bin_files_ready = list(DOWNLOAD_DIR.glob("*.bin"))
            if bin_files_ready:
                log("Download complete. Moving to conversion.")
            else:
                log("Nothing new to download.")
            break

        elif returncode == 4:
            # EXIT 4: Pendant not found — user is out of range.
            # Flag it so the service loop can arm the "welcome back" idle-detection.
            pendant_not_found = True
            break

        elif returncode == 3:
            # EXIT 3: Chunk limit reached (or natural gap found) — more data exists.
            # Before reconnecting: convert whatever .bin files we just downloaded,
            # then rest the pendant so the BLE radio can recover.
            any_data_downloaded = True
            log(f"More data exists. Giving pendant a short rest... ({BT_CHUNK_REST_SECONDS}s)")

            # Convert this chunk's .bin files to .wav now so MacWhisper can start
            # transcribing while the next chunk downloads. Only clean up .bin files
            # if conversion completes cleanly (exit 0).
            # Skip for omi_cloud — .bin files are kept and uploaded directly at the end.
            bin_files_chunk = list(DOWNLOAD_DIR.glob("*.bin"))
            if bin_files_chunk and TRANSCRIPTION_ENGINE != "omi_cloud":
                chunk_convert_exit = await run_step_with_logging(
                    [str(VENV_PYTHON), "-u", str(SCRIPTS_DIR / "convert.py"), str(DOWNLOAD_DIR)],
                    "WAV Conversion (mid-cycle chunk)"
                )
                if chunk_convert_exit == 0:
                    for f in bin_files_chunk:
                        f.unlink(missing_ok=True)
                    log(f"     | Cleaned up {len(bin_files_chunk)} raw .bin files.")

            # Rest the pendant before reconnecting.
            await asyncio.sleep(BT_CHUNK_REST_SECONDS)
            continue

        elif returncode == 2:
            # EXIT 2: Bluetooth hardware error — the radio stalled mid-download.
            # THE CIRCUIT BREAKER: wait 15s, then power-cycle the BT chip using
            # `blueutil` (a Homebrew utility for BT radio control). Wait for it
            # to fully reinitialize, then retry. After max_bt_resets attempts,
            # give up and mark the cycle as an error.
            log(f"Confirmed Bluetooth error (Exit Code 2). Resting radio for 15s... (Attempt {bt_reset_attempts + 1}/{max_bt_resets})")
            await asyncio.sleep(15)

            if bt_reset_attempts >= max_bt_resets:
                log("Max Bluetooth reset attempts reached. Giving up for this cycle.")
                notify("Limitless Sync", "Bluetooth error — sync failed")
                return "ERROR"

            log("Cycling Bluetooth radio to clear hardware stall...")
            r_off = subprocess.run(["/opt/homebrew/bin/blueutil", "-p", "0"], capture_output=True)  # Radio OFF
            if r_off.returncode != 0:
                log("[!] blueutil radio-off failed — BT radio may not have reset correctly.")
            await asyncio.sleep(5)
            r_on = subprocess.run(["/opt/homebrew/bin/blueutil", "-p", "1"], capture_output=True)   # Radio ON
            if r_on.returncode != 0:
                log("[!] blueutil radio-on failed — BT radio may not have restarted correctly.")
            await asyncio.sleep(10)  # Allow the radio and OS stack to fully reinitialize
            log("Bluetooth radio successfully restarted.")

            bt_reset_attempts += 1
            continue

        else:
            # Unexpected exit code — treat as an unrecoverable error.
            log(f"Download script failed with Exit Code {returncode}. Aborting cycle.")
            notify("Limitless Sync", "Script error — check log")
            return "ERROR"

    # ==========================================
    # omi_cloud ENGINE: skip convert + transcribe + send_to_omi
    # ==========================================
    # When using omi_cloud, upload the raw .bin files directly to Omi's API.
    # Omi runs Deepgram transcription and speaker ID server-side, exactly like
    # the mobile app's offline batch sync. Phases 2, 3, and 4 are bypassed.
    if TRANSCRIPTION_ENGINE == "omi_cloud":
        bin_files = list(DOWNLOAD_DIR.glob("*.bin"))
        if bin_files:
            any_data_downloaded = True
            cloud_exit = await run_step_with_logging([
                str(VENV_PYTHON), "-u",
                str(SCRIPTS_DIR / "sync_omi_cloud.py"),
                str(DOWNLOAD_DIR),
                "--firebase-key", OMI_FIREBASE_WEB_API_KEY,
            ], "Omi Cloud Sync")

            if cloud_exit == 0:
                log("Cycle complete.")
                notify("Limitless Sync", "Pendant Sync Complete")
                return "PROCESSED_DATA"
            else:
                log("[!] Omi Cloud Sync failed — check log for details.")
                notify("Limitless Sync", "Cloud sync failed — check log")
                return "ERROR"
        else:
            log("Cycle complete.")
            if pendant_not_found and not any_data_downloaded:
                return "ERROR"
            notify("Limitless Sync", "Pendant Sync — Already up to date")
            return "CAUGHT_UP"

    # ==========================================
    # PHASE 2: CONVERT
    # ==========================================
    # Check if any .bin files were produced by the download phase.
    # Pass them all to convert.py, which decodes the Opus frames into .wav files.
    # Only delete the source .bin files if conversion exited cleanly (code 0),
    # so a failed conversion doesn't silently lose raw audio data.
    bin_files = list(DOWNLOAD_DIR.glob("*.bin"))
    if bin_files:
        any_data_downloaded = True
        exit_code = await run_step_with_logging([str(VENV_PYTHON), "-u", str(SCRIPTS_DIR / "convert.py"), str(DOWNLOAD_DIR)], "WAV Conversion")

        # Safe cleanup: only remove .bin files after a confirmed successful conversion.
        if exit_code == 0:
            for f in bin_files:
                f.unlink(missing_ok=True)
            log(f"     | Cleaned up {len(bin_files)} raw .bin files.")

    # ==========================================
    # PHASE 3: TRANSCRIBE
    # ==========================================
    wav_files = list(TRANSCRIPT_DIR.glob("*.wav"))

    if wav_files:
        if TRANSCRIPTION_ENGINE == "faster-whisper":
            # Run transcribe.py directly — no polling needed.
            # It finds all .wav files without matching .json transcripts and
            # processes them synchronously before we proceed to upload.
            await run_step_with_logging(
                [str(VENV_PYTHON), "-u", str(SCRIPTS_DIR / "transcribe.py"), str(TRANSCRIPT_DIR)],
                "Transcription (faster-whisper)"
            )
        elif TRANSCRIPTION_ENGINE == "whisperx":
            # Run transcribe_whisperx.py directly — no polling needed.
            # Like faster-whisper but adds speaker diarization: each segment
            # gets a "speaker" field (SPEAKER_00, SPEAKER_01, etc.) that
            # send_to_omi.py uses to correctly attribute is_user in Omi.
            # Uses WHISPERX_PYTHON (the .venv-whisperx interpreter) because
            # WhisperX requires Python <3.14 and won't install in the main venv.
            await run_step_with_logging(
                [str(WHISPERX_PYTHON), "-u", str(SCRIPTS_DIR / "transcribe_whisperx.py"), str(TRANSCRIPT_DIR)],
                "Transcription (whisperx)"
            )
        else:
            # MacWhisper path: watch-folder polling.
            # MacWhisper runs as a separate application monitoring wav_exports/.
            # When it detects a new .wav it transcribes it and saves a .dote file.
            # We poll every 30 seconds until all .wav files have a matching transcript
            # (.dote or .json), or until the 30-minute timeout is reached.
            #
            # Upload as soon as any transcript is ready — no need to batch since
            # merging was removed. send_to_omi.py archives processed files, so
            # repeated calls are safe no-ops when nothing new has arrived.
            def count_ready_transcripts():
                return len(list(TRANSCRIPT_DIR.glob("*.dote"))) + len(list(TRANSCRIPT_DIR.glob("*.json")))

            expected_transcripts = len(wav_files)
            waiting_for_transcripts = True
            wait_time = 0
            max_wait = 30 * 60  # 30 minutes maximum wait
            i = 0

            while waiting_for_transcripts and wait_time < max_wait:
                current_transcripts = count_ready_transcripts()
                # Recalculate in case a background upload pass moved some files.
                expected_transcripts = len(list(TRANSCRIPT_DIR.glob("*.wav")))

                if current_transcripts >= expected_transcripts and expected_transcripts > 0:
                    log(f"All {expected_transcripts} transcripts are ready.")
                    waiting_for_transcripts = False
                elif expected_transcripts == 0:
                    log("All transcripts have been processed by the background uploader.")
                    waiting_for_transcripts = False
                else:
                    if i % 10 == 0:
                        log(f"Waiting for {expected_transcripts} transcripts... ({current_transcripts}/{expected_transcripts})")

                    # Upload any ready transcripts on every tick.
                    if waiting_for_transcripts and current_transcripts > 0:
                        log(f"     | {current_transcripts} transcript(s) ready. Uploading...")
                        await run_step_with_logging([
                            str(VENV_PYTHON),
                            str(SCRIPTS_DIR / "send_to_omi.py"),
                            str(TRANSCRIPT_DIR),
                            "--key", OMI_KEY
                        ], "Background Uploads")

                    await asyncio.sleep(30)
                    wait_time += 30
                    i += 1

            if wait_time >= max_wait:
                log("Timed out waiting for MacWhisper to finish. Will process what is ready.")

    # ==========================================
    # PHASE 4: OMI IMPORT
    # ==========================================
    # Process all ready transcripts (.dote, .json, .srt). send_to_omi.py handles
    # quality filtering, speaker merging, API upload, and file archival.
    transcript_files = (
        list(TRANSCRIPT_DIR.glob("*.dote")) +
        list(TRANSCRIPT_DIR.glob("*.json")) +
        list(TRANSCRIPT_DIR.glob("*.srt"))
    )

    if transcript_files:
        await run_step_with_logging([
            str(VENV_PYTHON),
            "-u",
            str(SCRIPTS_DIR / "send_to_omi.py"),
            str(TRANSCRIPT_DIR),
            "--key", OMI_KEY
        ], "Omi Upload")

        log("Cycle complete.")
        notify("Limitless Sync", "Pendant Sync Complete")
        return "PROCESSED_DATA"

    log("Cycle complete.")
    # If the pendant was out of range and nothing was downloaded, return ERROR so
    # the service loop arms the "welcome back" idle-detection. This ensures an
    # early sync fires the moment you return to your desk after being away.
    if pendant_not_found and not any_data_downloaded:
        return "ERROR"
    notify("Limitless Sync", "Pendant Sync — Already up to date")
    return "CAUGHT_UP"


# ==========================================
# SERVICE LOOP
# ==========================================

async def main():
    """
    The top-level service loop. Runs indefinitely, triggering sync_cycle()
    on a schedule and handling the "welcome back" early-sync logic.

    NORMAL SCHEDULE:
        Runs sync_cycle() once per hour (CHECK_INTERVAL_SECONDS = 3600).
        After each cycle, sleeps and waits for the next scheduled time.

    EARLY TRIGGER — "WELCOME BACK" LOGIC:
        The pendant maintains a BLE connection to the Mac while you're seated.
        When you walk away for more than 5 minutes (idle > 300s), the Mac's
        BLE stack eventually drops the connection and the pendant stops syncing.
        When you return and move the mouse/keyboard (idle < 60s), we detect
        this transition and trigger an immediate catch-up sync — without making
        you wait up to an hour for the next scheduled run.

        This is tracked with two flags:
        - pendant_missed: True when the last cycle returned "ERROR" (pendant
          not found or BT failure), indicating the pendant was out of range.
        - user_was_away: True when idle time exceeded 5 minutes during an
          error state, confirming the user stepped away.

        An early trigger fires when BOTH flags are true AND idle drops back
        below 1 minute (user has returned).
    """
    log("Automation Service Started", separator=True)

    # Start the timer as if we're already overdue, so the first cycle
    # runs immediately on startup rather than waiting a full hour.
    last_check_time = datetime.now() - timedelta(seconds=CHECK_INTERVAL_SECONDS)
    pendant_missed = False
    user_was_away = False

    while True:
        now = datetime.now()
        time_since_last = (now - last_check_time).total_seconds()

        early_trigger = False

        # Check the "welcome back" condition only when the pendant was previously
        # unreachable (pendant_missed), so we don't fire early triggers during
        # normal idle time between scheduled syncs.
        if pendant_missed:
            idle_time = get_mac_idle_seconds()

            if idle_time > IDLE_AWAY_SECONDS:
                # User has been away long enough — set the "was away" flag.
                user_was_away = True

            elif user_was_away and idle_time < IDLE_BACK_SECONDS:
                # User was away and has just returned (idle < 1 minute).
                # Trigger an early sync immediately.
                log("Activity detected after being away. Triggering early sync...")
                early_trigger = True
                user_was_away = False

        # Run a sync cycle if the scheduled interval has elapsed, or if an
        # early trigger fired from the idle-detection logic above.
        if time_since_last >= CHECK_INTERVAL_SECONDS or early_trigger:
            status = await sync_cycle()

            last_check_time = datetime.now()

            if status == "PROCESSED_DATA" or status == "CAUGHT_UP":
                next_check = last_check_time + timedelta(seconds=CHECK_INTERVAL_SECONDS)
                log(f"Sleeping. Next scheduled check at {next_check.strftime('%I:%M %p')}")
                pendant_missed = False
                user_was_away = False

            elif status == "ERROR":
                # The cycle failed (BT reset limit hit or pendant not found).
                # Set pendant_missed so the idle-detection logic activates,
                # enabling an early sync when the user returns to their desk.
                log("Will check again in 1 hour, or sooner if you return to the computer.")
                pendant_missed = True
                user_was_away = False

        # Sleep 30 seconds between loop iterations. This is the polling granularity
        # for both the scheduled interval check and the idle-detection logic.
        await asyncio.sleep(30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log(f"Service crashed: {e}\n{traceback.format_exc()}")
