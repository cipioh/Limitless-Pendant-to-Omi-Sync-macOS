#!/usr/bin/env python3

"""
Limitless Pendant BLE Downloader  (download.py)
------------------------------------------------
Role in the pipeline: PHASE 1 of 4

This script connects to a Limitless Pendant over Bluetooth Low Energy (BLE),
reads the stored audio pages from its flash memory, extracts the raw Opus
audio frames, and writes them to `.bin` files for downstream conversion.

HOW THE PENDANT COMMUNICATES:
The pendant uses a custom proprietary BLE protocol layered on top of standard
GATT. Commands are sent to a custom TX characteristic; responses and audio data
arrive asynchronously as BLE notifications on a custom RX characteristic.
The binary encoding used for both is a hand-rolled Protobuf-like varint format
that mirrors the field layout of the pendant's actual .proto definitions.
The `LimitlessProtocol` class encodes/decodes this format in pure Python,
without requiring the original .proto source files.

FRAGMENT REASSEMBLY:
BLE has a maximum transmission unit (MTU) that limits each notification to
a small number of bytes. The pendant splits large messages across multiple
notification "fragments". Each fragment carries:
  - A group index (which message this fragment belongs to)
  - A sequence number (position within that message)
  - A total fragment count (how many fragments make up the full message)
  - The fragment payload bytes
The `_notification_handler` collects fragments by group index and reassembles
them into complete messages once all expected fragments have arrived.

THE SORTING CONVEYOR BELT:
Pages arrive from the pendant in roughly sequential order, but BLE notifications
are not guaranteed to arrive in order. Before processing, the list of completed
pages is sorted by flash page index every loop iteration. This guarantees that
the output `.bin` files contain frames in the correct chronological sequence,
regardless of the arrival order of the underlying BLE notifications.

GAP DETECTION:
The pendant assigns a millisecond timestamp to each flash page. If the timestamp
of an incoming page differs from the expected time (based on frame count and
sample rate) by more than 60 seconds, a temporal gap is assumed and a new `.bin`
file is started. This splits separate recording sessions into separate files rather
than incorrectly concatenating them.

THE CONVERSATIONSTREAMER (streaming-to-disk pattern):
Earlier versions buffered all audio in RAM before writing. This caused memory
pressure and data loss on crashes. The `ConversationStreamer` class streams each
Opus frame directly to a `.part` file on disk as it arrives. The `.part` extension
prevents downstream tools from reading an incomplete file. When a session is
complete (or a gap is detected), the `.part` file is atomically renamed to its
final `.bin` name with a human-readable timestamp.

EXIT CODES (interpreted by pendant_sync.py):
    0 — Total success: all pages up to newest_page were downloaded.
    1 — Script/logic error (Python exception, programming bug).
    2 — Bluetooth hardware error (stall, disconnect, encryption failure).
    3 — Partial success: safe chunk limit (2,000 pages) reached, more data remains.
        pendant_sync.py will loop and call this script again immediately.
    4 — Pendant not found: out of range or powered off.
"""

import argparse
import asyncio
import importlib
import json
import os
import struct
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def local_now_stamp() -> str:
    return datetime.now().strftime("Imported %m-%d-%Y at %I.%M%p")

# ==========================================
# DEPENDENCY GUARD (bleak BLE library)
# ==========================================
# bleak is the cross-platform Python BLE library. We import it dynamically
# so that import errors produce a clear message rather than a cryptic traceback.
# If bleak is missing, BleakClient and BleakScanner are set to None and the
# main() function will raise a clear RuntimeError.
try:
    bleak_module = importlib.import_module("bleak")
    BleakClient = getattr(bleak_module, "BleakClient")
    BleakScanner = getattr(bleak_module, "BleakScanner")
    try:
        bleak_exc_module = importlib.import_module("bleak.exc")
        BleakBluetoothNotAvailableError = getattr(
            bleak_exc_module, "BleakBluetoothNotAvailableError"
        )
    except ModuleNotFoundError:
        BleakBluetoothNotAvailableError = RuntimeError
except ModuleNotFoundError:
    BleakClient = None
    BleakScanner = None
    BleakBluetoothNotAvailableError = RuntimeError

BleakClientType = Any

# ==========================================
# BLE SERVICE AND CHARACTERISTIC UUIDs
# ==========================================
# Standard Bluetooth GATT UUIDs follow a pattern where the base UUID is always
# the same and only the first 8 hex digits change. The constants below cover
# the two standard services (Battery, Device Information) and the Limitless
# pendant's fully custom service that handles all audio data transfer.

BLUETOOTH_BASE_UUID = "-0000-1000-8000-00805f9b34fb"

# Standard GATT Battery Service — reports pendant battery percentage (0-100).
BATTERY_SERVICE_UUID = "0000180f" + BLUETOOTH_BASE_UUID
BATTERY_LEVEL_CHARACTERISTIC_UUID = "00002a19" + BLUETOOTH_BASE_UUID

# Standard GATT Device Information Service — used for the firmware version read.
DEVICE_INFO_SERVICE_UUID = "0000180a" + BLUETOOTH_BASE_UUID
FIRMWARE_REVISION_CHARACTERISTIC_UUID = "00002a26" + BLUETOOTH_BASE_UUID

# Limitless Pendant Custom Service — all audio download commands flow through here.
# TX (write with response): where we SEND commands TO the pendant.
# RX (notify):              where we RECEIVE data FROM the pendant.
LIMITLESS_SERVICE_UUID = "632de001-604c-446b-a80f-7963e950f3fb"
LIMITLESS_TX_CHAR_UUID = "632de002-604c-446b-a80f-7963e950f3fb"
LIMITLESS_RX_CHAR_UUID = "632de003-604c-446b-a80f-7963e950f3fb"

# ==========================================
# DEFAULT PATHS
# ==========================================
DEFAULT_LOG_FILE = Path.home() / "omi/limitless_data/logs/limitless_download.log"
DEFAULT_OUTPUT_ROOT = Path.home() / "omi/limitless_data/downloads"

# ==========================================
# PENDANT AUDIO CONSTANTS
# ==========================================
# These values describe the pendant's audio storage format and are used to
# estimate download duration, detect temporal gaps between recording sessions,
# and calculate accurate timestamps for output filenames.
#
# SECONDS_PER_FLASH_PAGE: Each flash page holds approximately 1.4 seconds of
#   audio. Used to estimate total download duration from the page range.
# FRAMES_PER_FLASH_PAGE: Each page holds 8 Opus audio frames.
# FRAMES_PER_SECOND: The pendant records at 50 frames/second (20ms per frame).
SECONDS_PER_FLASH_PAGE = 1.4
FRAMES_PER_FLASH_PAGE = 8
FRAMES_PER_SECOND = 50

# Reject timestamps before Nov 2023 — the pendant occasionally emits a
# near-epoch-zero timestamp ("1969 glitch") that would produce garbage filenames.
MIN_VALID_TIMESTAMP_MS = 1_700_000_000_000

# Maximum pages to download per chunk before handing off to conversion.
# Passed as safe_page_limit to download_audio(). Named here so it appears
# in one place and is easy to tune.
SAFE_PAGE_LIMIT = 2000


# ==========================================
# DATA CLASSES
# ==========================================

@dataclass
class DeviceCandidate:
    """
    Represents a BLE device discovered during scanning that might be the pendant.

    `score` is a priority ranking: devices advertising the Limitless custom service
    UUID score highest (100), followed by devices with "limitless" (80) or "pendant"
    (60) in their advertised name. The highest-scoring candidate is chosen.
    """
    device: Any
    advertisement: Any
    name: str
    services: set[str]
    score: int


# ==========================================
# RUN LOGGER
# ==========================================

class RunLogger:
    """
    Appends structured log entries to a dedicated download log file.

    This is separate from the main automation.log written by pendant_sync.py.
    It records low-level BLE events (battery reads, storage status, acks, errors)
    in a structured key=value format. Useful for debugging BLE issues without
    cluttering the main operational log.
    """
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = time.monotonic()
        self.last_event_at = self.started_at

    def log(self, event: str, **data: Any) -> None:
        now = time.monotonic()
        timestamp_str = datetime.now().strftime('%m-%d-%Y %I:%M:%S %p')
        data_parts = [f"{k}={v}" for k, v in data.items()]
        data_str = " | " + ", ".join(data_parts) if data_parts else ""

        log_line = f"[{timestamp_str}] {event.upper()}{data_str}\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(log_line)
        self.last_event_at = now


# ==========================================
# PROGRESS BAR
# ==========================================

class ProgressBar:
    """
    Renders an animated in-place terminal progress bar during download.

    Uses carriage-return (\r) to overwrite the same line repeatedly, giving
    the appearance of an updating progress indicator without scrolling the
    terminal. Shows: filled bar, page count, percent, pages/sec, ETA, file count.

    The `quiet` flag suppresses all output (used when running as a subprocess
    under pendant_sync.py, where the output is captured and logged line-by-line —
    the animated bar would create thousands of identical log entries).
    """
    def __init__(self, total_pages: int, quiet: bool = False):
        self.total_pages = max(total_pages, 1)
        self.last_render = 0.0
        self.last_width = 0
        self.quiet = quiet

    def update(self, processed_pages: int, files_saved: int, started_at: float) -> None:
        if self.quiet:
            return

        now = time.monotonic()
        # Throttle renders to max 5 per second (every 0.2s) to avoid CPU waste.
        if now - self.last_render < 0.2 and processed_pages < self.total_pages:
            return

        elapsed = max(now - started_at, 0.001)
        percent = min(max(processed_pages / self.total_pages, 0.0), 1.0)
        pages_per_second = processed_pages / elapsed if processed_pages > 0 else 0.0
        remaining_pages = max(self.total_pages - processed_pages, 0)
        eta_seconds = (
            int(round(remaining_pages / pages_per_second))
            if pages_per_second > 0
            else None
        )

        bar_width = 28
        filled = int(percent * bar_width)
        bar = "#" * filled + "-" * (bar_width - filled)

        eta_text = format_duration(eta_seconds) if eta_seconds is not None else "--:--"
        line = (
            f"[{bar}] {processed_pages:>4}/{self.total_pages:<4} pages "
            f"{percent * 100:5.1f}% | {pages_per_second:4.2f} p/s | ETA {eta_text} | files {files_saved}"
        )
        self.last_width = max(self.last_width, len(line))
        print(f"\r{line.ljust(self.last_width)}", end="", flush=True)
        self.last_render = now

    def finish(self, message: str) -> None:
        if self.quiet:
            return
        print(f"\r{message.ljust(max(self.last_width, len(message)))}")


