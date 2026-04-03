"""
Omi Transcript Uploader  (send_to_omi.py)
-----------------------------------------
Role in the pipeline: PHASE 4 of 4

This script is called by pendant_sync.py after transcription has finished.
It reads transcript files, runs them through a multi-stage quality filter,
and uploads the survivors to your Omi timeline via the Omi Developer API.

SUPPORTED TRANSCRIPT FORMATS:
    .dote  — MacWhisper JSON (default, with speaker diarization)
    .json  — Standard Whisper JSON (from faster-whisper, whisper.cpp, etc.)
    .srt   — SubRip subtitle format (no speaker information)

INPUT:  A directory of transcript files paired with matching `.wav` audio files.

OUTPUT: Successfully uploaded transcripts are archived in `synced_to_omi/`.
        Rejected transcripts are handled according to DISCARD_ACTION policy.
        The original transcript file is always deleted after processing.

UPLOAD ENDPOINT:
    POST https://api.omi.me/v1/dev/user/conversations/from-segments
    See: https://docs.omi.me/api-reference/introduction

QUALITY FILTER PIPELINE (applied before any upload):
    Phase 1 — Text Check:       Skip completely empty transcripts.
    Phase 2 — Hallucination Check:
        2a. 1-Word Kill Switch:  Skip anything with 1 or fewer total words.
        2b. Standard Checks:     Skip transcripts that contain ONLY:
                                 - Action tags  (*like this*)
                                 - Ghost phrases (filler text Whisper hallucinates)
                                 - Isolated short utterances (likely microphone noise)
                                 - Glitch segments (too short, too few words)
    Phase 3 — Speaker Merge:   Merge consecutive segments from the same speaker
                                 into single blocks for a clean Omi timeline UI.
    Phase 4 — Omi Upload:       POST the cleaned payload. Archive on success.

FILE RETENTION POLICIES (configured via .env):
    DISCARD_ACTION            — "keep" or "delete" for rejected files
    DISCARD_RETENTION_DAYS    — Days before discarded_audio auto-cleans (0 = forever)
    SYNCED_WAV_ACTION         — "keep" or "delete" for WAV files after upload
    SYNCED_WAV_RETENTION_DAYS — Days before synced WAV files auto-clean (0 = forever)
    SYNCED_JSON_RETENTION_DAYS — Days before synced JSON archives auto-clean (0 = forever)
"""

import argparse
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv

# Load environment variables from the project root .env file.
# This file sits two levels up from this script: /scripts/../.env
ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)


# ==========================================
# RETENTION CLEANUP FUNCTIONS
# ==========================================

def discard_files(dote_path: Path, wav_path: Path, discard_dir: Path, action: str):
    """
    Applies the DISCARD_ACTION policy to a rejected transcript and its matching WAV.

    action="delete" — immediately deletes both files (no folder entry).
    action="keep"   — moves both files to discard_dir for later review/cleanup.

    Centralizes the repeated move-or-delete pattern so it only lives in one place.
    """
    if action == "delete":
        dote_path.unlink(missing_ok=True)
        if wav_path.exists():
            wav_path.unlink(missing_ok=True)
    else:
        try:
            dote_path.rename(discard_dir / dote_path.name)
        except Exception as e:
            print(f"  [!] Could not move {dote_path.name} to discard folder: {e}", flush=True)
        if wav_path.exists():
            try:
                wav_path.rename(discard_dir / wav_path.name)
            except Exception as e:
                print(f"  [!] Could not move {wav_path.name} to discard folder: {e}", flush=True)


def clean_old_discards(discard_dir: Path, days=7):
    """
    Deletes files in the discarded_audio folder that are older than `days` days.

    This runs at the start of every sync cycle (when DISCARD_ACTION=keep) to
    prevent indefinite accumulation of rejected audio. Files are identified by
    their filesystem modification time (mtime).
    """
    now = time.time()
    cutoff = now - (days * 86400)
    for filepath in discard_dir.glob("*"):
        if filepath.name.startswith("."):
            continue  # Skip macOS metadata files (.DS_Store etc.)
        if filepath.is_file() and filepath.stat().st_mtime < cutoff:
            try:
                filepath.unlink()
                print(f"Auto-deleted old discarded file: {filepath.name}")
            except Exception as e:
                print(f"  [!] Could not delete {filepath.name}: {e}", flush=True)

