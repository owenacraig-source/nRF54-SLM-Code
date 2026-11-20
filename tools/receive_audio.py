#!/usr/bin/env python3
"""Receive one checked PCM block from the XIAO and save a mono 48 kHz WAV.

Install: python3 -m pip install -r tools/requirements.txt
List ports: python3 tools/receive_audio.py --list-ports
Receive: python3 tools/receive_audio.py --port /dev/cu.usbmodem... --output capture.wav
Windows: python tools/receive_audio.py --port COM5 --output capture.wav
"""

from __future__ import annotations

import argparse
from array import array
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import uuid
from typing import Callable
import wave
import zlib


MAGIC = b"PCM1"
VERSION = 1
INFO, DATA, END, ERROR, STATUS, HELLO, ACK, PING = 1, 2, 3, 4, 5, 16, 17, 18
HEADER = struct.Struct("<4sBBHII")
INFO_FIELDS = struct.Struct("<IIIIiIIIIIII")
MAX_PAYLOAD = 1024
MAX_PCM_BYTES = 1_048_576
COUNTER_FILE = Path(__file__).resolve().parents[1] / ".capture-counter.sqlite3"
STATUS_FIELDS = struct.Struct("<IiIIIIIII")
STAGES = {1: "boot", 2: "microphone warmup", 3: "recording", 4: "waiting for laptop",
          5: "transferring", 6: "complete", 10: "UART initialization failed",
          11: "microphone configuration failed", 12: "microphone read failed",
          13: "microphone stop failed", 14: "transfer failed", 15: "CPU fatal error"}


class CaptureError(Exception):
    """An invalid, incomplete, or failed capture; no WAV should be published."""


@dataclass(frozen=True)
class Frame:
    kind: int
    seq: int
    payload: bytes = b""

    def encode(self) -> bytes:
        if len(self.payload) > MAX_PAYLOAD:
            raise ValueError("Frame payload exceeds 1024 bytes")
        prefix = struct.pack("<4sBBHI", MAGIC, VERSION, self.kind, len(self.payload), self.seq)
        crc = zlib.crc32(self.payload, zlib.crc32(prefix))
        return prefix + struct.pack("<I", crc) + self.payload


class FrameReader:
    def __init__(self, port):
        self.port = port
        self.buffer = bytearray()
        self.bad_frames = 0
        self.bytes_received = 0

    def read(self, timeout: float) -> Frame | None:
        deadline = time.monotonic() + timeout
        while True:
            while len(self.buffer) >= 4:
                start = self.buffer.find(MAGIC)
                if start < 0:
                    del self.buffer[:-3]
                    break
                if start:
                    del self.buffer[:start]
                if len(self.buffer) < HEADER.size:
                    break
                _, version, kind, size, seq, crc = HEADER.unpack_from(self.buffer)
                if version != VERSION or size > MAX_PAYLOAD:
                    del self.buffer[0]
                    self.bad_frames += 1
                    continue
                total = HEADER.size + size
                if len(self.buffer) < total:
                    break
                payload = bytes(self.buffer[HEADER.size:total])
                actual_crc = zlib.crc32(payload, zlib.crc32(self.buffer[:12]))
                if actual_crc != crc:
                    # Keep potential following frames, even if a bad length ate
                    # their bytes while collecting this candidate.
                    del self.buffer[0]
                    self.bad_frames += 1
                    continue
                del self.buffer[:total]
                return Frame(kind, seq, payload)
            if time.monotonic() >= deadline:
                return None
            waiting = self.port.in_waiting
            data = self.port.read(min(waiting or 1, 2048))
            self.bytes_received += len(data)
            self.buffer.extend(data)


def open_serial_port(serial_module, name: str, baud: int = 115200):
    """DTR MUST be asserted: the XIAO SAMD11 closes its UART when DTR is low."""
    port = serial_module.Serial(port=None, baudrate=baud, timeout=0.05,
                                write_timeout=1, rtscts=False, dsrdtr=False,
                                xonxoff=False)
    port.dtr = True
    port.rts = False
    if os.name == "posix":
        port.exclusive = True
    port.port = name
    port.open()
    return port