# ==========================================
# LIMITLESS PROTOCOL
# ==========================================

class LimitlessProtocol:
    """
    Encapsulates all communication with the Limitless Pendant's custom BLE protocol.

    The pendant's audio download protocol is a custom binary format that resembles
    Protocol Buffers (Protobuf) — it uses the same varint encoding and field
    tag format (field_number << 3 | wire_type), but without a compiled .proto schema.
    All encoding and decoding is implemented here by mirroring the known field layout.

    OUTGOING (TX) MESSAGE STRUCTURE:
    Every command we send is wrapped in an outer BLE envelope:
        Field 1: message_index (increments with each write — acts as a sequence ID)
        Field 2: constant 0
        Field 3: constant 1
        Field 4: the actual command payload bytes
    Inside the payload, most commands also include a "request envelope" (field 30)
    containing a monotonically increasing request_id.

    INCOMING (RX) NOTIFICATION STRUCTURE:
    Each BLE notification from the pendant is a fragment:
        Field 1: group_index (which logical message this fragment belongs to)
        Field 2: sequence_number (position within the group)
        Field 3: total fragment count for this group
        Field 4: fragment payload bytes
    The `_notification_handler` reassembles fragments by group_index and passes
    complete messages to `_handle_pendant_message` for parsing.

    STATE:
        fragment_buffer: dict mapping group_index → {seq: payload} for in-flight fragments
        completed_flash_pages: list of fully parsed page dicts, ready for processing
        storage_state: the most recently received device storage status
        storage_state_future: an asyncio Future resolved when a storage response arrives
        is_batch_mode: True while in batch download mode (we only process audio then)
    """
    def __init__(self, client: BleakClientType, logger: RunLogger):
        self.client = client
        self.logger = logger
        self.message_index = 0
        self.request_id = 0
        self.fragment_buffer: dict[int, dict[int, list[int]]] = {}
        self.completed_flash_pages: list[dict[str, Any]] = []
        self.storage_state: dict[str, int] | None = None
        self.storage_state_future: asyncio.Future[dict[str, int] | None] | None = None
        self.is_batch_mode = False

    async def start(self) -> None:
        """
        Subscribes to RX notifications and sends the initialization sequence.
        Must be called immediately after connecting to the pendant.
        """
        await self.client.start_notify(
            LIMITLESS_RX_CHAR_UUID, self._notification_handler
        )
        await asyncio.sleep(1)
        await self._initialize()

    async def stop(self) -> None:
        """Unsubscribes from RX notifications. Called on clean disconnect."""
        try:
            await self.client.stop_notify(LIMITLESS_RX_CHAR_UUID)
        except Exception:
            pass

    async def _initialize(self) -> None:
        """
        Sends the two required startup commands to the pendant:
        1. Set current time — the pendant uses this to timestamp recorded pages.
           Without it, page timestamps will be wrong and filenames will be inaccurate.
        2. Enable data stream — activates the pendant's data output mode.
           Without this, the pendant will not respond to storage status or batch mode commands.
        """
        await self._write(self._encode_set_current_time(int(time.time() * 1000)))
        await asyncio.sleep(1)
        await self._write(self._encode_enable_data_stream())
        await asyncio.sleep(1)
        self.logger.log("protocol_initialized")

    async def _write(self, payload: bytes) -> None:
        """
        Sends a command to the pendant via the TX characteristic.
        `response=True` means we wait for a GATT write acknowledgement before returning.
        """
        await self.client.write_gatt_char(LIMITLESS_TX_CHAR_UUID, payload, response=True)

    async def get_battery_level(self) -> int | None:
        """
        Reads the pendant's battery percentage from the standard GATT Battery Service.
        Returns None if the read fails (e.g., characteristic not available on this firmware).
        """
        try:
            data = await self.client.read_gatt_char(BATTERY_LEVEL_CHARACTERISTIC_UUID)
            if data and len(data) > 0:
                level = int(data[0])
                self.logger.log("battery_level_read", level=level)
                return level
        except Exception as error:
            self.logger.log("battery_level_read_error", error=str(error))
        return None

    async def get_storage_status(self) -> dict[str, int] | None:
        """
        Requests and returns the pendant's current flash storage status.

        The request is sent as a command to TX. The response arrives asynchronously
        as a BLE notification on RX, parsed by `_try_parse_device_status`. We use
        an asyncio Future to bridge this async gap: the notification handler resolves
        the Future when it sees a valid storage response, and we wait on that Future
        here with a 3-second timeout. If the notification never arrives, we fall back
        to `self.storage_state` (which may have been populated by a spontaneous notification).

        The returned dict contains:
            oldest_flash_page  — the lowest unacknowledged page index on the pendant
            newest_flash_page  — the highest recorded page index (inclusive)
            current_storage_session — the current recording session ID
            free_capture_pages — pages still available for new recording
            total_capture_pages — total flash capacity in pages
        """
        loop = asyncio.get_running_loop()
        self.storage_state_future = loop.create_future()
        await self._write(self._encode_get_device_status())
        self.logger.log("storage_status_requested")
        try:
            result = await asyncio.wait_for(self.storage_state_future, timeout=3.0)
        except asyncio.TimeoutError:
            result = self.storage_state
        finally:
            self.storage_state_future = None

        self.logger.log("storage_status_received", storage_status=result)
        return result

    async def enable_batch_mode(self) -> None:
        """
        Switches the pendant into batch download mode.

        In batch mode: batch_mode=True, real_time=False.
        The pendant stops streaming live audio and instead starts replaying stored
        pages from flash memory. Pages begin arriving as RX notifications immediately.
        `clear_buffers()` is called first to discard any leftover fragments from
        a previous session.
        """
        self.clear_buffers()
        self.is_batch_mode = True
        await self._write(
            self._encode_download_flash_pages(batch_mode=True, real_time=False)
        )
        self.logger.log("batch_mode_enabled")

    async def enable_real_time_mode(self) -> None:
        """
        Switches the pendant back to real-time streaming mode.

        In real-time mode: batch_mode=False, real_time=True.
        This is the pendant's normal operating state. It MUST be restored before
        disconnecting, or the pendant may remain in batch mode and miss live recordings.
        Called in the `finally` block of `download_audio` to ensure it always runs,
        even if the download was interrupted by an exception.
        """
        self.clear_buffers()
        self.is_batch_mode = False
        await self._write(
            self._encode_download_flash_pages(batch_mode=False, real_time=True)
        )
        self.logger.log("real_time_mode_enabled")

    async def acknowledge_processed_data(self, up_to_index: int) -> None:
        """
        Sends a cumulative acknowledgement to the pendant.

        IMPORTANT: Acknowledgement is DESTRUCTIVE. It tells the pendant that all
        pages up to and including `up_to_index` have been safely received and
        processed. The pendant will free that flash memory for new recordings.

        The acknowledgement is CUMULATIVE, not per-page. One ack of page 1500
        clears all pages from oldest up to 1500.

        We clamp the ack index to `end_page` (the newest page at the time the
        download started) to avoid accidentally acknowledging pages that were
        recorded during the download run itself.
        """
        await self._write(self._encode_acknowledge_processed_data(up_to_index))
        self.logger.log("ack_sent", up_to_index=up_to_index)

    def clear_buffers(self) -> None:
        """
        Clears the fragment reassembly buffer and the completed pages list.
        Called before switching modes to prevent fragments from a previous
        session from being mixed into the new one.
        """
        self.fragment_buffer.clear()
        self.completed_flash_pages.clear()


    def _notification_handler(self, _: Any, data: bytearray) -> None:
        """
        Callback invoked by bleak for every BLE notification on the RX characteristic.

        TWO JOBS:
        1. Always: Try to parse a storage status response from the raw packet bytes.
           Storage responses can arrive at any time, including spontaneously. We always
           check because `get_storage_status()` resolves a Future on this data.

        2. Fragment Reassembly: Parse the outer fragment wrapper to extract the group
           index, sequence number, total count, and payload. Store fragments in
           `fragment_buffer[group_index][seq] = payload`. When all fragments for a
           group have arrived (len == num_frags), concatenate them in sequence order
           and pass the complete payload to `_handle_pendant_message`.
        """
        packet = list(bytes(data))
        self._try_parse_device_status(packet)

        parsed = self._parse_ble_packet(packet)
        if parsed is None:
            return

        index = parsed.get("index")
        seq = parsed.get("seq")
        num_frags = parsed.get("num_frags")
        payload = parsed.get("payload")

        # A malformed packet may be missing required fields — skip it rather
        # than crashing the notification handler with a KeyError.
        if any(v is None for v in [index, seq, num_frags, payload]):
            return

        self.fragment_buffer.setdefault(index, {})
        self.fragment_buffer[index][seq] = payload

        if len(self.fragment_buffer[index]) == num_frags:
            # All fragments for this group have arrived. Reassemble in sequence order.
            complete_payload: list[int] = []
            for fragment_index in range(num_frags):
                fragment = self.fragment_buffer[index].get(fragment_index)
                if fragment is not None:
                    complete_payload.extend(fragment)
            del self.fragment_buffer[index]

            # Only process audio data while in batch mode.
            if self.is_batch_mode:
                self._handle_pendant_message(complete_payload)

    def _handle_pendant_message(self, payload: list[int]) -> None:
        """
        Parses a fully reassembled pendant message looking for storage buffer data.

        The outer pendant message is a Protobuf-like structure. We walk its fields
        looking for field 2 with wire type 2 (length-delimited bytes), which contains
        a storage buffer message. All other fields are skipped.

        Field 2 (wire type 2) → calls `_handle_storage_buffer` with the nested bytes.
        Field N (wire type 0) → varint, skipped.
        Anything else         → single byte advance (safe fallback skip).
        """
        try:
            pos = 0
            while pos < len(payload):
                tag = payload[pos]
                field_num = tag >> 3
                wire_type = tag & 0x07
                pos += 1

                if wire_type == 2:
                    length, pos = self._decode_varint(payload, pos)
                    field_data = payload[pos : pos + length]
                    pos += length
                    if field_num == 2:
                        self._handle_storage_buffer(field_data)
                elif wire_type == 0:
                    _, pos = self._decode_varint(payload, pos)
                else:
                    pos += 1
        except Exception as error:
            self.logger.log("handle_pendant_message_error", error=str(error))

    def _handle_storage_buffer(self, storage_data: list[int]) -> None:
        """
        Parses a storage buffer message and appends the result to `completed_flash_pages`.

        A storage buffer message contains metadata about one flash page:
            Field 2 (varint): session ID
            Field 4 (varint): sequence number
            Field 5 (varint): flash page index (the page's position in flash memory)
            Field 6 (bytes):  flash page data (contains the actual Opus audio + metadata)

        After parsing the envelope fields, the flash page data is passed to two
        sub-parsers:
        - `_parse_flash_page_info`: extracts the page timestamp and recording markers
        - `_extract_opus_frames_from_flash_page`: extracts the raw Opus audio frames

        Pages without flash_page_data (metadata-only pages with no audio field) are
        still appended to completed_flash_pages with empty opus_frames. This matters:
        these pages advance the flash page index and must be counted toward progress
        to avoid the "stopped early" failure mode described in the communication guide.
        """
        try:
            pos = 0
            session = None
            sequence = None
            index = None
            flash_page_data = None

            while pos < len(storage_data):
                tag = storage_data[pos]
                field_num = tag >> 3
                wire_type = tag & 0x07
                pos += 1

                if wire_type == 0:
                    value, pos = self._decode_varint(storage_data, pos)
                    if field_num == 2:
                        session = value
                    elif field_num == 4:
                        sequence = value
                    elif field_num == 5:
                        index = value
                elif wire_type == 2:
                    length, pos = self._decode_varint(storage_data, pos)
                    if field_num == 6:
                        flash_page_data = storage_data[pos : pos + length]
                    pos += length
                else:
                    pos += 1

            if not flash_page_data:
                return

            page_info = self._parse_flash_page_info(flash_page_data)
            opus_frames = self._extract_opus_frames_from_flash_page(flash_page_data)

            self.completed_flash_pages.append(
                {
                    "opus_frames": opus_frames,
                    "timestamp_ms": page_info.get("timestamp_ms"),
                    "did_start_recording": page_info.get("did_start_recording", False),
                    "did_stop_recording": page_info.get("did_stop_recording", False),
                    "session": session,
                    "seq": sequence,
                    "index": index,
                }
            )
        except Exception as error:
            self.logger.log("handle_storage_buffer_error", error=str(error))

    def _parse_flash_page_info(self, flash_page_data: list[int]) -> dict[str, Any]:
        """
        Extracts metadata from a raw flash page payload.

        Flash page structure (simplified):
            Byte 0:    0x08 tag (field 1, varint) — page timestamp in ms
            Byte 1+:   varint timestamp value
            ...
            0x1A tag (field 3, bytes) — recording chunk wrapper
                0x12 tag (field 2, bytes) — audio sub-message
                    0x40 tag (field 8, varint) — did_start_recording flag
                    0x48 tag (field 9, varint) — did_stop_recording flag

        Returns a dict with:
            timestamp_ms        — Unix epoch milliseconds for this page
            did_start_recording — True if a recording session started on this page
            did_stop_recording  — True if a recording session ended on this page
        """
        result = {
            "timestamp_ms": 0,
            "did_start_recording": False,
            "did_stop_recording": False,
        }

        try:
            pos = 0
            if pos < len(flash_page_data) and flash_page_data[pos] == 0x08:
                pos += 1
                result["timestamp_ms"], pos = self._decode_varint(flash_page_data, pos)

            while pos < len(flash_page_data) - 2:
                if flash_page_data[pos] != 0x1A:
                    pos += 1
                    continue

                pos += 1
                chunk_length, pos = self._decode_varint(flash_page_data, pos)
                chunk_end = pos + chunk_length

                while pos < chunk_end - 1:
                    marker = flash_page_data[pos]

                    if marker == 0x12:
                        pos += 1
                        audio_length, pos = self._decode_varint(flash_page_data, pos)
                        audio_end = pos + audio_length
                        while pos < audio_end - 1:
                            audio_marker = flash_page_data[pos]
                            pos += 1
                            if audio_marker == 0x40 and pos < audio_end:
                                result["did_start_recording"] = (flash_page_data[pos] != 0)
                                pos += 1
                            elif audio_marker == 0x48 and pos < audio_end:
                                result["did_stop_recording"] = (flash_page_data[pos] != 0)
                                pos += 1
                        continue

                    pos += 1
        except Exception as e:
            self.logger.log("parse_flash_page_error", error=str(e))
            return result

        return result

    def _extract_opus_frames_from_flash_page(
        self, flash_page_data: list[int]
    ) -> list[list[int]]:
        """
        Extracts all raw Opus audio frames from a flash page payload.

        Flash pages contain Opus frames nested inside several layers of Protobuf-like
        wrappers. The outer structure contains metadata (timestamp, flags) at the top
        level. Audio data is found in field 3 (0x1A) wrappers, and within those, in
        field 2 (0x12) audio sub-messages. The actual Opus frames are the leaf-level
        byte fields inside the audio sub-messages.

        The extraction skips the timestamp field (0x08) and an optional field 2 varint
        at the top level, then walks the 0x1A chunks, enters the 0x12 audio blocks,
        and calls `_extract_opus_recursive` to recursively descend the nested structure
        to find the actual compressed Opus frame bytes.

        Returns a list of Opus frames, where each frame is a list of raw bytes.
        """
        frames: list[list[int]] = []
        try:
            pos = 0

            # Skip the page timestamp field (field 1, varint) if present.
            if pos < len(flash_page_data) and flash_page_data[pos] == 0x08:
                pos += 1
                _, pos = self._decode_varint(flash_page_data, pos)

            # Skip an optional field 2 varint at the top level.
            if pos < len(flash_page_data) and flash_page_data[pos] == 0x10:
                pos += 1
                _, pos = self._decode_varint(flash_page_data, pos)

            # Walk through the page data looking for 0x1A chunk wrappers.
            while pos < len(flash_page_data) - 2:
                if flash_page_data[pos] != 0x1A:
                    pos += 1
                    continue

                pos += 1
                wrapper_length, pos = self._decode_varint(flash_page_data, pos)
                wrapper_end = pos + wrapper_length
                if wrapper_end > len(flash_page_data):
                    break

                # Inside the 0x1A wrapper, look for 0x08 metadata and 0x12 audio blocks.
                while pos < wrapper_end - 1:
                    marker = flash_page_data[pos]

                    if marker == 0x08:
                        # Skip chunk-level varint field.
                        pos += 1
                        _, pos = self._decode_varint(flash_page_data, pos)
                        continue

                    if marker == 0x12:
                        # Found an audio sub-message. Recursively extract Opus frames.
                        pos += 1
                        audio_length, pos = self._decode_varint(flash_page_data, pos)
                        audio_end = pos + audio_length
                        if audio_end > len(flash_page_data):
                            pos = wrapper_end
                            break

                        self._extract_opus_recursive(
                            flash_page_data, pos, audio_end, frames
                        )
                        pos = audio_end
                        continue

                    # Unknown field — skip it by wire type.
                    wire_type = marker & 0x07
                    pos += 1
                    if wire_type == 0:
                        _, pos = self._decode_varint(flash_page_data, pos)
                    elif wire_type == 2:
                        skip_length, pos = self._decode_varint(flash_page_data, pos)
                        pos += skip_length

                pos = wrapper_end
        except Exception as error:
            self.logger.log("extract_opus_frames_error", error=str(error))

        return frames

    def _is_protobuf_wrapper(self, payload: list[int]) -> bool:
        """
        Heuristic check: does this byte sequence cleanly parse as a Protobuf message?

        Used by `_extract_opus_recursive` to decide whether a length-delimited field
        contains a nested Protobuf wrapper (and should be recursed into) or raw Opus
        audio bytes (and should be collected as a frame).

        The heuristic: walk every field tag and skip the corresponding value bytes.
        If the parse completes at EXACTLY the end of the payload, it's a valid wrapper.
        If it fails or ends early, it's raw audio. Additionally, we reject any message
        with field numbers > 15, because the Limitless protocol uses small field numbers
        (1–10), while raw Opus data is high-entropy and will generate large "field numbers"
        when misinterpreted as Protobuf tags.
        """
        try:
            pos = 0
            while pos < len(payload):
                tag = payload[pos]
                wire_type = tag & 0x07
                field_num = tag >> 3
                pos += 1

                # Limitless wrappers use low field numbers (1-10).
                # Random Opus audio entropy will generate wild field numbers.
                if field_num == 0 or field_num > 15:
                    return False

                if wire_type == 0:
                    _, pos = self._decode_varint(payload, pos)
                elif wire_type == 1:
                    pos += 8
                elif wire_type == 2:
                    length, pos = self._decode_varint(payload, pos)
                    pos += length
                elif wire_type == 5:
                    pos += 4
                else:
                    return False
            # If it cleanly parses to exactly the end of the payload, it's a wrapper.
            return pos == len(payload)
        except Exception:
            return False

    def _extract_opus_recursive(
        self, data: list[int], start: int, end: int, frames: list[list[int]]
    ) -> None:
        """
        Recursively descends Protobuf-like nested structures to find Opus frames.

        For each length-delimited (wire type 2) field in the range [start, end):
        - If the field data (length > 10 and cleanly parses as Protobuf) → recurse.
        - If the field data is 10–200 bytes and does NOT parse as Protobuf → it's
          a raw Opus frame. Collect it.
        - All varint fields (wire type 0) are skipped.
        - Any other wire type breaks the loop (end of valid data).

        The 10–200 byte size range reflects expected Opus frame sizes for 16kHz mono
        audio at the pendant's encoding settings. Frames outside this range are
        ignored as they likely represent corrupt or non-audio data.
        """
        pos = start
        while pos < end - 1:
            tag = data[pos]
            wire_type = tag & 0x07
            pos += 1

            if wire_type == 2:
                length, pos = self._decode_varint(data, pos)
                if length > 0 and pos + length <= end:
                    field_data = data[pos : pos + length]

                    # If it perfectly parses as a nested Protobuf structure, open it.
                    # If it fails to parse as Protobuf, it is the raw Opus audio frame.
                    if length > 10 and self._is_protobuf_wrapper(field_data):
                        self._extract_opus_recursive(data, pos, pos + length, frames)
                    elif 10 <= length <= 200:
                        frames.append(field_data)
                pos += length

            elif wire_type == 0:
                _, pos = self._decode_varint(data, pos)
            else:
                break


    # ==========================================
    # STORAGE STATUS PARSING
    # ==========================================
    # The storage status response is buried inside several layers of nested
    # Protobuf-like wrappers. The parsing chain is:
    #
    #   _try_parse_device_status   — called on every raw RX packet
    #     → _extract_storage_state_robust — walks the outer packet for field 4
    #       → _parse_envelope       — walks field 4 for field 5
    #         → _parse_device_status — walks field 5 for field 5 (again, nested)
    #           → _parse_storage_state — extracts the actual flash page fields
    #
    # This deep nesting mirrors the pendant's actual message hierarchy.

    def _try_parse_device_status(self, data: list[int]) -> None:
        """
        Attempts to extract a storage status from a raw RX packet.

        Called on EVERY incoming notification (not just in response to our status
        request), because the pendant can send status updates spontaneously.
        If a valid storage state is found, it's stored in `self.storage_state` and
        any pending `storage_state_future` is resolved.
        """
        state = self._extract_storage_state_robust(data)
        if state:
            self.storage_state = state
            if self.storage_state_future and not self.storage_state_future.done():
                self.storage_state_future.set_result(state)

    def _extract_storage_state_robust(self, packet: list[int]) -> dict[str, int] | None:
        """
        Walks the outermost packet wrapper looking for the storage response envelope.
        The storage response is at field 4 (wire type 2) of the top-level packet.
        """
        try:
            pos = 0
            while pos < len(packet):
                tag = packet[pos]
                pos += 1
                wire = tag & 0x07
                if wire == 2:
                    length, pos = self._decode_varint(packet, pos)
                    if (tag >> 3) == 4:
                        return self._parse_envelope(packet, pos, pos + length)
                    pos += length
                elif wire == 0:
                    _, pos = self._decode_varint(packet, pos)
                elif wire == 1:
                    pos += 8
                elif wire == 5:
                    pos += 4
                else:
                    break
        except Exception:
            pass
        return None

    def _parse_envelope(self, data: list[int], start: int, end: int) -> dict[str, int] | None:
        """
        Parses the response envelope, looking for the device status message at field 5.
        """
        pos = start
        while pos < end:
            tag = data[pos]
            pos += 1
            wire = tag & 0x07
            if wire == 2:
                length, pos = self._decode_varint(data, pos)
                if (tag >> 3) == 5:
                    return self._parse_device_status(data, pos, pos + length)
                pos += length
            elif wire == 0:
                _, pos = self._decode_varint(data, pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        return None

    def _parse_device_status(self, data: list[int], start: int, end: int) -> dict[str, int] | None:
        """
        Parses the device status message, looking for the storage state sub-message at field 5.
        """
        pos = start
        while pos < end:
            tag = data[pos]
            pos += 1
            wire = tag & 0x07
            if wire == 2:
                length, pos = self._decode_varint(data, pos)
                if (tag >> 3) == 5:
                    return self._parse_storage_state(data, pos, pos + length)
                pos += length
            elif wire == 0:
                _, pos = self._decode_varint(data, pos)
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        return None

    def _parse_storage_state(self, data: list[int], start: int, end: int) -> dict[str, int] | None:
        """
        Extracts the actual flash page range values from the storage state message.

        Field mapping (all varint / wire type 0):
            Field 1 → oldest_flash_page    (lowest unacknowledged page)
            Field 2 → newest_flash_page    (highest recorded page)
            Field 3 → current_storage_session
            Field 4 → free_capture_pages   (remaining flash capacity)
            Field 5 → total_capture_pages  (total flash capacity)

        Returns None if no fields were found (not a valid storage state message).
        """
        state: dict[str, int] = {}
        pos = start
        while pos < end:
            tag = data[pos]
            pos += 1
            wire = tag & 0x07
            field = tag >> 3
            if wire == 0:
                val, pos = self._decode_varint(data, pos)
                if field == 1:
                    state["oldest_flash_page"] = val
                elif field == 2:
                    state["newest_flash_page"] = val
                elif field == 3:
                    state["current_storage_session"] = val
                elif field == 4:
                    state["free_capture_pages"] = val
                elif field == 5:
                    state["total_capture_pages"] = val
            elif wire == 2:
                length, pos = self._decode_varint(data, pos)
                pos += length
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                break
        return state if state else None

    def _parse_ble_packet(self, data: list[int]) -> dict[str, Any] | None:
        """
        Parses the outer BLE fragment wrapper from a raw RX notification payload.

        Fragment wrapper fields:
            Field 1 (varint): group_index   — which logical message this belongs to
            Field 2 (varint): sequence      — position within the group (0-based)
            Field 3 (varint): num_frags     — total fragments in this group
            Field 4 (bytes):  payload       — fragment payload bytes

        Returns None if the packet doesn't match the expected structure.
        """
        try:
            pos = 0
            index = None
            seq = 0
            num_frags = None
            payload = None

            while pos < len(data):
                tag = data[pos]
                field_num = tag >> 3
                wire_type = tag & 0x07
                pos += 1

                if wire_type == 0:
                    value, pos = self._decode_varint(data, pos)
                    if field_num == 1:
                        index = value
                    elif field_num == 2:
                        seq = value
                    elif field_num == 3:
                        num_frags = value
                elif wire_type == 2:
                    length, pos = self._decode_varint(data, pos)
                    if field_num == 4:
                        payload = data[pos : pos + length]
                    pos += length
                else:
                    break

            if index is not None and num_frags is not None and payload is not None:
                return {
                    "index": index,
                    "seq": seq,
                    "num_frags": num_frags,
                    "payload": payload,
                }
        except Exception:
            return None

        return None


    # ==========================================
    # PROTOBUF-LIKE ENCODING / DECODING
    # ==========================================
    # Protocol Buffers use "varints" — variable-length integers where each byte
    # contributes 7 bits of value data. The most-significant bit (0x80) is a
    # "continuation bit": 1 means more bytes follow, 0 means this is the last byte.
    # Small integers (< 128) encode in 1 byte; larger integers use more bytes.
    # Wire types: 0 = varint, 1 = 64-bit, 2 = length-delimited (bytes), 5 = 32-bit.
    # A field tag encodes both the field number and wire type: (field_num << 3) | wire_type

    def _encode_varint(self, value: int) -> list[int]:
        """Encodes an integer as a Protobuf varint (little-endian, 7 bits per byte)."""
        result: list[int] = []
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)  # 7 bits + continuation bit
            value >>= 7
        result.append(value & 0x7F)
        return result or [0]

    def _decode_varint(self, data: list[int], pos: int) -> tuple[int, int]:
        """Decodes a Protobuf varint from `data` at `pos`. Returns (value, new_pos)."""
        result = 0
        shift = 0
        while pos < len(data):
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift  # Take the 7 value bits
            if (byte & 0x80) == 0:             # Continuation bit is clear — done
                break
            shift += 7
        return result, pos

    def _encode_field(self, field_num: int, wire_type: int, value: list[int]) -> list[int]:
        """Encodes a Protobuf field tag + value bytes."""
        tag = (field_num << 3) | wire_type
        return [*self._encode_varint(tag), *value]

    def _encode_bytes_field(self, field_num: int, data: list[int]) -> list[int]:
        """Encodes a length-delimited (wire type 2) field: tag + varint length + data."""
        return self._encode_field(field_num, 2, [*self._encode_varint(len(data)), *data])

    def _encode_message(self, field_num: int, message_bytes: list[int]) -> list[int]:
        """Encodes a nested message as a length-delimited field."""
        return self._encode_bytes_field(field_num, message_bytes)

    def _encode_int32_field(self, field_num: int, value: int) -> list[int]:
        """Encodes an int32 as a varint field (wire type 0)."""
        return self._encode_field(field_num, 0, self._encode_varint(value))

    def _encode_int64_field(self, field_num: int, value: int) -> list[int]:
        """Encodes an int64 as a varint field (wire type 0). Identical encoding to int32."""
        return self._encode_field(field_num, 0, self._encode_varint(value))

    def _encode_ble_wrapper(self, payload: list[int]) -> bytes:
        """
        Wraps a command payload in the outer BLE message envelope.

        Every command written to TX uses this wrapper:
            Field 1: message_index (auto-incremented — acts as a write sequence counter)
            Field 2: constant 0
            Field 3: constant 1
            Field 4: the command payload bytes
        """
        message: list[int] = []
        message.extend(self._encode_int32_field(1, self.message_index))
        message.extend(self._encode_int32_field(2, 0))
        message.extend(self._encode_int32_field(3, 1))
        message.extend(self._encode_bytes_field(4, payload))
        self.message_index += 1
        return bytes(message)

    def _encode_request_data(self) -> list[int]:
        """
        Encodes a request envelope (field 30) with a monotonically increasing request_id.
        Appended to most commands as a transaction ID.
        """
        self.request_id += 1
        message: list[int] = []
        message.extend(self._encode_int64_field(1, self.request_id))
        message.extend(self._encode_field(2, 0, [0x00]))
        return self._encode_message(30, message)

    def _encode_set_current_time(self, timestamp_ms: int) -> bytes:
        """
        Encodes the "set current time" command (outer field 6).
        Sends the current Unix timestamp in milliseconds to the pendant,
        allowing it to assign accurate timestamps to newly recorded pages.
        """
        time_message = self._encode_int64_field(1, timestamp_ms)
        command = [*self._encode_message(6, time_message), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    def _encode_enable_data_stream(self, enable: bool = True) -> bytes:
        """
        Encodes the "stream/mode control" command (outer field 8).
        Used during initialization to activate the pendant's data output.
            Field 1: 0 (stream control flag)
            Field 2: 1 = enable, 0 = disable
        """
        message: list[int] = []
        message.extend(self._encode_field(1, 0, [0x00]))
        message.extend(self._encode_field(2, 0, [0x01 if enable else 0x00]))
        command = [*self._encode_message(8, message), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    def _encode_acknowledge_processed_data(self, up_to_index: int) -> bytes:
        """
        Encodes the "acknowledge processed pages" command (outer field 7).
        Tells the pendant it can free flash pages up to and including `up_to_index`.
        This is DESTRUCTIVE — the pendant will overwrite acknowledged pages with new recordings.
        """
        ack_message = self._encode_int32_field(1, up_to_index)
        command = [*self._encode_message(7, ack_message), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    def _encode_get_device_status(self) -> bytes:
        """
        Encodes the "request storage status" command (outer field 21, empty payload).
        The pendant responds with a storage status notification on the RX characteristic,
        which is parsed by `_try_parse_device_status`.
        """
        command = [*self._encode_message(21, []), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    def _encode_download_flash_pages(self, batch_mode: bool, real_time: bool) -> bytes:
        """
        Encodes the batch/real-time mode control command (outer field 8).
        This command switches the pendant between two operating modes:
            batch_mode=True,  real_time=False → Download stored flash pages
            batch_mode=False, real_time=True  → Resume normal real-time streaming

        Field 1: batch_mode flag (1 = batch, 0 = normal)
        Field 2: real_time flag  (1 = streaming, 0 = batch)
        """
        message: list[int] = []
        message.extend(self._encode_field(1, 0, [0x01 if batch_mode else 0x00]))
        message.extend(self._encode_field(2, 0, [0x01 if real_time else 0x00]))
        command = [*self._encode_message(8, message), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)


# ==========================================
# CLI ARGUMENT PARSING
# ==========================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download stored audio from a Limitless Pendant.")
    parser.add_argument("--address", help="Connect to a specific BLE address or macOS device identifier.")
    parser.add_argument("--name", help="Only consider devices whose advertised name contains this string.")
    parser.add_argument("--scan-timeout", type=float, default=8.0, help="Seconds to scan before giving up.")
    parser.add_argument("--connect-timeout", type=float, default=15.0, help="Seconds to wait for the BLE connection.")
    parser.add_argument("--no-pair", action="store_true", help="Do not ask BLE to pair/bond when connecting.")
    parser.add_argument("--output-dir", help="Directory to write downloaded audio batches into.")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="Append run details to this log file.")
    parser.add_argument("--persist-seconds", type=float, default=90.0, help="Save an in-progress batch to disk after this much idle accumulation time.")
    parser.add_argument("--no-ack", action="store_true", help="Do not acknowledge processed pages back to the pendant.")
    parser.add_argument("--keep-on-device", dest="no_ack", action="store_true", help="Keep downloaded audio on the pendant.")
    parser.add_argument("--dump-services", action="store_true", help="Print discovered services and characteristics.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress bar output.")
    return parser.parse_args(argv)


# ==========================================
# BLE DEVICE DISCOVERY UTILITIES
# ==========================================

def normalize_uuid(uuid: str) -> str:
    """
    Normalizes a UUID string to the full 36-character lowercase format.
    Handles 4-digit (short), 8-digit, and full 36-character UUID inputs.
    """
    lowered = str(uuid).strip().lower()
    if len(lowered) == 4:
        return f"0000{lowered}{BLUETOOTH_BASE_UUID}"
    if len(lowered) == 8:
        return f"{lowered}{BLUETOOTH_BASE_UUID}"
    return lowered

def iter_services(services: Any) -> Iterable[Any]:
    """
    Iterates over a bleak services collection regardless of its internal structure.
    bleak may return services as a dict or as a direct iterable depending on the version.
    """
    service_map = getattr(services, "services", None)
    if isinstance(service_map, dict):
        return service_map.values()
    if services is None:
        return ()
    return services

def get_device_name(device: Any, advertisement: Any) -> str:
    """
    Returns the best available display name for a discovered BLE device.
    Prefers the advertisement's `local_name` over the device's generic `name`.
    The pendant may advertise as "Pendant" rather than "Limitless Pendant".
    """
    local_name = getattr(advertisement, "local_name", None)
    device_name = getattr(device, "name", None)
    return local_name or device_name or "Unknown"

def get_advertised_services(advertisement: Any) -> set[str]:
    """Returns the set of normalized service UUIDs advertised by a BLE device."""
    service_uuids = getattr(advertisement, "service_uuids", None) or []
    return {normalize_uuid(uuid) for uuid in service_uuids}

def score_candidate(name: str, services: set[str]) -> int:
    """
    Scores a discovered BLE device as a Limitless Pendant candidate.

    Scoring priority:
        100 — Advertises the Limitless custom service UUID (most reliable signal)
         80 — Device name contains "limitless"
         60 — Device name contains "pendant"
          0 — No match (filtered out)

    A score > 0 is required for a device to be considered a candidate.
    """
    lowered_name = name.lower()
    score = 0
    if LIMITLESS_SERVICE_UUID in services:
        score += 100
    if "limitless" in lowered_name:
        score += 80
    if "pendant" in lowered_name:
        score += 60
    return score

async def discover_candidates(args: argparse.Namespace, logger: RunLogger) -> list[DeviceCandidate]:
    """
    Scans for nearby BLE devices and returns all Limitless Pendant candidates.

    If `--address` is specified, only that device is considered (by exact address match).
    If `--name` is specified, only devices containing that string are considered.
    All candidates are scored and sorted: highest score first, then alphabetically by name.
    """
    if BleakScanner is None:
        raise RuntimeError("Missing dependency: install BLE support with `python3 -m pip install bleak`.")

    try:
        discoveries = await BleakScanner.discover(timeout=args.scan_timeout, return_adv=True)
    except BleakBluetoothNotAvailableError as error:
        raise RuntimeError("Bluetooth is unavailable.") from error

    requested_name = args.name.lower() if args.name else None
    requested_address = args.address.lower() if args.address else None
    candidates: list[DeviceCandidate] = []

    for device, advertisement in discoveries.values():
        device_address = str(getattr(device, "address", "")).lower()
        name = get_device_name(device, advertisement)
        services = get_advertised_services(advertisement)

        if requested_address and device_address != requested_address:
            continue
        if requested_name and requested_name not in name.lower():
            continue

        score = score_candidate(name, services)
        if requested_address and device_address == requested_address and score <= 0:
            score = 1  # Force inclusion when explicitly requested by address

        if score > 0:
            candidates.append(
                DeviceCandidate(
                    device=device,
                    advertisement=advertisement,
                    name=name,
                    services=services,
                    score=score,
                )
            )

    candidates.sort(key=lambda c: (-c.score, c.name.lower(), str(c.device.address).lower()))
    return candidates

def choose_candidate(candidates: list[DeviceCandidate], requested_address: str | None) -> DeviceCandidate:
    """
    Selects the best candidate from the sorted list. Raises if none found.
    With an explicit address, the top match is always used. Without an address,
    the highest-scored candidate is selected.
    """
    if not candidates:
        raise RuntimeError("No matching Limitless Pendant found.")
    if requested_address:
        return candidates[0]
    return candidates[0]

async def ensure_services(client: BleakClientType) -> Any:
    """
    Returns the GATT service collection from a connected bleak client.
    Handles both bleak versions that auto-populate `client.services` and
    older versions that require an explicit `get_services()` call.
    """
    services = getattr(client, "services", None)
    if services:
        return services
    get_services = getattr(client, "get_services", None)
    if callable(get_services):
        return await cast(Any, get_services)()
    return getattr(client, "services", None)

def describe_services(services: Any) -> list[dict[str, Any]]:
    """Returns a structured list of service/characteristic descriptions for logging."""
    result: list[dict[str, Any]] = []
    for service in iter_services(services):
        result.append({
            "uuid": normalize_uuid(getattr(service, "uuid", "")),
            "description": getattr(service, "description", "") or "Unknown",
            "characteristics": [
                {
                    "uuid": normalize_uuid(getattr(c, "uuid", "")),
                    "description": getattr(c, "description", "") or "Unknown",
                    "properties": list(getattr(c, "properties", [])),
                }
                for c in getattr(service, "characteristics", [])
            ],
        })
    return result

def dump_services(services: Any) -> None:
    """Prints all discovered services and characteristics to stdout (--dump-services flag)."""
    print("\nDiscovered services:")
    for service in iter_services(services):
        print(f"- Service {service.uuid}")
        for characteristic in getattr(service, "characteristics", []):
            properties = ", ".join(getattr(characteristic, "properties", []))
            print(f"    - {characteristic.uuid} [{properties}]")

def format_duration(total_seconds: int | None) -> str:
    """Formats a duration in seconds as HH:MM:SS or MM:SS."""
    if total_seconds is None:
        return "--:--"
    minutes, seconds = divmod(max(total_seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def build_output_dir(requested_path: str | None) -> Path:
    """Resolves and creates the output directory for .bin files."""
    if requested_path:
        output_dir = Path(requested_path).expanduser()
    else:
        output_dir = DEFAULT_OUTPUT_ROOT
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


# ==========================================
# CONVERSATION STREAMER
# ==========================================

#### REPLACED ORIGINAL MEMORY-BASED METHOD (BELOW)
class ConversationStreamer:
    """
    Streams Opus audio frames directly to disk as they arrive, in real time.

    PROBLEM WITH THE OLD APPROACH:
    The original implementation accumulated all frames in a RAM list and wrote
    the file only at the very end of a download. On large backlogs (30+ minutes
    of audio), this consumed significant memory. More critically, if the Bluetooth
    connection dropped mid-download, ALL collected audio was lost.

    THIS APPROACH:
    Every batch of frames received from the pendant is immediately appended to
    a `.part` file on disk (with fsync to force the OS to flush the write cache).
    This means audio is persisted continuously — even a mid-download crash loses
    at most a few seconds of audio rather than the entire session.

    THE .PART FILE PATTERN:
    Files are written to `active_recording.bin.part` while in progress. The `.part`
    extension prevents downstream tools (convert.py file watcher) from treating an
    incomplete file as ready. Only when a session is complete (or a gap is detected)
    is the `.part` atomically renamed to its final `.bin` name.

    CRASH RECOVERY:
    If a previous run crashed and left `.part` files behind, they are detected at
    startup and flagged for manual inspection. They are NOT automatically deleted
    because they may contain valid audio data.

    NAMING CONVENTION:
    The final `.bin` filename encodes the recording's start and end time:
        "04-01-2026 02.30PM to 02.45PM.bin"
    This name is derived from the page timestamps received during download.
    If no valid timestamp was available, the file is named:
        "UNKNOWN_TIME__900s__001.bin"
    """
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.part_path: Path | None = None
        self.file_handle = None
        self.start_timestamp_ms: int | None = None
        self.frame_count: int = 0

    def append_frames(self, frames: list[list[int]], timestamp_ms: int | None):
        """
        Appends a batch of Opus frames to the active .part file.

        Opens the .part file on the first call. Each frame is written as a
        4-byte little-endian length prefix followed by the raw frame bytes
        (the same format that convert.py reads). After writing, fsync() forces
        the OS to flush the write buffer to physical storage immediately.
        """
        if not frames:
            return

        # If this is the first batch, open the .part file.
        if self.file_handle is None:
            self.start_timestamp_ms = timestamp_ms
            self.part_path = self.output_dir / "active_recording.bin.part"

            # Avoid overwriting a stale .part file from a previous hard crash.
            counter = 1
            while self.part_path.exists():
                self.part_path = self.output_dir / f"active_recording_{counter}.bin.part"
                counter += 1

            self.file_handle = self.part_path.open("wb")

        # Stream directly to the hard drive.
        for frame in frames:
            self.file_handle.write(struct.pack("<I", len(frame)))  # 4-byte length prefix
            self.file_handle.write(bytes(frame))                    # Raw Opus bytes
            self.frame_count += 1

        # Force OS to write to physical storage immediately.
        self.file_handle.flush()
        os.fsync(self.file_handle.fileno())

    def commit(self, batch_number: int) -> Path | None:
        """
        Finalizes the current .part file and renames it to its permanent .bin name.

        The filename includes the start time (from the first page's timestamp) and
        the calculated end time (start + frame_count / 50 frames per second).
        After rename, all state is reset so the streamer is ready for the next session.

        Returns the final Path, or None if there was nothing to commit.
        """
        if self.file_handle is None or self.part_path is None:
            return None

        self.file_handle.close()
        self.file_handle = None

        duration_sec = int(self.frame_count / FRAMES_PER_SECOND)

        if self.start_timestamp_ms is not None:
            start_dt = datetime.fromtimestamp(self.start_timestamp_ms / 1000)
            end_dt = datetime.fromtimestamp((self.start_timestamp_ms / 1000) + duration_sec)

            start_str = start_dt.strftime("%m-%d-%Y %I.%M%p")
            end_str = end_dt.strftime("%I.%M%p")
            base_name = f"{start_str} to {end_str}"
        else:
            base_name = f"UNKNOWN_TIME__{duration_sec}s__{batch_number:03d}"

        file_name = f"{base_name}.bin"
        final_path = self.output_dir / file_name

        # Avoid overwriting an existing file from an earlier run.
        counter = 1
        while final_path.exists():
            file_name = f"{base_name} ({counter}).bin"
            final_path = self.output_dir / file_name
            counter += 1

        # Atomic rename — the .bin file appears in one operation, never partially.
        self.part_path.rename(final_path)

        # Reset for the next session.
        self.part_path = None
        self.start_timestamp_ms = None
        self.frame_count = 0

        return final_path

    def has_data(self) -> bool:
        """Returns True if any frames have been written to the current .part file."""
        return self.frame_count > 0

    def discard(self) -> None:
        """
        Closes and deletes the active .part file, discarding all uncommitted audio.

        Used when a BLE stall fires before we've reached the natural-break zone
        (< 1,500 pages into the chunk). Rather than committing a tiny garbage file,
        we drop the partial data and let the pendant re-serve those pages next time.
        """
        if self.file_handle is not None:
            self.file_handle.close()
            self.file_handle = None
        if self.part_path is not None and self.part_path.exists():
            self.part_path.unlink()
            self.part_path = None
        self.start_timestamp_ms = None
        self.frame_count = 0

def load_downloaded_pages(output_dir: Path) -> set[str]:
    """
    Returns a set of all .bin filenames already present in the output directory.
    Used to avoid double-counting files that existed from a previous run when
    reporting download statistics.
    """
    seen = set()
    for f in output_dir.glob("*.bin"):
        seen.add(f.name)
    return seen


# ==========================================
# MAIN DOWNLOAD LOOP
# ==========================================

async def download_audio(
    protocol: LimitlessProtocol,
    start_page: int,
    end_page: int,
    output_dir: Path,
    logger: RunLogger,
    persist_seconds: float,
    acknowledge: bool,
    batch_number_offset: int = 0,
    quiet: bool = False,
    safe_page_limit: int = 2000
) -> tuple[list[dict[str, Any]], int | None, int, int]:
    """
    The core download loop. Receives flash pages from the pendant and writes them
    to .bin files via ConversationStreamer.

    ARGUMENTS:
        start_page / end_page — the flash page range to download (inclusive).
            These are snapshotted BEFORE download begins. The pendant may continue
            recording new pages during the run; those are ignored (boundary clamped).
        safe_page_limit — maximum pages to download per call (default 2000).
            When reached, the function exits with partial data. pendant_sync.py
            will call download.py again immediately to pull the next chunk (exit code 3).
        acknowledge — if True, sends ACKs to the pendant after each file is saved
            and at the end of the run. ACKs free flash pages permanently (destructive).

    THE SORTING CONVEYOR BELT:
        BLE notifications do not always arrive in page-index order. Before processing
        the queue each loop iteration, completed_flash_pages is sorted by index. This
        guarantees chronological frame order in the output files.

    GAP DETECTION:
        If the timestamp of a page differs from the expected cumulative time by more
        than 60 seconds, a recording gap is assumed. The current .part file is committed
        and a new one is started. This produces separate .bin files for separate
        recording sessions, rather than incorrectly concatenating them.

    THE BOUNDARY CLAMP:
        Pages with index > end_page are discarded. This prevents audio recorded AFTER
        the download started from being mixed into this run's files.

    PHANTOM PAGE DETECTION:
        If the actual number of downloaded pages is < 50 AND we downloaded 0 pages,
        the storage status was likely inaccurate (a "phantom gap" — the pendant
        reported pages that weren't actually available). We return 0 files and the
        caller exits with code 0 rather than triggering a BT reset.

    STALL DETECTION:
        If no new pages arrive for 20 consecutive seconds, the data stream is assumed
        to have stalled (common with BT congestion). The loop exits with whatever
        was collected so far. The final ACK is skipped in this case to avoid
        acknowledging pages that may not have been fully received.

    RETURNS:
        (saved_files, last_processed_index, actual_pages_downloaded, total_pages)
    """
    total_pages = max(end_page - start_page + 1, 0)
    progress = ProgressBar(total_pages, quiet=quiet)
    batch_number = batch_number_offset
    files_saved = 0
    last_processed_index: int | None = None
    sync_started_at = time.monotonic()
    last_save_at = time.monotonic()
    streamer = ConversationStreamer(output_dir)
    batch_min_timestamp: int | None = None
    saved_files: list[dict[str, Any]] = []
    already_downloaded = load_downloaded_pages(output_dir)
    requested_range_complete = False

    # Warn about any stale .part files from previous crashes.
    orphaned_parts = list(output_dir.glob("*.part"))
    if orphaned_parts:
        if not quiet:
            print(f"\n[!] Notice: Found {len(orphaned_parts)} orphaned .part file(s) from a previous crash. You may want to manually delete them.", flush=True)
        logger.log("orphaned_part_files_detected", count=len(orphaned_parts), files=str([f.name for f in orphaned_parts]))

    actual_pages_downloaded = 0
    actual_start_page = start_page
    last_data_received_at = time.monotonic()
    current_file_start_page = None
    skip_final_ack = False
    discard_streamer_tail = False  # True when a stall fires before the 1,500-page safe zone

    # Read the last ACKed page index from the state file, if present.
    # If the pendant's streaming cursor regressed since the last chunk,
    # we silently skip those already-ACKed pages to avoid duplicate audio.
    skip_before_page = 0
    state_file = output_dir / ".ack_state"
    if state_file.exists():
        try:
            skip_before_page = int(state_file.read_text().strip())
        except Exception:
            pass
    skip_message_logged = False  # Printed lazily after "First Page Sent" is known

    await protocol.enable_batch_mode()

    # --- 1. PRIME THE PUMP & CATCH HARDWARE BURPS ---
    # The pendant immediately flushes both its oldest AND newest real-time pages
    # the instant batch mode is enabled — before the true sequential stream begins.
    # We wait 0.5s to catch these early pages so we can identify the true stream start
    # and purge the spurious newest-page notification before it tricks the completion check.
    await asyncio.sleep(0.5)

    first_page_logged = False

    if protocol.completed_flash_pages:
        # Find the minimum (oldest) page index in whatever arrived during the sleep.
        initial_min_index = min(
            (p.get("index") for p in protocol.completed_flash_pages if isinstance(p.get("index"), int)),
            default=None
        )

        if initial_min_index is not None:
            if not quiet:
                print(f"First Page Sent: {initial_min_index}", flush=True)
            first_page_logged = True

            # Recalibrate if the stream started later than expected.
            if initial_min_index > start_page:
                actual_start_page = initial_min_index
                total_pages = max(end_page - actual_start_page + 1, 1)
                progress.total_pages = total_pages

                # Kill switch: if the recalibrated window is tiny, the pendant's
                # storage report was a phantom — leave the pages to accumulate.
                if total_pages < 50:
                    return [], None, 0, total_pages
            else:
                actual_start_page = start_page

            # PURGE THE BURP: remove any pages that arrived wildly out of order
            # (i.e., the spurious newest-page notification). The pendant will resend
            # the real copy when the sequential stream legitimately reaches that page.
            protocol.completed_flash_pages = [
                p for p in protocol.completed_flash_pages
                if not (isinstance(p.get("index"), int) and p.get("index") > initial_min_index + 50)
            ]

    try:
        while True:
            # 2. THE SORTING CONVEYOR BELT
            # Sort the queue by index every loop to guarantee perfectly sequential processing.
            # Pages may arrive out of order due to BLE notification ordering — sorting here
            # ensures frames are written to the .bin files in the correct time sequence.
            protocol.completed_flash_pages.sort(key=lambda p: p.get("index", 0) if isinstance(p.get("index"), int) else 0)

            while protocol.completed_flash_pages:
                page = protocol.completed_flash_pages.pop(0)
                last_data_received_at = time.monotonic()

                page_index = page.get("index")
                opus_frames = page.get("opus_frames") or []
                raw_timestamp = page.get("timestamp_ms")

                # Filter out 1969 glitch timestamps.
                # The pendant occasionally emits a timestamp near epoch 0, which would
                # produce an incorrect filename like "12-31-1969 07.00PM". We treat any
                # timestamp before Nov 2023 as invalid and fall back to None.
                valid_timestamp = raw_timestamp if (isinstance(raw_timestamp, int) and raw_timestamp > MIN_VALID_TIMESTAMP_MS) else None

                # THE BOUNDARY CLAMP:
                # Discard pages that arrived after our snapshot end_page. The pendant
                # may have continued recording while we were downloading older pages.
                # Including those new pages would mix two different recording sessions.
                if end_page is not None and isinstance(page_index, int) and page_index > end_page:
                    continue

                actual_pages_downloaded += 1

                if isinstance(page_index, int):
                    # --- IN-LOOP FALLBACK: FIRST PAGE DETECTION ---
                    # If the 0.5s sleep didn't catch any pages (slow connection),
                    # detect and log the first page here instead.
                    if not first_page_logged:
                        if not quiet:
                            print(f"First Page Sent: {page_index}", flush=True)
                        first_page_logged = True

                        # Recalibrate if the first real page is far ahead of where we expected.
                        if last_processed_index is None and page_index > start_page:
                            actual_start_page = page_index
                            total_pages = max(end_page - actual_start_page + 1, 1)
                            progress.total_pages = total_pages

                            if total_pages < 50:
                                return [], None, 0, total_pages

                    # Skip pages already downloaded and ACKed in a previous chunk.
                    # The pendant's streaming cursor occasionally regresses from the
                    # ACKed boundary for unknown firmware reasons, causing overlap.
                    if page_index <= skip_before_page:
                        if not skip_message_logged and not quiet:
                            print(f"Skipping pages already ACKed (up to {skip_before_page})...", flush=True)
                            skip_message_logged = True
                        last_processed_index = page_index
                        continue

                    # Track the starting page index for this file's progress reporting.
                    # Must be after the skip check so skipped pages don't corrupt the range display.
                    if current_file_start_page is None:
                        current_file_start_page = page_index

                    # --- GAP DETECTION ---
                    # Compare this page's actual timestamp against the expected timestamp
                    # based on how many frames we've accumulated so far. If the gap exceeds
                    # 60 seconds, this page belongs to a different recording session.
                    # We commit the current file and start a new one.
                    gap_detected = False
                    if opus_frames:
                        if batch_min_timestamp is not None and valid_timestamp is not None and streamer.has_data():
                            expected_time_ms = batch_min_timestamp + int((streamer.frame_count / FRAMES_PER_SECOND) * 1000)
                            if abs(valid_timestamp - expected_time_ms) > 60_000:
                                # Don't split if this is the literal last page — that would
                                # create an empty file for the final few frames.
                                if page_index < end_page:
                                    gap_detected = True

                    if gap_detected:
                        # Commit the old file using the PREVIOUS page as the end bound.
                        batch_number += 1
                        duration_sec = int(streamer.frame_count / FRAMES_PER_SECOND)
                        file_path = streamer.commit(batch_number)

                        if file_path and file_path.name not in already_downloaded:
                            files_saved += 1
                            saved_files.append({"relative_path": file_path.name, "timestamp_ms": batch_min_timestamp})
                            if not quiet:
                                print()
                                print(f"File created from pages {current_file_start_page} - {last_processed_index} ({format_duration(duration_sec)})", flush=True)

                        if acknowledge and last_processed_index is not None:
                            try:
                                if protocol.client.is_connected:
                                    safe_ack_index = min(last_processed_index, end_page)
                                    await protocol.acknowledge_processed_data(safe_ack_index)
                                    try:
                                        (output_dir / ".ack_state").write_text(str(safe_ack_index))
                                    except Exception:
                                        pass  # Non-fatal
                            except Exception as error:
                                logger.log("ack_error", error=str(error))

                        # --- NATURAL BREAK ZONE ---
                        # If we're deep enough into a large chunk (>= 1,500 pages downloaded)
                        # and a natural recording gap just appeared, use this as the chunk
                        # boundary. Exit cleanly here rather than continuing to the hard
                        # 2,000-page limit. The current page (start of next session) is left
                        # un-ACKed — the pendant will serve it at the top of the next chunk.
                        if total_pages > safe_page_limit and actual_pages_downloaded >= 1500:
                            if not quiet:
                                print(f"\nGiving pendant a short rest... (natural gap found at page {actual_pages_downloaded})", flush=True)
                            requested_range_complete = True
                            break  # Break inner loop — outer loop sees requested_range_complete

                        # Otherwise: start a new file with the CURRENT page.
                        current_file_start_page = page_index
                        batch_min_timestamp = valid_timestamp
                        streamer.append_frames(opus_frames, batch_min_timestamp)
                    else:
                        # Normal append — add frames to the current ongoing file.
                        if opus_frames:
                            if batch_min_timestamp is None and valid_timestamp is not None:
                                batch_min_timestamp = valid_timestamp
                            streamer.append_frames(opus_frames, batch_min_timestamp)

                    # --- UPDATE PROGRESS ---
                    # IMPORTANT: We update last_processed_index for EVERY page, including
                    # metadata-only pages with no Opus frames. This is critical — without it,
                    # the download would stall when it encountered a metadata-only page because
                    # it would never advance toward the end_page target.
                    last_processed_index = page_index
                    processed_pages = min(max(last_processed_index - actual_start_page + 1, 0), total_pages)
                    progress.update(processed_pages, files_saved, sync_started_at)

                    if last_processed_index >= end_page:
                        requested_range_complete = True
                        break  # Break inner queue loop — we've processed the full range.

            # 2. EVALUATE OUTER LOOP BREAK CONDITIONS

            if requested_range_complete:
                # Successfully processed every page up to end_page. Clean exit.
                break

            if time.monotonic() - last_data_received_at > 20.0:
                # No pages have arrived in 20 seconds — the data stream has stalled.
                # Discard the active .part file (uncommitted tail) to avoid leaving an
                # orphaned partial file on disk. Any .bin files already committed this
                # session were mid-stream ACKed and are kept for conversion. Skip the
                # final ACK so the pendant re-serves the remaining un-ACKed pages on
                # the next connection.
                if not quiet:
                    print(f"\n[!] Data stream stalled ({actual_pages_downloaded} pages received). Discarding uncommitted tail — pendant will re-serve remaining pages on next connection.", flush=True)
                discard_streamer_tail = True
                skip_final_ack = True
                break

            if actual_pages_downloaded >= safe_page_limit:
                # Hard stop at the 2,000-page chunk limit (no natural gap was found
                # in the 1,500-2,000 zone). Save progress and signal the orchestrator
                # to run conversion, rest the pendant, then pull the next chunk.
                if not quiet:
                    print(f"\nGiving pendant a short rest... (chunk limit: {safe_page_limit} pages)", flush=True)
                break

            # Dropped the sleep to 0.1s for a highly responsive polling loop.
            await asyncio.sleep(0.1)

        # Commit any remaining frames that didn't trigger a gap split above.
        # Exception: if the stream stalled, discard the entire session — both the
        # uncommitted tail and any .bin files already committed during this run.
        # The pendant keeps those pages (no ACK was sent) and will re-serve them
        # in full on the next connection, ensuring clean audio boundaries.
        if discard_streamer_tail:
            # Discard only the active .part file (uncommitted tail). Any .bin files
            # already committed this session were mid-stream ACKed and belong to the
            # pendant's history — they proceed to conversion normally.
            streamer.discard()
        elif streamer.has_data():
            batch_number += 1
            duration_sec = int(streamer.frame_count / FRAMES_PER_SECOND)
            file_path = streamer.commit(batch_number)

            if file_path and file_path.name not in already_downloaded:
                files_saved += 1
                saved_files.append({"relative_path": file_path.name, "timestamp_ms": batch_min_timestamp})

            if not quiet:
                print()  # Drop down so we don't overwrite the progress bar
                print(f"File created from pages {current_file_start_page} - {last_processed_index} ({format_duration(duration_sec)})", flush=True)

        # Send the final cumulative ACK after all files are committed.
        # Clamped to end_page to avoid acknowledging pages recorded during this run.
        # Skipped if the stream stalled (skip_final_ack) to avoid partial acks.
        if acknowledge and last_processed_index is not None and not skip_final_ack:
            try:
                if protocol.client.is_connected:
                    safe_ack_index = min(last_processed_index, end_page)
                    await protocol.acknowledge_processed_data(safe_ack_index)
                    try:
                        (output_dir / ".ack_state").write_text(str(safe_ack_index))
                    except Exception:
                        pass  # Non-fatal
                    await asyncio.sleep(1.5)  # Brief pause to ensure the ACK is transmitted
            except Exception as error:
                logger.log("final_ack_error", error=str(error))

        if not quiet:
            progress.update(total_pages, files_saved, sync_started_at)
            print()

        return saved_files, last_processed_index, actual_pages_downloaded, total_pages

    finally:
        # ALWAYS restore real-time mode before disconnecting, even on exceptions.
        # If we leave the pendant in batch mode, it will not record live audio until
        # the next successful connection restores it.
        try:
            if protocol.client.is_connected:
                await protocol.enable_real_time_mode()
        except Exception as error:
            logger.log("real_time_mode_restore_error", error=str(error))

def read_status_log_hint(log_file: Path) -> str:
    return f"Log file: {log_file.resolve()}"

def is_insufficient_encryption_error(error: Exception) -> bool:
    """
    Returns True if the error indicates the pendant requires a paired/bonded BLE link.
    This happens when connecting to a pendant that has previously been paired and now
    requires encryption. The fix is to allow pairing (do not use --no-pair).
    """
    message = str(error).lower()
    return "encryption is insufficient" in message or "code=15" in message


# ==========================================
# ENTRY POINT
# ==========================================

async def main(argv: list[str] | None = None) -> int:
    """
    Main entry point. Connects to the pendant, downloads audio, and returns an exit code.

    EXIT CODE LOGIC:
        0 — last_processed_index >= newest_page: entire range downloaded successfully.
        1 — Unexpected Python exception (programming error).
        2 — BLE hardware error: disconnect, stall, or encryption failure.
        3 — Partial download: chunk limit reached, more pages remain on the pendant.
        4 — Pendant not found: out of range or powered off.

    PHANTOM PAGE DETECTION:
        If storage_status claims a large page range but the actual download produces
        < 50 pages AND actual_downloaded == 0, the pendant's reported page range was
        a ghost (stale or inaccurate metadata). We exit 0 rather than triggering a
        BT reset, because this is a data state issue, not a hardware issue.
    """
    args = parse_args(argv)
    log_file = Path(args.log_file).expanduser()
    logger = RunLogger(log_file)
    logger.log("run_started", argv=argv if argv is not None else sys.argv[1:])

    if BleakClient is None:
        raise RuntimeError("Missing bleak library.")

    if args.address:
        # Skip scanning — connect directly to the saved pendant address.
        # On macOS, this is a UUID-format identifier (not a MAC address).
        candidate_name = args.name or "Limitless Pendant"
        candidate_address = args.address
        candidate_device: Any = args.address
    else:
        # Scan for nearby BLE devices and pick the best Limitless Pendant candidate.
        candidates = await discover_candidates(args, logger)
        candidate = choose_candidate(candidates, args.address)
        candidate_name = candidate.name
        candidate_address = candidate.device.address
        candidate_device = candidate.device

    output_dir = build_output_dir(args.output_dir)
    overall_saved_files = []

    try:
        async with BleakClient(
            candidate_device,
            timeout=args.connect_timeout,
            pair=not args.no_pair,  # Allow bonding/pairing by default (required for custom protocol)
        ) as client:

            services = await ensure_services(client)
            protocol = LimitlessProtocol(client, logger)

            try:
                await protocol.start()
            except Exception as error:
                if is_insufficient_encryption_error(error):
                    raise RuntimeError("Encrypted BLE link required.") from error
                raise

            battery_level = await protocol.get_battery_level()
            if battery_level is not None:
                print(f"Battery Level: {battery_level}%", flush=True)

            await asyncio.sleep(1.0)

            # Request and snapshot the current storage state.
            # We snapshot newest_page NOW, before batch mode starts. The pendant may
            # continue recording new pages while we download older ones. We clamp our
            # download to this snapshot boundary to avoid mixing sessions.
            storage_status = await protocol.get_storage_status()
            if not storage_status:
                raise RuntimeError("Could not read storage status from the pendant.")

            oldest_page = int(storage_status.get("oldest_flash_page", 0))
            newest_page = int(storage_status.get("newest_flash_page", -1))
            storage_session = storage_status.get("current_storage_session")

            if newest_page < oldest_page:
                print("No unacknowledged data on the pendant. You're all caught up!", flush=True)
                return 0

            current_start_page = oldest_page
            total_pages = newest_page - oldest_page + 1

            estimated_seconds = max(int(round(total_pages * SECONDS_PER_FLASH_PAGE)), 0)

            print(f"Oldest Flash Page: {oldest_page}", flush=True)
            print(f"Newest Flash Page: {newest_page}", flush=True)
            if storage_session is not None:
                # Session health check: compare with previous sync's session ID.
                # During healthy recording the session ID advances ~30-60 times per hour
                # (the pendant creates a new session on every speech pause via VAD).
                # A zero delta over a short interval means the pendant may have stopped recording.
                # Thresholds: Warning after 5 min of no sessions, Unhealthy (alert) after 15 min.
                # Only meaningful for continuous/always-on recording — opt in via .env.
                health_monitoring = os.getenv("PENDANT_HEALTH_MONITORING", "").lower() in ("1", "true", "enabled", "yes")
                session_state_file = output_dir / ".session_state"
                now_ts = time.time()
                if session_state_file.exists():
                    try:
                        parts = session_state_file.read_text().strip().split(",")
                        prev_session = int(parts[0])
                        prev_ts = float(parts[1])
                        elapsed_min = (now_ts - prev_ts) / 60
                        delta = storage_session - prev_session
                        if elapsed_min >= 2 and health_monitoring:
                            rate = delta / (elapsed_min / 60)
                            if delta == 0 and elapsed_min >= 15:
                                print("[!] Pendant Status: Unhealthy - Stop and restart recording using pendant button to reset.", flush=True)
                            elif delta == 0 and elapsed_min >= 5:
                                print("[~] Pendant Status: Warning - No new sessions detected, pendant may not be recording.", flush=True)
                            else:
                                print(f"Pendant Status: Healthy (+{delta} sessions in {elapsed_min:.0f}min, {rate:.0f}/hr)", flush=True)
                    except Exception:
                        pass
                try:
                    session_state_file.write_text(f"{storage_session},{now_ts}")
                except Exception:
                    pass

            # Stop the download before it starts if under 50 pages.
            # Small backlogs are left to accumulate on the pendant to avoid creating
            # many tiny audio files. 50 pages ≈ 70 seconds of audio.
            if total_pages < 50:
                print(f"Found only {total_pages} unread pages. Leaving on pendant to accumulate.", flush=True)
                return 0

            print(f"Pendant reported {total_pages} unread flash pages (approx {format_duration(estimated_seconds)} of audio)", flush=True)
            print("Downloading:", flush=True)

            download_start_time = time.monotonic()

            saved_files, last_processed, actual_downloaded, recalibrated_total = await download_audio(
                protocol=protocol,
                start_page=current_start_page,
                end_page=newest_page,
                output_dir=output_dir,
                logger=logger,
                persist_seconds=args.persist_seconds,
                acknowledge=not args.no_ack,
                batch_number_offset=len(overall_saved_files),
                quiet=args.quiet,
                safe_page_limit=SAFE_PAGE_LIMIT
            )

            overall_saved_files.extend(saved_files)
            elapsed_time = max(time.monotonic() - download_start_time, 0.001)

            # PHANTOM DETECTION:
            # If the recalibrated total was tiny AND we got zero actual pages, the
            # pendant's storage status was a phantom (ghost pages that weren't real).
            # Exit 0 gracefully rather than treating this as a BT error.
            if recalibrated_total < 50 and actual_downloaded == 0:
                print(f"Phantom gap detected. Only {recalibrated_total} actual pages available.", flush=True)
                print(f"Aborting download. Leaving on pendant to accumulate.", flush=True)
                return 0

            # Use actual_downloaded for speed math to prevent inflated ghost speeds.
            pps = actual_downloaded / elapsed_time if elapsed_time > 0 else 0
            print(f"Downloaded {actual_downloaded} of {recalibrated_total} pages - {pps:.2f} p/s - {len(overall_saved_files)} files.", flush=True)

            # Determine exit code based on how far we got through the page range.
            if last_processed is not None and last_processed >= newest_page:
                # EXIT 0: Total Success — pendant fully drained to the snapshot boundary.
                return 0

            # We didn't reach the end — evaluate what we actually got.
            if actual_downloaded > 0:
                # EXIT 3: Partial download — chunk limit or stall. Convert and come back.
                return 3
            else:
                # EXIT 2: Stalled immediately with no data — Bluetooth needs a reset.
                return 2

    except Exception as error:
        error_str = str(error).lower()

        # EXIT 4: Pendant not found — user is out of range or the pendant is off.
        if "not found" in error_str:
            print(f"Pendant not found (out of range or off). Aborting...", flush=True)
            return 4

        # EXIT 2: True Bluetooth / hardware issues — mid-stream drops, encryption errors.
        # Catches bleak errors, disconnects, CoreBluetooth encryption timeouts (CBErrorDomain),
        # and any other "failed to encrypt" / "encrypted ble link" connection failures.
        if "bleak" in error_str or "disconnected" in error_str or "encrypt" in error_str or "cberrordomain" in error_str:
            print(f"[!] Bluetooth hardware error: {error}", flush=True)
            logger.log("ble_hardware_error", error=str(error))
            return 2

        # EXIT 1: Python / logic bugs — unexpected exceptions that indicate a code error.
        print(f"[!] Critical Script Error: {error}", flush=True)
        logger.log("main_loop_error", error=str(error), traceback=traceback.format_exc())
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        log_path = DEFAULT_LOG_FILE.resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.now().strftime('%m-%d-%Y %I:%M:%S %p')
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp_str}] RUN_CRASHED | \n{traceback.format_exc()}\n")
        raise