def clean_old_synced(synced_dir: Path, json_days=0, wav_days=0):
    """
    Deletes aged-out files in the synced_to_omi folder.

    JSON and WAV files are managed on independent retention schedules,
    because you may want to keep the JSON payload archives (proof of upload)
    longer than — or instead of — the raw WAV audio.

    A days value of 0 means "keep forever" — that file type is never auto-deleted.

    Called once per sync cycle, immediately after clean_old_discards().
    """
    now = time.time()
    for filepath in synced_dir.glob("*"):
        if not filepath.is_file(): continue
        age = now - filepath.stat().st_mtime
        if filepath.suffix == ".json" and json_days > 0 and age > json_days * 86400:
            try:
                filepath.unlink()
                print(f"Auto-deleted old synced JSON: {filepath.name}")
            except Exception as e:
                print(f"  [!] Could not delete {filepath.name}: {e}", flush=True)
        elif filepath.suffix == ".wav" and wav_days > 0 and age > wav_days * 86400:
            try:
                filepath.unlink()
                print(f"Auto-deleted old synced WAV: {filepath.name}")
            except Exception as e:
                print(f"  [!] Could not delete {filepath.name}: {e}", flush=True)


# ==========================================
# HELPER FUNCTIONS
# ==========================================

def get_file_datetime(filepath: Path):
    """
    Parses the conversation start time from a filename.

    download.py names files with a human-readable timestamp prefix:
        "04-01-2026 02.30PM to 02.45PM.bin"
    After convert.py and transcription, the transcript retains the same base name.

    This timestamp is used both for chronological sort order (so conversations
    are uploaded to Omi in the correct sequence) and as the `started_at` field
    in the Omi API payload.

    Returns datetime.min on parse failure so the file sorts to the front
    rather than crashing the upload run.
    """
    try:
        name = filepath.name
        for ext in (".dote", ".json", ".srt"):
            name = name.replace(ext, "")
        name = name.replace("nudged_", "").replace("merged_", "")
        start_str = name.split(" to ")[0]
        return datetime.strptime(start_str, "%m-%d-%Y %I.%M%p")
    except Exception: return datetime.min

def time_to_seconds(time_str: str) -> float:
    """
    Converts a MacWhisper timestamp string into a raw float of seconds.

    MacWhisper `.dote` files use the format "HH:MM:SS,mmm" (SRT-style) or
    "HH:MM:SS.mmm" (dot-separated). This function handles both variants.
    These timestamps are relative to the start of the audio file, not wall-clock time.

    Used when building the Omi API payload's `start` and `end` fields for
    each transcript segment.
    """
    try:
        h, m, s_ms = time_str.split(':')
        s, ms = s_ms.split(',') if ',' in s_ms else (s_ms.split('.') if '.' in s_ms else (s_ms, 0))
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
    except Exception: return 0.0

def extract_segments(data):
    """
    Finds the transcript segment array inside a .dote JSON file.

    MacWhisper's .dote format is JSON, but the top-level structure can vary
    depending on the version. The transcript array may be at the root level
    or nested under a key like "lines", "segments", or "transcription".
    This function handles all known variants gracefully.
    """
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for key in ["lines", "segments", "transcription"]:
            if key in data and isinstance(data[key], list): return data[key]
    return []

def get_matching_wav(transcript_path: Path):
    """
    Returns the Path to the WAV file that corresponds to a given transcript file.

    Both files live in the same directory with the same base name. Handles all
    supported transcript extensions (.dote, .json, .srt) as well as the
    `.wav.merged` variant produced when pendant_sync.py stitches audio files.

    Falls back to the plain `.wav` path if no file is found — the caller
    checks `.exists()` before doing anything with the returned path.
    """
    base_name = transcript_path.name
    for ext in (".dote", ".json", ".srt"):
        base_name = base_name.replace(ext, "")
    for wav_ext in [".wav", ".wav.merged"]:
        wav_path = transcript_path.parent / (base_name + wav_ext)
        if wav_path.exists(): return wav_path
    return transcript_path.parent / (base_name + ".wav")


def parse_srt(filepath: Path) -> list:
    """
    Parses an .srt subtitle file into the same segment list shape used by .dote/.json.

    SRT format is a sequence of numbered cue blocks:
        1
        00:00:01,000 --> 00:00:03,500
        Transcribed text here.

    Each block becomes a dict with `text`, `startTime` (float seconds), and
    `endTime` (float seconds). SRT has no speaker diarization — all segments
    will fall through to the SPEAKER_00 default in the upload logic.

    Multi-line cue text is joined with a space.
    """
    segments = []
    content = filepath.read_text(encoding='utf-8', errors='replace')
    # Each cue: optional blank lines, cue number, timestamp line, one or more text lines
    pattern = re.compile(
        r'^\d+\s*$\n'                                        # cue number on its own line
        r'^(\d{2}:\d{2}:\d{2}[,\.]\d{3})'                  # start timestamp
        r'\s*-->\s*'
        r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*$\n'             # end timestamp
        r'([\s\S]*?)(?=\n\d+\s*\n|\Z)',                     # text until next cue or EOF
        re.MULTILINE
    )
    for match in pattern.finditer(content):
        start_str = match.group(1).replace('.', ',')         # normalise dot → comma
        end_str   = match.group(2).replace('.', ',')
        text      = ' '.join(match.group(3).strip().splitlines())
        if not text:
            continue
        segments.append({
            "text":      text,
            "startTime": time_to_seconds(start_str),
            "endTime":   time_to_seconds(end_str),
        })
    return segments


