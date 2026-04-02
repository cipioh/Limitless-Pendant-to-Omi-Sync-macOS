#!/usr/bin/env python3

"""
Limitless Pendant Audio Converter  (convert.py)
------------------------------------------------
Role in the pipeline: PHASE 2 of 4

This script is called by pendant_sync.py after a successful download.
It takes the raw `.bin` files produced by download.py and converts them
into standard `.wav` audio files that MacWhisper can transcribe.

INPUT:  A directory of `.bin` files, each containing raw Opus-encoded audio
        frames in a simple length-prefixed format:
            [4-byte little-endian frame length][raw Opus frame bytes]
            [4-byte little-endian frame length][raw Opus frame bytes]
            ...

OUTPUT: One `.wav` file per `.bin` file, written to a `wav_exports/`
        subdirectory. The output is standard 16kHz mono PCM — the same
        format that any transcription engine expects.

WHY THIS STEP EXISTS:
The pendant stores audio as raw Opus frames because Opus is extremely
efficient for storage. But transcription engines like MacWhisper need
uncompressed PCM audio. This step bridges the gap: decode Opus → write WAV.

ATOMIC WRITE PATTERN:
Each output file is first written as a `.wav.tmp` and only renamed to `.wav`
after the entire file is successfully written and closed. This prevents
pendant_sync.py's file watcher from picking up a half-written file.
"""

import argparse
import struct
import wave
from pathlib import Path
import sys

# Opus decoding requires the opuslib package.
try:
    import opuslib
except ImportError:
    print("Error: opuslib not found. Run: pip install opuslib")
    sys.exit(1)

# ==========================================
# PENDANT AUDIO SPECIFICATIONS
# ==========================================
# These values are fixed by the Limitless Pendant's firmware.
# The pendant records at 16kHz mono, generating roughly 50 Opus
# frames per second. Each frame covers exactly 20ms of audio,
# which corresponds to 320 PCM samples at 16kHz (16000 * 0.02).
SAMPLE_RATE = 16000  # Hz — must match the pendant's recording rate
CHANNELS = 1         # Mono
FRAME_SIZE = 320     # PCM samples per Opus frame (20ms at 16kHz)