def describe_status(payload: bytes) -> str:
    if len(payload) != STATUS_FIELDS.size:
        raise CaptureError("Invalid diagnostic status length")
    stage, error, blocks, received, dropped, errors, retries, sent, hellos = STATUS_FIELDS.unpack(payload)
    return (f"MCU: {STAGES.get(stage, f'unknown stage {stage}')} (stage={stage}, error={error}), "
            f"saved_blocks={blocks}, uart_rx_bytes={received}, rx_dropped={dropped}, "
            f"uart_error_mask=0x{errors:x}, retries={retries}, "
            f"tx_frames={sent}, HELLOs={hellos}")


def probe_device(port, *, timeout: float = 8, report=print) -> None:
    """Request diagnostics without starting a transfer or taking another capture."""
    reader = FrameReader(port)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        send_control(port, PING)
        frame = reader.read(min(1, max(0, deadline - time.monotonic())))
        if frame is not None and frame.kind == STATUS:
            report(describe_status(frame.payload))
            return
    raise CaptureError(f"No diagnostic response ({reader.bytes_received} bytes received, "
                       f"{reader.bad_frames} rejected frames). --probe requires updated firmware.")


@dataclass(frozen=True)
class CaptureInfo:
    sample_rate_hz: int
    sample_count: int
    pcm_bytes: int
    pdm_clock_hz: int
    gain_millidb: int
    channels: int
    bits_per_sample: int
    duration_ms: int
    discarded_warmup_ms: int
    pcm_crc32: int
    decimation_ratio: int
    flags: int

    @classmethod
    def parse(cls, payload: bytes) -> CaptureInfo:
        if len(payload) != INFO_FIELDS.size:
            raise CaptureError("Invalid capture metadata length")
        info = cls(*INFO_FIELDS.unpack(payload))
        if (info.sample_rate_hz != 48_000 or info.channels != 1 or
                info.bits_per_sample != 16 or info.pdm_clock_hz != 2_400_000 or
                info.decimation_ratio != 50 or info.flags != 1 or
                not -20_000 <= info.gain_millidb <= 20_000 or
                not 0 < info.pcm_bytes <= MAX_PCM_BYTES or
                info.pcm_bytes != info.sample_count * 2 or
                info.sample_count * 1000 != info.sample_rate_hz * info.duration_ms):
            raise CaptureError("Unsupported or inconsistent capture metadata")
        return info


def send_control(port, kind: int, seq: int = 0) -> None:
    data = Frame(kind, seq).encode()
    if port.write(data) != len(data):
        raise CaptureError("Serial control write was incomplete")
    port.flush()