# ==========================================
# MAIN UPLOAD FUNCTION
# ==========================================

def upload_transcripts(input_dir: str, api_key: str):
    """
    Main entry point. Processes all .dote files in `input_dir`.

    For each file:
      1. Runs the quality filter (empty check, hallucination check).
      2. If rejected, applies the DISCARD_ACTION policy (delete or move to folder).
      3. If valid, merges consecutive same-speaker segments and uploads to Omi.
      4. On success, saves a JSON archive and applies the SYNCED_WAV_ACTION policy.
      5. On failure, leaves the .dote in place so it can be retried next cycle.
    """
    folder = Path(input_dir).expanduser()
    base_dir = folder.parent.parent
    discard_dir = base_dir / "discarded_audio"
    synced_dir = base_dir / "synced_to_omi"

    # --- READ RETENTION POLICY FROM .env ---
    # These settings let you tune file lifecycle without touching code.
    # See .env.example for full documentation of each variable.
    discard_action       = os.getenv("DISCARD_ACTION", "keep").lower()
    discard_days         = int(os.getenv("DISCARD_RETENTION_DAYS", "7"))
    synced_wav_action    = os.getenv("SYNCED_WAV_ACTION", "keep").lower()
    synced_wav_days      = int(os.getenv("SYNCED_WAV_RETENTION_DAYS", "0"))
    synced_json_days     = int(os.getenv("SYNCED_JSON_RETENTION_DAYS", "0"))

    # USER_SPEAKER_LABEL: if set (e.g. "Speaker 1"), only segments from that
    # speaker get is_user: true. All others get false. This lets Omi correctly
    # distinguish your voice from other people captured by the pendant.
    # Leave empty to mark all segments as is_user: true (default behaviour).
    # Note: this relies on consistent speaker labelling from your transcription
    # tool. MacWhisper diarizes but does not identify specific voices.
    user_speaker_label   = os.getenv("USER_SPEAKER_LABEL", "").strip()

    # Ensure the archive directories exist (first run creates them).
    for d in [discard_dir, synced_dir]: d.mkdir(parents=True, exist_ok=True)

    # Run housekeeping at the start of each upload pass.
    # Only run discard cleanup when we're keeping files (not deleting immediately).
    if discard_action == "keep":
        clean_old_discards(discard_dir, days=discard_days)
    clean_old_synced(synced_dir, json_days=synced_json_days, wav_days=synced_wav_days)

    # Process files in chronological order so Omi receives conversations
    # in the order they actually happened. Accepts .dote (MacWhisper), .json
    # (standard Whisper output), and .srt (SubRip subtitles).
    transcript_files = sorted(
        list(folder.glob("*.dote")) + list(folder.glob("*.json")) + list(folder.glob("*.srt")),
        key=get_file_datetime
    )

    for transcript_file in transcript_files:
        wav_file = get_matching_wav(transcript_file)

        # Parse the transcript based on its format.
        if transcript_file.suffix == ".srt":
            raw_segments = parse_srt(transcript_file)
        else:
            # Both .dote and .json are JSON-based formats.
            try:
                with open(transcript_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                print(f"  [!] Could not read {transcript_file.name}: {e} — skipping", flush=True)
                continue
            raw_segments = extract_segments(data)

        # ==========================================
        # PHASE 1: BOUNCER — TEXT CHECK
        # ==========================================
        # Completely empty transcripts (no text in any segment) are useless.
        # This catches files where MacWhisper ran but produced no output —
        # often from very short or nearly silent audio clips.
        if not any(seg.get("text", "").strip() for seg in raw_segments):
            print(f"Skipped: {transcript_file.name} is empty.")
            discard_files(transcript_file, wav_file, discard_dir, discard_action)
            continue

        # ==========================================
        # PHASE 2: BOUNCER — HALLUCINATION CHECK
        # ==========================================
        # Whisper-family models are well known for "hallucinating" text onto
        # near-silent audio. The filters below catch the most common patterns.

        # --- 2a. The Blanket 1-Word Kill Switch ---
        # A transcript with a single word is almost certainly a hallucination.
        # Real conversations have more content. This is the fastest, broadest check.
        total_word_count = sum(len(seg.get("text", "").split()) for seg in raw_segments)

        if total_word_count <= 1:
            print(f"Skipped: {transcript_file.name} contains only one word.")
            discard_files(transcript_file, wav_file, discard_dir, discard_action)
            continue

        # --- 2b. Standard Segment-Level Checks ---
        # These checks evaluate each segment individually. A file is only
        # rejected if it has NO segments that pass all four sub-filters.
        # If even one valid segment exists, the whole file is considered real speech.
        has_valid_speech = False
        last_end = 0.0

        for seg in raw_segments:
            text = seg.get("text", "").strip()
            if not text: continue

            # Accept both MacWhisper-style keys (startTime/endTime) and standard
            # Whisper JSON keys (start/end). String timestamps go through time_to_seconds;
            # floats are used directly.
            start_v = seg.get("startTime") if seg.get("startTime") is not None else seg.get("start", 0.0)
            end_v   = seg.get("endTime")   if seg.get("endTime")   is not None else seg.get("end",   0.0)
            start = time_to_seconds(start_v) if isinstance(start_v, str) else float(start_v or 0.0)
            end   = time_to_seconds(end_v)   if isinstance(end_v,   str) else float(end_v   or 0.0)

            words = text.split()
            cleaned = "".join(c for c in text.lower() if c.isalnum() or c.isspace()).strip()

            # Common phrases Whisper hallucinates onto silence or ambient noise.
            # These are YouTube/podcast filler phrases that almost never appear
            # in real ambient recordings from a wearable pendant.
            ghosts = ["thank you", "i cant wait", "subscribe", "thanks for watching", "bye", "okay", "go ahead", "do that"]

            # Sub-filter 1 — Action Tags: Text wrapped in *asterisks* is a Whisper
            # convention for non-speech sounds like *music* or *applause*. Not speech.
            is_action_tag = text.startswith("*") and text.endswith("*")

            # Sub-filter 2 — Ghost Phrases: Short segments that exactly match known
            # hallucination phrases are discarded. The word-count guard (<=6) ensures
            # we don't accidentally discard a real sentence that contains one of these
            # phrases embedded in the middle of real speech.
            is_ghost = not cleaned or (len(words) <= 6 and any(g in cleaned for g in ghosts))

            # Sub-filter 3 — Isolated Utterances: A very short phrase preceded by a
            # long silence is almost certainly ambient noise that triggered the VAD
            # (Voice Activity Detector), not actual speech directed at Omi. The
            # thresholds (20s gap / <=8 words, or 30s gap / <=12 words) were tuned
            # empirically against real pendant recordings.
            is_isolated = ((start - last_end) > 20.0 and len(words) <= 8) or ((start - last_end) > 30.0 and len(words) <= 12)

            # Sub-filter 4 — Audio Glitch: A segment lasting 0.1 seconds or less
            # with 3 or fewer words is physically impossible as real human speech.
            # It's a frame boundary artifact or a very short audio glitch.
            is_glitch = (end - start) <= 0.1 and len(words) <= 3

            if not (is_ghost or is_isolated or is_action_tag or is_glitch):
                has_valid_speech = True
                last_end = max(last_end, end)

        if not has_valid_speech:
            print(f"Skipped: {transcript_file.name} contains only hallucinations.")
            discard_files(transcript_file, wav_file, discard_dir, discard_action)
            continue

        # ==========================================
        # PHASE 3: SPEAKER MERGE
        # ==========================================
        # MacWhisper produces one segment per sentence or phrase. The Omi timeline
        # UI renders each segment as a separate text block. To avoid a wall of
        # tiny fragments, we merge consecutive segments from the same speaker
        # into a single block by concatenating their text and updating the end time.
        #
        # The `start` and `end` times in the final payload are relative seconds
        # from the beginning of the audio file, which Omi uses to anchor the
        # conversation in time when combined with the `started_at` timestamp.
        compressed = []
        for seg in raw_segments:
            text = seg.get("text", "").strip()
            if not text: continue

            # Accept both MacWhisper speaker key and generic diarization labels.
            speaker = (seg.get("speakerDesignation") or seg.get("speaker") or "").strip() or "SPEAKER_00"
            start_v = seg.get("startTime") if seg.get("startTime") is not None else seg.get("start", 0.0)
            end_v   = seg.get("endTime")   if seg.get("endTime")   is not None else seg.get("end",   0.0)
            start = time_to_seconds(start_v) if isinstance(start_v, str) else float(start_v or 0.0)
            end   = time_to_seconds(end_v)   if isinstance(end_v,   str) else float(end_v   or 0.0)

            # is_user: if USER_SPEAKER_LABEL is set, only that speaker is the user.
            # If not set, all segments are marked as the user (backwards-compatible default).
            is_user = (not user_speaker_label) or (speaker.lower() == user_speaker_label.lower())

            if compressed and compressed[-1]["speaker"] == speaker:
                # Same speaker as the previous segment — append text and extend end time.
                compressed[-1]["text"] += " " + text
                compressed[-1]["end"] = end
            else:
                # New speaker (or first segment) — start a new block.
                compressed.append({
                    "text": text,
                    "speaker": speaker,
                    "is_user": is_user,
                    "start": start,
                    "end": end
                })

        # ==========================================
        # PHASE 4: OMI UPLOAD
        # ==========================================
        # POST to the Omi "conversations from segments" endpoint.
        # The payload uses the conversation start time (parsed from the filename)
        # as both `started_at` and `created_at`, localized to the user's timezone.
        # See: https://docs.omi.me/api-reference/introduction
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        local_tz = datetime.now().astimezone().tzinfo

        print(f"Uploading {transcript_file.name} ({len(compressed)} compressed segments)...")
        payload = {"transcript_segments": compressed, "source": "manual_macwhisper_import", "language": "en"}

        try:
            dt_naive = get_file_datetime(transcript_file)
            iso_time = dt_naive.replace(tzinfo=local_tz).isoformat()
            payload["started_at"] = iso_time
            payload["created_at"] = iso_time
            # finished_at: started_at + the end time of the last transcript segment.
            # This tells Omi the exact conversation duration rather than leaving it unknown.
            if compressed:
                last_end_s = max(seg["end"] for seg in compressed)
                payload["finished_at"] = (dt_naive + timedelta(seconds=last_end_s)).replace(tzinfo=local_tz).isoformat()
        except Exception as e:
            print(f"  [!] Could not parse timestamp for {transcript_file.name}: {e} — uploading without started_at", flush=True)

        try:
            response = requests.post("https://api.omi.me/v1/dev/user/conversations/from-segments", headers=headers, json=payload)
        except requests.exceptions.ConnectTimeout:
            print(f"  [!] Network timeout uploading {transcript_file.name} — no network access? Will retry next cycle.", flush=True)
            continue
        except requests.exceptions.ConnectionError:
            print(f"  [!] Network error uploading {transcript_file.name} — no network access? Will retry next cycle.", flush=True)
            continue

        if response.status_code in (200, 201):
            # --- CHECK FOR APPLICATION-LEVEL ERRORS IN THE RESPONSE BODY ---
            # The Omi API occasionally returns 200 with an error field in the JSON body.
            # Treat this as a failure so the .dote is left in place for retry.
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("error"):
                    print(f"  [!] Omi API returned an error for {transcript_file.name}: {body['error']} — will retry next cycle.", flush=True)
                    continue
            except Exception:
                pass  # Non-JSON body on 200/201 is fine — proceed to archive

            # --- SUCCESS: Archive the results ---

            # 1. Save the exact payload we sent to Omi as a JSON file.
            #    This is the permanent record: if you ever need to audit what
            #    was uploaded, or replay a conversation, this is the source of truth.
            #    Always use .json as the archive extension regardless of source format.
            base_stem = transcript_file.name
            for ext in (".dote", ".json", ".srt"):
                base_stem = base_stem.replace(ext, "")
            json_path = synced_dir / (base_stem + ".json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)

            # 2. Delete the original transcript file. It has been fully processed and
            #    its content is preserved in the .json archive above.
            transcript_file.unlink(missing_ok=True)

            # 3. Handle the matching WAV file according to SYNCED_WAV_ACTION policy.
            #    "keep" moves it to synced_to_omi/ for local retention.
            #    "delete" removes it immediately after upload — useful once you're
            #    confident the pipeline is working and don't need the raw audio.
            if wav_file.exists():
                if synced_wav_action == "delete":
                    wav_file.unlink()
                else:
                    wav_file.rename(synced_dir / wav_file.name)

            print(f"Success: Uploaded and archived locally as {json_path.name}.")
        else:
            # Upload failed — leave the .dote in place so the next sync cycle
            # will attempt the upload again automatically.
            print(f"Failed: {transcript_file.name} - {response.status_code} - {response.text[:200]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir")
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    upload_transcripts(args.input_dir, args.key)
