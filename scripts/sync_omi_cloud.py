#!/usr/bin/env python3
"""
sync_omi_cloud.py — Upload raw .bin files to Omi's cloud sync endpoint.
------------------------------------------------------------------------
Role in the pipeline: PHASE 3/4 REPLACEMENT (when TRANSCRIPTION_ENGINE=omi_cloud)

Instead of local transcription, this script sends the Opus .bin files produced
by download.py directly to Omi's /v2/sync-local-files API endpoint, which runs
Deepgram Nova-3 transcription + speaker identification server-side — exactly like
the Omi mobile app does when syncing offline audio from the pendant.

Omi's server-side pipeline (per their open-source backend):
    1. Decode Opus frames → PCM
    2. VAD segmentation (120s gap merging)
    3. Deepgram Nova-3 transcription (your account language/vocabulary settings)
    4. Speaker identification (biometric matching against your speech profiles)
    5. Memory/conversation creation

Auth:
    The /v2/sync-local-files endpoint requires a Firebase ID token, not the
    OMI_API_KEY used by the developer endpoints. Since Omi uses Google/Apple
    OAuth (not email/password), the token must be obtained manually once from
    a browser session, then the refresh token is cached for long-term use.

    One-time setup:
      1. Open app.omi.me in Chrome and sign in with Google/Apple
      2. DevTools → Network → any request → copy "Authorization: Bearer eyJ..."
         Set OMI_FIREBASE_TOKEN=eyJ... in .env
      3. For long-term auto-refresh (recommended): DevTools → Application →
         IndexedDB → firebaseLocalStorageDb → firebaseLocalStorage → your user
         entry → stsTokenManager.refreshToken
         Set OMI_FIREBASE_REFRESH_TOKEN=AMf... in .env
      Once a valid refresh token is cached, the script auto-renews indefinitely.

Usage:
    python sync_omi_cloud.py <bin_dir>

    All credentials are read from .env / environment variables.

Exit codes:
    0 — All segments synced successfully (or partial_failure with at least some success)
    1 — Fatal error (auth failure, upload rejected, all segments failed, timeout)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# ==========================================
# CONSTANTS
# ==========================================

# Public Firebase Web API Key embedded in the Omi mobile app.
# This is not a secret — it's shipped in the open-source app and identifies
# the Firebase project, not any individual user's account.
DEFAULT_FIREBASE_WEB_API_KEY = "***REDACTED***"

FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={key}"
)
FIREBASE_REFRESH_URL = (
    "https://securetoken.googleapis.com/v1/token?key={key}"
)

OMI_SYNC_V1_URL = "https://api.omi.me/v1/sync-local-files"
OMI_SYNC_V2_URL = "https://api.omi.me/v2/sync-local-files"
OMI_JOB_URL     = "https://api.omi.me/v2/sync-local-files/{job_id}"

# Firebase ID tokens expire after 1 hour; subtract a 60-second buffer.
TOKEN_EXPIRY_BUFFER_SECONDS = 60

# Polling settings for the async v2 job.
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS  = 600  # 10 minutes


# ==========================================
# FIREBASE AUTHENTICATION
# ==========================================

def _token_cache_path(bin_dir: Path) -> Path:
    """Cache file lives at the project root (parent of bin_dir's parent)."""
    # bin_dir is typically limitless_data/downloads/
    # project root is two levels up
    return bin_dir.parent.parent / ".firebase_token_cache.json"


def _save_token_cache(cache_path: Path, id_token: str, refresh_token: str, expires_at: float):
    cache_path.write_text(json.dumps({
        "idToken":      id_token,
        "refreshToken": refresh_token,
        "expires_at":   expires_at,
    }))


def _refresh_id_token(refresh_token: str, firebase_key: str) -> tuple[str, str, float]:
    """
    Exchange a Firebase refresh token for a fresh ID token.
    Returns (id_token, new_refresh_token, expires_at).
    Exits with code 1 on failure.
    """
    resp = requests.post(
        FIREBASE_REFRESH_URL.format(key=firebase_key),
        json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    if resp.status_code != 200:
        sys.exit(
            f"ERROR: Firebase token refresh failed ({resp.status_code}): {resp.text}\n"
            "Your refresh token may have expired. Grab a new one from the browser:\n"
            "  DevTools → Application → IndexedDB → firebaseLocalStorageDb\n"
            "  → firebaseLocalStorage → stsTokenManager.refreshToken\n"
            "Update OMI_FIREBASE_REFRESH_TOKEN in .env."
        )
    data = resp.json()
    new_id_token      = data["id_token"]
    new_refresh_token = data["refresh_token"]
    expires_at        = time.time() + int(data.get("expires_in", 3600))
    return new_id_token, new_refresh_token, expires_at


def get_firebase_token(bin_dir: Path, firebase_key: str,
                       direct_token: str = "", direct_refresh_token: str = "") -> str:
    """
    Returns a valid Firebase ID token. Resolution order:

      1. Cache file (.firebase_token_cache.json) — if token still valid, use it.
      2. Cache file — if expired but refresh token present, silently refresh.
      3. OMI_FIREBASE_REFRESH_TOKEN env var — refresh and cache.
      4. OMI_FIREBASE_TOKEN env var — use directly (warns that it expires in ~1hr,
         and seeds the cache so future runs can try a refresh if also given refresh token).

    On first run with only OMI_FIREBASE_TOKEN set, the token works for up to 1 hour.
    Set OMI_FIREBASE_REFRESH_TOKEN (from browser IndexedDB) for long-term auto-renewal.
    """
    cache_path = _token_cache_path(bin_dir)
    now = time.time()

    # --- 1 & 2: Try the local cache ---
    if cache_path.exists():
        try:
            cache         = json.loads(cache_path.read_text())
            expires_at    = float(cache.get("expires_at", 0))
            id_token      = cache.get("idToken", "")
            refresh_token = cache.get("refreshToken", "")

            if id_token and expires_at - TOKEN_EXPIRY_BUFFER_SECONDS > now:
                print("Using cached Firebase token.")
                return id_token

            if refresh_token:
                print("Cached Firebase token expired — refreshing...")
                new_id, new_refresh, new_exp = _refresh_id_token(refresh_token, firebase_key)
                _save_token_cache(cache_path, new_id, new_refresh, new_exp)
                print("Firebase token refreshed and cached.")
                return new_id

        except Exception as e:
            print(f"[!] Could not read token cache ({e}) — trying env vars.")

    # --- 3: OMI_FIREBASE_REFRESH_TOKEN env var (no cache yet, but refresh token known) ---
    if direct_refresh_token:
        print("Refreshing Firebase token from OMI_FIREBASE_REFRESH_TOKEN...")
        new_id, new_refresh, new_exp = _refresh_id_token(direct_refresh_token, firebase_key)
        _save_token_cache(cache_path, new_id, new_refresh, new_exp)
        print("Firebase token obtained and cached.")
        return new_id

    # --- 4: OMI_FIREBASE_TOKEN env var — use directly ---
    if direct_token:
        print("Using OMI_FIREBASE_TOKEN directly (valid for ~1 hour).")
        print("[!] Tip: also set OMI_FIREBASE_REFRESH_TOKEN for automatic long-term renewal.")
        # Seed the cache with no refresh token so future runs check here first.
        # Assume the token was just freshly grabbed; it expires in up to 3600s.
        _save_token_cache(cache_path, direct_token, "", now + 3600)
        return direct_token

    sys.exit(
        "ERROR: No Firebase credentials found.\n"
        "Set one of the following in .env:\n"
        "  OMI_FIREBASE_TOKEN=eyJ...        (Bearer token from browser, expires ~1hr)\n"
        "  OMI_FIREBASE_REFRESH_TOKEN=AMf...  (from browser IndexedDB, auto-renews)\n"
        "\n"
        "One-time setup:\n"
        "  1. Open app.omi.me in Chrome, sign in with Google/Apple\n"
        "  2. DevTools → Network → any request → Authorization: Bearer eyJ...\n"
        "     → set OMI_FIREBASE_TOKEN\n"
        "  3. DevTools → Application → IndexedDB → firebaseLocalStorageDb\n"
        "     → firebaseLocalStorage → stsTokenManager.refreshToken\n"
        "     → set OMI_FIREBASE_REFRESH_TOKEN"
    )


# ==========================================
# FILENAME MAPPING
# ==========================================

def make_upload_name(bin_path: Path) -> str:
    """
    Maps a human-readable .bin filename to the format expected by Omi's API.

    download.py names files like:  "04-01-2026 02.30PM to 02.45PM.bin"
    Omi's API expects a name like: "recording_1743530400.bin"
                                    (any name with an underscore + Unix timestamp)

    Parsing strategy:
      1. Extract the start-time portion before " to " using the known strftime
         format "%m-%d-%Y %I.%M%p" (e.g. "04-01-2026 02.30PM").
      2. Convert to a local Unix timestamp.
      3. Fall back to the file's mtime if parsing fails (e.g. UNKNOWN_TIME files).
    """
    stem = bin_path.stem  # filename without .bin extension
    try:
        # Split on " to " — the start time is everything before it.
        start_part = stem.split(" to ")[0]
        # strptime with %p handles both "PM" and "AM" (case-sensitive on some
        # platforms; upper() normalizes it).
        dt = datetime.strptime(start_part.upper(), "%m-%d-%Y %I.%M%p")
        unix_ts = int(dt.timestamp())
        # Include _fs320 so Omi's backend uses the correct Opus frame size.
        # The Limitless pendant records at 50 fps × 16kHz = 320 samples/frame.
        # Without this, the backend defaults to 160 samples/frame and decodes garbage.
        return f"recording_fs320_{unix_ts}.bin"
    except (ValueError, IndexError):
        mtime = int(bin_path.stat().st_mtime)
        print(f"[!] Could not parse timestamp from '{bin_path.name}', using mtime ({mtime}).")
        return f"recording_fs320_{mtime}.bin"


# ==========================================
# UPLOAD
# ==========================================

def upload_bins(bin_dir: Path, id_token: str) -> dict | None:
    """
    POSTs all .bin files in bin_dir to Omi's sync endpoint as multipart/form-data.

    Tries /v2/sync-local-files first (async, returns job_id via 202).
    If the server responds with 200 instead, treats it as a /v1 synchronous
    result and returns the parsed response dict directly (no polling needed).

    Returns:
      - A dict with "job_id" key → caller should poll.
      - A dict with "new_memories" / "updated_memories" keys → v1 result, done.
      - Exits with code 1 on error.
    """
    bin_files = sorted(bin_dir.glob("*.bin"))
    if not bin_files:
        print("No .bin files found to upload.")
        sys.exit(0)

    print(f"Uploading {len(bin_files)} .bin file(s) to Omi cloud sync...")

    file_handles = []
    files_param  = []
    try:
        for bf in bin_files:
            upload_name = make_upload_name(bf)
            print(f"  {bf.name}  →  {upload_name}")
            fh = open(bf, "rb")
            file_handles.append(fh)
            files_param.append(("files", (upload_name, fh, "application/octet-stream")))

        resp = requests.post(
            OMI_SYNC_V2_URL,
            headers={"Authorization": f"Bearer {id_token}"},
            files=files_param,
            timeout=120,
        )
    finally:
        for fh in file_handles:
            fh.close()

    if resp.status_code == 202:
        # v2 async: poll for completion.
        data = resp.json()
        print(f"Upload accepted (async). job_id={data.get('job_id')}, total_segments={data.get('total_segments', '?')}")
        return data

    if resp.status_code == 200:
        print(f"Upload complete (synchronous response). Raw: {resp.text[:500]}")
        return resp.json()

    print(f"[!] Upload failed ({resp.status_code}): {resp.text}")
    sys.exit(1)


# ==========================================
# POLLING
# ==========================================

def poll_job(job_id: str, id_token: str) -> dict:
    """
    Polls /v2/sync-local-files/{job_id} until the job reaches a terminal state.

    Terminal states: completed, partial_failure, failed
    Prints progress on each poll tick.

    Returns the final result dict on success/partial_failure.
    Exits with code 1 on failure or timeout.
    """
    url     = OMI_JOB_URL.format(job_id=job_id)
    headers = {"Authorization": f"Bearer {id_token}"}
    elapsed = 0

    print(f"Polling job {job_id} (checking every {POLL_INTERVAL_SECONDS}s)...")

    while elapsed < POLL_TIMEOUT_SECONDS:
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException as e:
            print(f"[!] Poll request failed: {e} — retrying...")
            continue

        if resp.status_code != 200:
            print(f"[!] Poll returned {resp.status_code}: {resp.text} — retrying...")
            continue

        data   = resp.json()
        status = data.get("status", "unknown")
        done   = data.get("processed_segments", "?")
        total  = data.get("total_segments", "?")

        print(f"  [{elapsed:>4}s] status={status}  segments={done}/{total}")

        if status == "completed":
            print("Job completed successfully.")
            return data

        if status == "partial_failure":
            failed = data.get("failed_segments", "?")
            print(f"Job completed with partial failure: {failed} segment(s) failed.")
            return data

        if status == "failed":
            error = data.get("error", "unknown error")
            print(f"[!] Job failed: {error}")
            sys.exit(1)

        # Still processing — keep polling.

    print(f"[!] Polling timed out after {POLL_TIMEOUT_SECONDS}s.")
    sys.exit(1)


# ==========================================
# CLEANUP
# ==========================================

def cleanup_bins(bin_dir: Path, synced_dir: Path, action: str):
    """
    After a successful sync, either move or delete the uploaded .bin files.

    action: "keep" — move to synced_dir (default mirrors existing WAV behavior)
            "delete" — permanently remove
    """
    bin_files = sorted(bin_dir.glob("*.bin"))
    if not bin_files:
        return

    if action == "delete":
        for bf in bin_files:
            bf.unlink(missing_ok=True)
        print(f"Deleted {len(bin_files)} .bin file(s).")
    else:
        synced_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for bf in bin_files:
            dest = synced_dir / bf.name
            # Avoid overwriting if a same-named file already exists.
            if dest.exists():
                dest = synced_dir / f"{bf.stem}_dup{int(time.time())}.bin"
            bf.rename(dest)
            moved += 1
        print(f"Moved {moved} .bin file(s) to {synced_dir}/")


# ==========================================
# MAIN
# ==========================================

def main():
    # Load .env from project root (two levels above bin_dir, or alongside this script).
    script_dir = Path(__file__).resolve().parent
    load_dotenv(dotenv_path=script_dir.parent / ".env")

    parser = argparse.ArgumentParser(
        description="Upload .bin files to Omi cloud for server-side transcription."
    )
    parser.add_argument("bin_dir", type=Path, help="Directory containing .bin files")
    parser.add_argument("--firebase-key", default=os.getenv("OMI_FIREBASE_WEB_API_KEY", DEFAULT_FIREBASE_WEB_API_KEY),
                        help="Firebase Web API Key (defaults to Omi's public key)")
    parser.add_argument("--synced-bin-action", default=os.getenv("SYNCED_BIN_ACTION", "keep"),
                        choices=["keep", "delete"],
                        help="What to do with .bin files after successful sync (keep/delete)")
    args = parser.parse_args()

    bin_dir = args.bin_dir.resolve()
    if not bin_dir.is_dir():
        sys.exit(f"ERROR: bin_dir does not exist: {bin_dir}")

    bin_files = list(bin_dir.glob("*.bin"))
    if not bin_files:
        print("No .bin files found — nothing to sync.")
        sys.exit(0)

    # Determine synced_to_omi directory — sibling of downloads/, inside limitless_data/.
    synced_dir = bin_dir.parent / "synced_to_omi"

    # Step 1: Authenticate.
    id_token = get_firebase_token(
        bin_dir,
        firebase_key=args.firebase_key,
        direct_token=os.getenv("OMI_FIREBASE_TOKEN", ""),
        direct_refresh_token=os.getenv("OMI_FIREBASE_REFRESH_TOKEN", ""),
    )

    # Step 2: Upload.
    upload_result = upload_bins(bin_dir, id_token)

    # Step 3: Poll if async (v2), or use result directly if sync (v1).
    if "job_id" in upload_result:
        result = poll_job(upload_result["job_id"], id_token)
        job_result = result.get("result") or {}
    else:
        job_result = upload_result

    # Step 4: Print summary.
    new_mems   = job_result.get("new_memories", [])
    upd_mems   = job_result.get("updated_memories", [])
    errors     = job_result.get("errors", [])

    print(f"\n--- Omi Cloud Sync Summary ---")
    print(f"  New conversations/memories : {len(new_mems)}")
    print(f"  Updated memories           : {len(upd_mems)}")
    print(f"  Failed segments            : {job_result.get('failed_segments', 0)}")
    if errors:
        for err in errors:
            print(f"  [!] {err}")

    # Step 5: Cleanup — only if Omi confirmed it received something.
    # An empty result with 0 new and 0 updated could mean the upload silently
    # failed. Require at least one non-empty field before moving files.
    if new_mems or upd_mems or job_result.get("failed_segments", 0) > 0:
        cleanup_bins(bin_dir, synced_dir, args.synced_bin_action)
    else:
        print("[!] No conversations or memories returned — files NOT moved. Check Omi app before retrying.")
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
