#!/usr/bin/env python3

"""
Limitless Pendant LED Brightness Control  (set_brightness.py)
-------------------------------------------------------------
Sets the LED brightness on a Limitless Pendant over Bluetooth Low Energy.

The brightness command uses the same custom protobuf-like encoding as all other
pendant commands (see download.py for detailed protocol notes). It sends a write
to the TX characteristic with outer field 26, which the pendant's firmware maps
to its LED dimming control.

BRIGHTNESS RANGE:
    0   — off (or minimum glow, pendant-dependent)
    50  — half brightness
    100 — full brightness
    Values outside 0–100 are clamped with a printed warning.

NOTE — BRIGHTNESS CANNOT BE READ BACK:
    The Limitless firmware does not expose a readable GATT characteristic for
    the current brightness level. The official desktop app (LimitlessDeviceConnection.swift)
    tracks brightness locally in memory after each write. There is no way to query
    the current brightness from the device without having set it first in this session.

USAGE:
    python set_brightness.py 75
    python set_brightness.py --address AA:BB:CC:DD:EE:FF 50
    python set_brightness.py --name "Pendant" 0

.ENV AUTO-CONNECT:
    If PENDANT_MAC_ADDRESS is set in the project-root .env file (written by
    pendant_sync.py), the address is used automatically — skipping BLE
    discovery entirely and connecting in seconds instead of ~8 seconds.
    Passing --address on the CLI always takes precedence over .env.

EXIT CODES:
    0 — Success
    1 — Error (bad args, BLE failure, pendant not found)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ==========================================
# DEPENDENCY GUARD (bleak BLE library)
# ==========================================
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
# All UUIDs are identical to those used in download.py.

BLUETOOTH_BASE_UUID = "-0000-1000-8000-00805f9b34fb"

# Standard GATT Battery Service — reports battery percentage (0–100).
BATTERY_LEVEL_CHARACTERISTIC_UUID = "00002a19" + BLUETOOTH_BASE_UUID

# Limitless Pendant Custom Service.
# TX (write with response): where we SEND commands TO the pendant.
# RX (notify):              where we RECEIVE data FROM the pendant.
LIMITLESS_SERVICE_UUID  = "632de001-604c-446b-a80f-7963e950f3fb"
LIMITLESS_TX_CHAR_UUID  = "632de002-604c-446b-a80f-7963e950f3fb"
LIMITLESS_RX_CHAR_UUID  = "632de003-604c-446b-a80f-7963e950f3fb"


# ==========================================
# BLE DEVICE DISCOVERY HELPERS
# ==========================================

@dataclass
class DeviceCandidate:
    device: Any
    advertisement: Any
    name: str
    services: set[str]
    score: int


def _normalize_uuid(uuid: str) -> str:
    lowered = str(uuid).strip().lower()
    if len(lowered) == 4:
        return f"0000{lowered}{BLUETOOTH_BASE_UUID}"
    if len(lowered) == 8:
        return f"{lowered}{BLUETOOTH_BASE_UUID}"
    return lowered


def _get_device_name(device: Any, advertisement: Any) -> str:
    local_name = getattr(advertisement, "local_name", None)
    device_name = getattr(device, "name", None)
    return local_name or device_name or "Unknown"


def _get_advertised_services(advertisement: Any) -> set[str]:
    service_uuids = getattr(advertisement, "service_uuids", None) or []
    return {_normalize_uuid(uuid) for uuid in service_uuids}


def _score_candidate(name: str, services: set[str]) -> int:
    """
    Scores a BLE device as a Limitless Pendant candidate (same logic as download.py):
        100 — Advertises the Limitless custom service UUID
         80 — Device name contains "limitless"
         60 — Device name contains "pendant"
          0 — No match
    """
    score = 0
    lowered = name.lower()
    if LIMITLESS_SERVICE_UUID in services:
        score += 100
    if "limitless" in lowered:
        score += 80
    if "pendant" in lowered:
        score += 60
    return score


async def _discover_candidates(
    scan_timeout: float,
    filter_address: str | None,
    filter_name: str | None,
) -> list[DeviceCandidate]:
    if BleakScanner is None:
        raise RuntimeError(
            "Missing dependency: install BLE support with "
            "`python3 -m pip install bleak`."
        )
    try:
        discoveries = await BleakScanner.discover(timeout=scan_timeout, return_adv=True)
    except BleakBluetoothNotAvailableError as error:
        raise RuntimeError("Bluetooth is unavailable.") from error

    req_address = filter_address.lower() if filter_address else None
    req_name    = filter_name.lower()    if filter_name    else None
    candidates: list[DeviceCandidate] = []

    for device, advertisement in discoveries.values():
        device_address = str(getattr(device, "address", "")).lower()
        name     = _get_device_name(device, advertisement)
        services = _get_advertised_services(advertisement)

        if req_address and device_address != req_address:
            continue
        if req_name and req_name not in name.lower():
            continue

        score = _score_candidate(name, services)
        # Force inclusion when explicitly requested by address even if unrecognised.
        if req_address and device_address == req_address and score <= 0:
            score = 1

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


# ==========================================
# LIMITLESS PROTOCOL (brightness subset)
# ==========================================

class BrightnessProtocol:
    """
    Minimal implementation of the Limitless pendant BLE protocol — just enough
    to initialize the connection and send the LED brightness command.

    Encoding mirrors download.py's LimitlessProtocol exactly. The full protocol
    notes live in download.py; only the brightness-relevant subset is reproduced here.

    BRIGHTNESS COMMAND ENCODING (translated from Swift encodeSetLedBrightness):
        inner_msg = encode_int32_field(1, brightness)       # field 1: the brightness value (0–100)
        command   = encode_message(26, inner_msg)           # outer field 26 = LED brightness command
                  + encode_request_data()                   # field 30 transaction envelope
        payload   = encode_ble_wrapper(command)             # standard outer BLE wrapper
    """

    def __init__(self, client: BleakClientType) -> None:
        self.client       = client
        self.message_index = 0
        self.request_id    = 0

    # ------------------------------------------------------------------
    # Protobuf-like encoding helpers (identical to download.py versions)
    # ------------------------------------------------------------------

    def _encode_varint(self, value: int) -> list[int]:
        """Encodes an integer as a Protobuf varint (little-endian, 7 bits per byte)."""
        result: list[int] = []
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return result or [0]

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
        Wraps a command in the outer BLE message envelope (same for every command):
            Field 1: message_index (auto-incremented write sequence counter)
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
        Encodes a request envelope (field 30) with a monotonically increasing
        request_id. Appended to every command as a transaction ID.
        """
        self.request_id += 1
        message: list[int] = []
        message.extend(self._encode_int64_field(1, self.request_id))
        message.extend(self._encode_field(2, 0, [0x00]))
        return self._encode_message(30, message)

    # ------------------------------------------------------------------
    # Command encoders
    # ------------------------------------------------------------------

    def _encode_set_current_time(self, timestamp_ms: int) -> bytes:
        """Encodes the 'set current time' command (outer field 6)."""
        time_message = self._encode_int64_field(1, timestamp_ms)
        command = [*self._encode_message(6, time_message), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    def _encode_enable_data_stream(self, enable: bool = True) -> bytes:
        """
        Encodes the 'enable data stream' command (outer field 8).
        Required during initialization to activate the pendant's data output;
        without it the pendant ignores subsequent commands.
        """
        message: list[int] = []
        message.extend(self._encode_field(1, 0, [0x00]))
        message.extend(self._encode_field(2, 0, [0x01 if enable else 0x00]))
        command = [*self._encode_message(8, message), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    def _encode_set_led_brightness(self, brightness: int) -> bytes:
        """
        Encodes the 'set LED brightness' command (outer field 26).

        Translated directly from Swift encodeSetLedBrightness():
            var msg = [UInt8]()
            msg.append(contentsOf: encodeField(1, 0, encodeVarint(max(0, min(100, brightness)))))
            let cmd = encodeMessage(26, msg) + encodeRequestData()
            return encodeBleWrapper(cmd)

        Field 1 inside the message holds the brightness integer (0–100) as a varint.
        """
        brightness = max(0, min(100, brightness))
        inner_msg = self._encode_int32_field(1, brightness)
        command = [*self._encode_message(26, inner_msg), *self._encode_request_data()]
        return self._encode_ble_wrapper(command)

    # ------------------------------------------------------------------
    # BLE operations
    # ------------------------------------------------------------------

    async def _write(self, payload: bytes) -> None:
        """Sends a command to the pendant via the TX characteristic (with GATT ack)."""
        await self.client.write_gatt_char(LIMITLESS_TX_CHAR_UUID, payload, response=True)

    def _noop_notification_handler(self, _sender: Any, _data: bytearray) -> None:
        """
        No-op RX notification handler.
        We must subscribe to RX notifications as part of the pendant's expected
        initialization sequence, but for brightness-only operations we don't need
        to act on any incoming data.
        """

    async def initialize(self) -> None:
        """
        Subscribes to RX notifications and runs the standard init sequence:
            1. Subscribe to RX (required — pendant expects this before commands)
            2. Set current time   (field 6)
            3. Enable data stream (field 8)

        This mirrors the Swift app's initialization order exactly.
        """
        await self.client.start_notify(LIMITLESS_RX_CHAR_UUID, self._noop_notification_handler)
        await asyncio.sleep(1)
        await self._write(self._encode_set_current_time(int(time.time() * 1000)))
        await asyncio.sleep(1)
        await self._write(self._encode_enable_data_stream())
        await asyncio.sleep(1)

    async def stop(self) -> None:
        """Unsubscribes from RX notifications. Called on clean disconnect."""
        try:
            await self.client.stop_notify(LIMITLESS_RX_CHAR_UUID)
        except Exception:
            pass

    async def get_battery_level(self) -> int | None:
        """
        Reads the pendant's battery percentage from the standard GATT Battery Service.
        Returns None if the characteristic is unavailable.
        """
        try:
            data = await self.client.read_gatt_char(BATTERY_LEVEL_CHARACTERISTIC_UUID)
            if data and len(data) > 0:
                return int(data[0])
        except Exception:
            pass
        return None

    async def set_brightness(self, brightness: int) -> None:
        """Sends the LED brightness command to the pendant."""
        await self._write(self._encode_set_led_brightness(brightness))


# ==========================================
# .ENV ADDRESS LOOKUP
# ==========================================

def _env_pendant_address() -> str | None:
    """
    Returns PENDANT_MAC_ADDRESS from the project-root .env file, or None.

    The .env file lives one level above the scripts/ directory — the same
    location pendant_sync.py uses. We try python-dotenv first (already a
    project dependency); if it's not importable we fall back to a simple
    line-by-line parse that handles KEY=VALUE and KEY="VALUE" formats.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return None

    # Preferred: use python-dotenv (already required by pendant_sync.py).
    try:
        dotenv_module = importlib.import_module("dotenv")
        load_dotenv = getattr(dotenv_module, "load_dotenv")
        load_dotenv(dotenv_path=env_path, override=False)
        return os.getenv("PENDANT_MAC_ADDRESS") or None
    except ModuleNotFoundError:
        pass

    # Fallback: parse KEY=VALUE lines manually.
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == "PENDANT_MAC_ADDRESS":
                return val.strip().strip('"').strip("'") or None
    except Exception:
        pass

    return None


# ==========================================
# CLI ARGUMENT PARSING
# ==========================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set the LED brightness on a Limitless Pendant over BLE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python set_brightness.py 75\n"
            "  python set_brightness.py --address AA:BB:CC:DD:EE:FF 50\n"
            "  python set_brightness.py --name Pendant 0\n"
            "\n"
            "Brightness 0 = off (or minimum glow), 100 = full brightness.\n"
            "Values outside 0–100 are clamped automatically."
        ),
    )
    parser.add_argument(
        "brightness",
        type=int,
        help="LED brightness level (0–100).",
    )
    parser.add_argument(
        "--address",
        help="Connect to a specific BLE address or macOS device identifier.",
    )
    parser.add_argument(
        "--name",
        help="Only consider devices whose advertised name contains this string.",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=8.0,
        help="Seconds to scan for the pendant before giving up. Default: 8.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for the BLE connection. Default: 15.",
    )
    parser.add_argument(
        "--no-pair",
        action="store_true",
        help="Do not ask BLE to pair/bond when connecting.",
    )
    return parser.parse_args(argv)


# ==========================================
# MAIN
# ==========================================

async def main() -> int:
    args = parse_args()

    # Clamp and warn if out of range.
    raw_brightness = args.brightness
    brightness = max(0, min(100, raw_brightness))
    if brightness != raw_brightness:
        print(
            f"[!] Brightness {raw_brightness} is out of range — clamped to {brightness}.",
            file=sys.stderr,
        )

    if BleakClient is None or BleakScanner is None:
        print(
            "[!] bleak is not installed. Run: python3 -m pip install bleak",
            file=sys.stderr,
        )
        return 1

    # --- 1. RESOLVE ADDRESS (env or scan) ---
    # Priority: --address CLI flag > PENDANT_MAC_ADDRESS in .env > BLE scan.
    connect_target: Any  # str address OR DeviceCandidate
    display_name: str

    if args.address:
        # Explicit CLI address — connect directly, no scan needed.
        connect_target = args.address
        display_name = args.address
        print(f"Using address from --address: {args.address}")
    else:
        env_address = _env_pendant_address()
        if env_address:
            # Known address from .env — connect directly, skip discovery scan.
            connect_target = env_address
            display_name = env_address
            print(f"Using address from .env: {env_address}")
        else:
            # No address known — fall back to BLE discovery scan.
            print(f"Scanning for Limitless Pendant ({args.scan_timeout:.0f}s)…")
            try:
                candidates = await _discover_candidates(
                    scan_timeout=args.scan_timeout,
                    filter_address=None,
                    filter_name=args.name,
                )
            except RuntimeError as error:
                print(f"[!] Scan failed: {error}", file=sys.stderr)
                return 1

            if not candidates:
                print(
                    "[!] No Limitless Pendant found. Make sure it is nearby and powered on.",
                    file=sys.stderr,
                )
                return 1

            best = candidates[0]
            connect_target = best.device
            display_name = f"{best.name}  [{best.device.address}]"
            print(f"Found: {display_name}  (score {best.score})")

    # --- 2. CONNECT ---
    pair = not args.no_pair
    print(f"Connecting (timeout {args.connect_timeout:.0f}s)…")
    try:
        async with BleakClient(
            connect_target,
            timeout=args.connect_timeout,
            pair=pair,
        ) as client:
            protocol = BrightnessProtocol(client)

            # --- 3. INITIALIZE ---
            print("Initializing pendant…")
            await protocol.initialize()

            # --- 4. BATTERY ---
            battery = await protocol.get_battery_level()
            if battery is not None:
                print(f"Battery: {battery}%")
            else:
                print("Battery: unavailable")

            # --- 5. SET BRIGHTNESS ---
            print(f"Setting LED brightness to {brightness}…")
            await protocol.set_brightness(brightness)

            # Give the pendant a moment to process before we drop the connection.
            await asyncio.sleep(0.5)

            # --- 6. CLEAN UP ---
            await protocol.stop()

    except Exception as error:
        print(f"[!] BLE error: {error}", file=sys.stderr)
        return 1

    print(f"Done. LED brightness set to {brightness}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
