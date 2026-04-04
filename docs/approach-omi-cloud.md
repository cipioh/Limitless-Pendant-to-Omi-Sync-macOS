# Approach: Omi Cloud Sync

**Best for:** Simplest setup, no local ML dependencies, best speaker identification.

In this mode, raw audio is pulled off the pendant over Bluetooth and uploaded directly to Omi's backend — the same path the Omi mobile app uses for offline batch sync. Omi handles everything: Deepgram Nova-3 transcription, speaker identification against your stored voice profiles, memory extraction, and timeline integration. Nothing runs locally beyond the BLE download itself.

```
Limitless Pendant
       │ BLE
       ▼
┌──────────────┐
│ download.py  │  Phase 1 — BLE download → raw Opus .bin files
└──────┬───────┘
       │ .bin (raw Opus)
       ▼
┌──────────────────────┐
│ sync_omi_cloud.py    │  Upload → Omi processes server-side:
│                      │    • Deepgram Nova-3 transcription
│                      │    • Speaker identification
│                      │    • Memory + conversation creation
└──────┬───────────────┘
       ▼
  Omi Timeline
```

---

## What Omi Does Server-Side

When `.bin` files are received, Omi's backend runs:

1. Opus decode → PCM
2. Voice Activity Detection (VAD) — splits on 120s silence gaps
3. Deepgram Nova-3 transcription (uses your account's language and vocabulary settings)
4. Speaker identification — biometric matching against your stored speech profiles
5. Conversation and memory creation — same result as if the pendant synced via the mobile app

---

## Prerequisites

- Omi account (Google or Apple sign-in)
- Firebase credentials from a browser session (one-time setup, see below)
- No local ML models, no GPU, no additional pip installs

### Subscription requirement

**This approach consumes Omi cloud transcription minutes** because Omi runs Deepgram on your audio server-side. The free Omi plan includes **1,200 minutes/month**. If you record more than that, you need the **Unlimited plan** ($19/month or $199/year).

Every minute of audio you sync counts against this quota. A typical workday of pendant recording (5–8 hours) would exhaust the free tier in a few days. This approach is best suited to users with an Unlimited subscription.

---

## Setup

### 1. Get your Firebase credentials (one-time)

Since Omi uses Google/Apple OAuth rather than email/password, you need to extract a Firebase token from a live browser session once. After that, the token auto-renews indefinitely.

**Step 1 — Get the Bearer token:**
1. Open [app.omi.me](https://app.omi.me) in Chrome and sign in
2. Open DevTools (`F12` or `Cmd+Option+I`) → **Network** tab
3. Click any request in the list → **Headers** → copy the value after `Authorization: Bearer `

**Step 2 — Get the refresh token (for automatic long-term renewal):**
1. In DevTools → **Application** tab
2. **IndexedDB** → `firebaseLocalStorageDb` → `firebaseLocalStorage`
3. Expand your user entry → `stsTokenManager` → copy `refreshToken`

### 2. Add to `.env`

```env
TRANSCRIPTION_ENGINE=omi_cloud

# Required: one-time Bearer token (expires ~1hr, used for first run)
OMI_FIREBASE_TOKEN=eyJ...

# Strongly recommended: refresh token for automatic long-term renewal
OMI_FIREBASE_REFRESH_TOKEN=AMf...
```

Once `OMI_FIREBASE_REFRESH_TOKEN` is set and the script has run once successfully, it caches the tokens locally. You can clear `OMI_FIREBASE_TOKEN` from `.env` after the first run — the refresh token handles everything from there.

> **Note:** `OMI_API_KEY` is not required for this approach. The Firebase credentials are used instead.

---

## How the Token Cache Works

After the first successful run, credentials are cached in `.firebase_token_cache.json` at the project root:

- **`idToken`** — short-lived Firebase ID token (~1 hour)
- **`refreshToken`** — long-lived token that auto-generates new ID tokens
- **`expires_at`** — Unix timestamp of when the current ID token expires

On each run, the script checks if the cached ID token is still valid. If it's expired, it silently exchanges the refresh token for a fresh one and updates the cache. The refresh token itself rotates on each use — the latest value is always written back to the cache. This means as long as the script runs at least once every few months, it never needs manual credential renewal.

---

## What to Expect in the Logs

```
==========================================
[04-03-2026 05:30:00PM] Starting Sync Cycle...
[04-03-2026 05:30:00PM] Transcription engine: omi_cloud
[04-03-2026 05:30:01PM] Attempting connection to Pendant...
[04-03-2026 05:30:04PM]      | Battery Level: 87%
[04-03-2026 05:30:05PM]      | Pendant reported 1240 unread flash pages (28:45 of audio)
[04-03-2026 05:30:38PM] Download complete. Moving to conversion.
[04-03-2026 05:30:38PM] Starting Omi Cloud Sync...
[04-03-2026 05:30:38PM]      | Using cached Firebase token.
[04-03-2026 05:30:38PM]      | Uploading 6 .bin file(s) to Omi cloud sync...
[04-03-2026 05:30:41PM]      | Upload complete (synchronous response).
[04-03-2026 05:30:41PM]      | New conversations/memories: 3
[04-03-2026 05:30:41PM]      | Moved 6 .bin file(s) to synced_to_omi/
[04-03-2026 05:30:41PM] Cycle complete.
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `TRANSCRIPTION_ENGINE` | — | Set to `omi_cloud` |
| `OMI_FIREBASE_TOKEN` | *(empty)* | Bearer token from browser session. Only needed for first run. |
| `OMI_FIREBASE_REFRESH_TOKEN` | *(empty)* | Long-lived refresh token from browser IndexedDB. Auto-renews indefinitely. |
| `OMI_FIREBASE_WEB_API_KEY` | `***REDACTED***` | Omi's Firebase project key. Only change if self-hosting. |
| `SYNCED_BIN_ACTION` | `keep` | What to do with `.bin` files after upload: `keep` (move to `synced_to_omi/`) or `delete` |
| `SYNCED_BIN_RETENTION_DAYS` | `7` | Days to keep `.bin` files in `synced_to_omi/`. `0` = forever. |

---

## Trade-offs

| | Omi Cloud Sync |
|---|---|
| Transcription quality | Deepgram Nova-3 (high quality) |
| Speaker identification | Yes — biometric matching against your Omi profile |
| Requires internet | Yes — for upload and processing |
| Privacy | Audio is sent to Omi's servers |
| Local ML dependencies | None |
| Setup complexity | Low (one-time browser token extraction) |
| Processing location | Omi cloud |
| **Subscription** | **Unlimited plan recommended** — consumes cloud transcription minutes (1,200 min/month free, then paid) |