def convert_bin_to_wav(bin_path: Path, wav_path: Path):
    """
    Decodes a single `.bin` file into a standard `.wav` file.

    The .bin format (written by download.py) is a flat sequence of
    length-prefixed Opus frames:
        Bytes 0-3:  Frame length N (little-endian uint32)
        Bytes 4 to 4+N: Raw Opus compressed audio data
        (repeats for each frame)

    Each Opus frame is decoded into 320 raw 16-bit PCM samples,
    which are then written sequentially into the WAV file.

    The output file is written atomically: it goes to a `.wav.tmp`
    first, then renamed to `.wav` only after a successful close.
    This prevents the MacWhisper watch folder from picking up an
    incomplete file.

    Returns the number of Opus frames successfully decoded.
    """
    decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)

    # Write to a .tmp path first. The file watcher in pendant_sync.py
    # only triggers on .wav files, so this is safe to write at any time.
    temp_path = wav_path.with_suffix(".wav.tmp")

    with open(bin_path, "rb") as f_in, wave.open(str(temp_path), "wb") as f_out:
        # Configure the WAV container header before writing any audio data.
        f_out.setnchannels(CHANNELS)
        f_out.setsampwidth(2)  # 16-bit PCM = 2 bytes per sample
        f_out.setframerate(SAMPLE_RATE)

        frames_processed = 0
        frames_skipped = 0

        while True:
            # Read the 4-byte length prefix for the next Opus frame.
            length_bytes = f_in.read(4)
            if not length_bytes:
                break  # Clean EOF — all frames have been read.

            if len(length_bytes) != 4:
                # Partial read at end of file — the .bin was likely truncated.
                print(f"  [!] Warning: Truncated frame length at end of {bin_path.name}")
                break

            # Unpack as a little-endian unsigned 32-bit integer.
            frame_len = struct.unpack("<I", length_bytes)[0]

            # Sanity check: valid Opus frames for 16kHz mono are typically
            # 10–200 bytes. Anything outside this range indicates file corruption.
            if frame_len <= 0 or frame_len > 2000:
                print(f"  [!] Warning: Invalid frame length {frame_len} in {bin_path.name}")
                break

            # Read exactly frame_len bytes of compressed Opus data.
            opus_data = f_in.read(frame_len)

            if len(opus_data) != frame_len:
                # The file ended before we could read the full frame payload.
                print(f"  [!] Warning: Truncated frame payload in {bin_path.name}")
                break

            try:
                # Decode the Opus frame into raw 16-bit signed PCM samples.
                # FRAME_SIZE tells the decoder how many samples to produce (320 = 20ms).
                pcm_data = decoder.decode(opus_data, FRAME_SIZE)

                # Sanity-check the decoded output size.
                # 16-bit PCM = 2 bytes/sample × FRAME_SIZE samples × CHANNELS.
                expected_bytes = FRAME_SIZE * CHANNELS * 2
                if len(pcm_data) != expected_bytes:
                    print(f"  [!] Frame {frames_processed}: unexpected PCM size {len(pcm_data)}B (expected {expected_bytes}B) — skipping", flush=True)
                    frames_skipped += 1
                    continue

                f_out.writeframes(pcm_data)
                frames_processed += 1
            except Exception as e:
                # A single bad frame is skipped rather than aborting the whole file.
                # Corrupt frames occasionally appear at the beginning or end of a
                # recording session when the pendant's buffer boundary is unclean.
                print(f"  [!] Frame {frames_processed} decode failed ({e}) — skipping", flush=True)
                frames_skipped += 1
                continue

    # Atomic rename: only make the .wav visible to downstream tools after
    # the file is fully written and the file handle is closed.
    temp_path.rename(wav_path)

    if frames_skipped:
        print(f"  [!] {frames_skipped} frame(s) skipped in {bin_path.name} due to decode errors.", flush=True)

    return frames_processed


def main():
    """
    CLI entry point. Processes all `.bin` files in the given directory.

    Called by pendant_sync.py as:
        python3 convert.py <DOWNLOAD_DIR>

    Writes output `.wav` files to <DOWNLOAD_DIR>/wav_exports/.
    Exits with code 0 on success. pendant_sync.py uses this exit code
    to decide whether it's safe to delete the source .bin files.
    """
    parser = argparse.ArgumentParser(
        description="Convert Limitless Pendant .bin audio to .wav"
    )
    parser.add_argument(
        "input_dir",
        help="Directory containing the downloaded .bin files"
    )
    args = parser.parse_args()

    input_path = Path(args.input_dir).expanduser()

    if not input_path.is_dir():
        print(f"Error: {input_path} is not a valid directory.")
        sys.exit(1)

    # Output goes into a dedicated subdirectory so the MacWhisper watch
    # folder can be pointed at wav_exports/ without seeing the raw .bin files.
    output_dir = input_path / "wav_exports"
    output_dir.mkdir(exist_ok=True)

    bin_files = list(input_path.glob("*.bin"))

    if not bin_files:
        print(f"No .bin files found in {input_path}")
        sys.exit(0)

    print(f"Found {len(bin_files)} .bin files. Converting to .wav...")

    success_count = 0

    # Process in alphabetical order. Because download.py names files with a
    # date/time prefix (e.g. "04-01-2026 02.30PM to 02.45PM.bin"), alphabetical
    # order is effectively chronological order.
    for bin_file in sorted(bin_files):
        wav_file = output_dir / (bin_file.stem + ".wav")

        try:
            convert_bin_to_wav(bin_file, wav_file)
            success_count += 1
        except Exception as e:
            print(f"Failed to convert {bin_file.name}: {e}")

    print(f"\nDone! Successfully converted {success_count} files.")
    print(f"Your .wav files are ready in: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