def receive_capture(port, *, start_timeout: float = 60, frame_timeout: float = 25,
                    linger: float = 2.5,
                    progress: Callable[[int, int], None] | None = None,
                    debug: Callable[[str], None] | None = None
                    ) -> tuple[CaptureInfo, bytes, int]:
    reader = FrameReader(port)
    deadline = time.monotonic() + start_timeout
    next_hello = 0.0
    while True:
        now = time.monotonic()
        if now >= deadline:
            raise CaptureError(f"No capture response ({reader.bytes_received} serial bytes received, "
                               f"{reader.bad_frames} rejected frames). "
                               "DTR must be ON for the XIAO CMSIS-DAP bridge. "
                               "Check the LED state; press RESET once if a prior transfer ended.")
        if now >= next_hello:
            send_control(port, HELLO)
            if debug:
                debug(f"HELLO sent; received={reader.bytes_received} bytes, rejected={reader.bad_frames}")
            next_hello = now + 1.0
        frame = reader.read(min(deadline, next_hello) - time.monotonic())
        if frame is None:
            continue
        if frame.kind == STATUS:
            if debug:
                debug(describe_status(frame.payload))
            continue
        if frame.kind == ERROR and frame.seq == 0 and len(frame.payload) == 4:
            send_control(port, ACK, 0)
            code, = struct.unpack("<i", frame.payload)
            raise CaptureError(f"MCU capture failed (Zephyr errno {code}); no partial audio saved")
        if frame.kind == INFO and frame.seq == 0:
            info = CaptureInfo.parse(frame.payload)
            if debug:
                debug(f"Capture INFO: {info.sample_count} samples at {info.sample_rate_hz} Hz")
            send_control(port, ACK, 0)
            break

    pcm = bytearray()
    expected_seq = 1
    deadline = time.monotonic() + frame_timeout
    previous_data: Frame | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureError("Transfer timed out; press RESET and run the receiver again")
        frame = reader.read(remaining)
        if frame is None:
            continue
        if frame.kind == INFO and frame.seq == 0:
            if frame.payload != INFO_FIELDS.pack(*asdict(info).values()):
                raise CaptureError("Capture metadata changed during transfer (device reset?)")
            send_control(port, ACK, 0)  # The previous ACK may have been lost.
            deadline = time.monotonic() + frame_timeout
            continue
        if previous_data is not None and frame == previous_data:
            send_control(port, ACK, frame.seq)  # Duplicate: never append twice.
            deadline = time.monotonic() + frame_timeout
            continue
        if frame.seq != expected_seq:
            raise CaptureError(f"Out-of-order frame {frame.seq}; expected {expected_seq}")
        if frame.kind == DATA:
            expected_size = min(MAX_PAYLOAD, info.pcm_bytes - len(pcm))
            if expected_size <= 0 or len(frame.payload) != expected_size:
                raise CaptureError("PCM packet has an unexpected length")
            pcm.extend(frame.payload)
            send_control(port, ACK, frame.seq)
            previous_data = frame
            expected_seq += 1
            deadline = time.monotonic() + frame_timeout
            if progress:
                progress(len(pcm), info.pcm_bytes)
        elif frame.kind == END:
            if len(frame.payload) != 4 or len(pcm) != info.pcm_bytes:
                raise CaptureError("End marker arrived before a complete recording")
            end_crc, = struct.unpack("<I", frame.payload)
            if zlib.crc32(pcm) != info.pcm_crc32 or end_crc != info.pcm_crc32:
                raise CaptureError("Whole-recording CRC32 failed")
            send_control(port, ACK, frame.seq)
            # Remain available for a retransmitted END if its ACK was lost.
            end_deadline = time.monotonic() + linger
            while time.monotonic() < end_deadline:
                duplicate = reader.read(end_deadline - time.monotonic())
                if duplicate == frame:
                    send_control(port, ACK, frame.seq)
            return info, bytes(pcm), reader.bad_frames
        else:
            raise CaptureError(f"Unexpected packet type {frame.kind}")


def available_output_path(requested: Path, *, counter_file: Path | None = None) -> Path:
    """Reserve a number durably; deletion of recordings never lowers it.

    SQLite's write transaction serializes separate receiver processes. Commit
    before saving audio so even an interrupted save cannot recycle its number.
    The state lives in the project root, outside the recordings directory.
    """
    state = COUNTER_FILE if counter_file is None else counter_file
    state.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(state, timeout=10)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS counter "
                       "(id INTEGER PRIMARY KEY CHECK(id=1), "
                       "next_number INTEGER NOT NULL CHECK(next_number>0))")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next_number FROM counter WHERE id=1").fetchone()
            number = row[0] if row else 1
            while True:
                candidate = requested.with_name(f"{requested.stem}_{number:03d}{requested.suffix}")
                if not any(candidate.with_suffix(suffix).exists()
                           for suffix in (requested.suffix, ".json", ".s16le")):
                    break
                number += 1
            db.execute("INSERT OR REPLACE INTO counter (id, next_number) VALUES (1, ?)",
                       (number + 1,))
        return candidate
    except sqlite3.Error as error:
        raise CaptureError(f"Cannot reserve recording number in {state}: {error}. "
                           "The counter was not reset; keep this state file.") from error


def write_wav(path: Path, info: CaptureInfo, pcm: bytes) -> None:
    """Write and atomically publish only a fully validated recording."""
    if len(pcm) != info.pcm_bytes or zlib.crc32(pcm) != info.pcm_crc32:
        raise CaptureError("Refusing to write an incomplete or corrupt PCM block")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".audio-", suffix=".wav", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            with wave.open(output, "wb") as wav:
                wav.setnchannels(info.channels)
                wav.setsampwidth(info.bits_per_sample // 8)
                wav.setframerate(info.sample_rate_hz)
                wav.writeframes(pcm)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def signal_stats(pcm: bytes) -> dict:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return {
        "peak_integer": peak,
        "rms_dbfs": 20 * math.log10(rms / 32768) if rms else None,
        "mean_integer": sum(samples) / len(samples),
        "samples_at_rails": sum(value in (-32768, 32767) for value in samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--port", help="/dev/cu.usbmodem... (macOS), COM5 (Windows), /dev/ttyACM0 (Linux)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--output", type=Path, default=Path("recordings/capture.wav"),
                        help="WAV basename; a persistent counter appends _001, _002, etc.")
    parser.add_argument("--raw", action="store_true", help="Also save headerless signed 16-bit little-endian PCM")
    parser.add_argument("--overwrite", action="store_true",
                        help="Deprecated compatibility option; recordings always use unique filenames")
    parser.add_argument("--start-timeout", type=float, default=60)
    parser.add_argument("--debug", action="store_true", help="Show handshake and MCU diagnostic status")
    parser.add_argument("--probe", action="store_true", help="Query updated MCU firmware without consuming the recording")
    args = parser.parse_args()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        parser.error("Install pyserial: python3 -m pip install -r tools/requirements.txt")
    if args.list_ports:
        for port in list_ports.comports():
            print(f"{port.device}: {port.description}")
        return 0
    if not args.port:
        parser.error("--port is required unless --list-ports is used")
    if args.start_timeout <= 0:
        parser.error("--start-timeout must be positive")
    if args.output.suffix.lower() != ".wav":
        parser.error("--output must have a .wav extension")
    last_percent = -1

    def show_progress(done: int, total: int) -> None:
        nonlocal last_percent
        percent = done * 100 // total
        if percent // 10 != last_percent // 10:
            print(f"Receiving: {percent}%", flush=True)
        last_percent = percent

    try:
        with open_serial_port(serial, args.port, args.baud) as port:
            print(f"Opened {args.port}: {args.baud} baud, DTR=ON (USB bridge enabled).", flush=True)
            # Let the USB control request reopen/reinitialize the SAMD11 UART.
            time.sleep(0.1)
            if args.probe:
                probe_device(port, timeout=args.start_timeout)
                return 0
            print("Waiting for the retained capture. Press RESET for a new capture.", flush=True)
            info, pcm, bad_frames = receive_capture(port, start_timeout=args.start_timeout,
                                                    progress=show_progress,
                                                    debug=(lambda message: print(message, flush=True)) if args.debug else None)
        stats = signal_stats(pcm)
        # Reserve the next number after reception. The durable counter prevents
        # deleted names being recycled, including with old --overwrite commands.
        received_at = datetime.now().astimezone()
        recording_id = uuid.uuid4().hex
        audio_sha256 = hashlib.sha256(pcm).hexdigest()
        args.output = available_output_path(args.output)
        if args.overwrite:
            print("--overwrite is ignored: each recording now uses a unique filename.")
        write_wav(args.output, info, pcm)
        args.output.with_suffix(".json").write_text(
            json.dumps({**asdict(info), "rejected_frames": bad_frames, **stats,
                        "received_at": received_at.isoformat(), "recording_id": recording_id,
                        "pcm_sha256": audio_sha256}, indent=2) + "\n",
            encoding="utf-8")
        if args.raw:
            args.output.with_suffix(".s16le").write_bytes(pcm)
        print(f"Saved {args.output}: {info.sample_count} samples, "
              f"{info.duration_ms / 1000:.3f} s, {info.sample_rate_hz} Hz, mono s16.")
        if args.debug:
            print(f"PCM SHA-256: {audio_sha256}")
        if stats["samples_at_rails"]:
            print(f"{stats['samples_at_rails']} samples reached a PCM rail; consider reducing CONFIG_APP_GAIN_DB.")
        if stats["peak_integer"] == 0:
            print("All samples are zero: check microphone power, CLOCK, DATA and SELECT wiring.")
        return 0
    except (CaptureError, serial.SerialException, OSError) as error:
        print(f"Capture failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

