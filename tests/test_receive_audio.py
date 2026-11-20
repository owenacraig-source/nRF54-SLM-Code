"""Protocol fault tests and a complete synthetic four-second WAV round trip."""

from dataclasses import replace
import hashlib
from concurrent.futures import ThreadPoolExecutor
import shutil
from types import SimpleNamespace
import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
import wave
import zlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import receive_audio as rx


def metadata(pcm, *, duration_ms=4000, crc=None):
    return rx.INFO_FIELDS.pack(48000, len(pcm) // 2, len(pcm), 2400000,
                              20000, 1, 16, duration_ms, 100,
                              zlib.crc32(pcm) if crc is None else crc, 50, 1)


class ScriptedSerial:
    """A sender that advances only when the correct ACK arrives."""

    def __init__(self, frames, *, duplicate_seqs=(), corrupt_seqs=(), fragment=37):
        self.frames = frames
        self.duplicate_seqs = set(duplicate_seqs)
        self.corrupt_seqs = set(corrupt_seqs)
        self.fragment = fragment
        self.pending = bytearray(b"ignored boot noise\n")
        self.controls = []
        self.current = -1

    @property
    def in_waiting(self):
        return min(len(self.pending), self.fragment)

    def read(self, count):
        count = min(count, self.fragment)
        result = bytes(self.pending[:count])
        del self.pending[:count]
        if not result:
            time.sleep(0.0001)
        return result

    def queue_current(self):
        data = self.frames[self.current].encode()
        if self.frames[self.current].seq in self.corrupt_seqs:
            self.corrupt_seqs.remove(self.frames[self.current].seq)
            bad = bytearray(data)
            bad[-1] ^= 0x80
            self.pending.extend(bad)
        self.pending.extend(data)

    def write(self, data):
        magic, version, kind, size, seq, crc = rx.HEADER.unpack(data)
        assert magic == rx.MAGIC and version == 1 and size == 0
        assert crc == zlib.crc32(data[:12])
        self.controls.append((kind, seq))
        if kind == rx.HELLO and self.current < 0:
            self.current = 0
            self.queue_current()
        elif kind == rx.ACK and self.current >= 0 and seq == self.frames[self.current].seq:
            if seq in self.duplicate_seqs:
                self.duplicate_seqs.remove(seq)
                self.queue_current()  # Simulate a lost ACK and retransmission.
            elif self.current + 1 < len(self.frames):
                self.current += 1
                self.queue_current()
        return len(data)

    def flush(self):
        pass


def recording_frames(pcm, *, info=None, final_crc=None):
    frames = [rx.Frame(rx.INFO, 0, info or metadata(pcm))]
    for offset in range(0, len(pcm), rx.MAX_PAYLOAD):
        frames.append(rx.Frame(rx.DATA, len(frames), pcm[offset:offset + rx.MAX_PAYLOAD]))
    frames.append(rx.Frame(rx.END, len(frames), struct.pack(
        "<I", zlib.crc32(pcm) if final_crc is None else final_crc)))
    return frames


class CaptureTests(unittest.TestCase):
    def receive(self, port, **kwargs):
        return rx.receive_capture(port, start_timeout=0.05, frame_timeout=0.05,
                                  linger=0.002, **kwargs)

    def test_full_four_second_recording_faults_and_bit_exact_wav(self):
        # Contains the packet magic inside the audio, and both signed rails.
        pcm = (b"PCM1" + struct.pack("<hh", -32768, 32767)) * 48000
        self.assertEqual(len(pcm), 384000)
        frames = recording_frames(pcm)
        port = ScriptedSerial(frames, duplicate_seqs=(0, 1, len(frames) - 1),
                              corrupt_seqs=(2,), fragment=13)
        info, received, bad = self.receive(port)
        self.assertEqual(received, pcm)
        self.assertGreaterEqual(bad, 1)
        self.assertEqual(port.controls.count((rx.ACK, 0)), 2)
        self.assertEqual(port.controls.count((rx.ACK, 1)), 2)
        self.assertEqual(port.controls.count((rx.ACK, len(frames) - 1)), 2)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "capture.wav"
            rx.write_wav(output, info, received)
            with wave.open(str(output), "rb") as wav:
                self.assertEqual(wav.getparams()[:4], (1, 2, 48000, 192000))
                self.assertEqual(wav.readframes(192000), pcm)
            self.assertEqual(output.stat().st_size, 384044)

    def test_crc_reference_vector(self):
        self.assertEqual(zlib.crc32(b"123456789"), 0xCBF43926)
        encoded = rx.Frame(rx.HELLO, 0).encode()
        self.assertEqual(encoded.hex(), "50434d3101100000000000005c277ef1")

    def test_inconsistent_metadata_rejected(self):
        pcm = bytes(960)
        info = bytearray(metadata(pcm, duration_ms=10))
        struct.pack_into("<I", info, 0, 46875)
        with self.assertRaisesRegex(rx.CaptureError, "metadata"):
            self.receive(ScriptedSerial(recording_frames(pcm, info=bytes(info))))

    def test_whole_recording_crc_rejected(self):
        pcm = bytes(960)
        port = ScriptedSerial(recording_frames(pcm, info=metadata(pcm, duration_ms=10, crc=1)))
        with self.assertRaisesRegex(rx.CaptureError, "CRC32"):
            self.receive(port)

    def test_end_crc_rejected(self):
        pcm = bytes(960)
        frames = recording_frames(pcm, info=metadata(pcm, duration_ms=10), final_crc=1)
        with self.assertRaisesRegex(rx.CaptureError, "CRC32"):
            self.receive(ScriptedSerial(frames))

    def test_out_of_order_rejected(self):
        pcm = bytes(960)
        frames = recording_frames(pcm, info=metadata(pcm, duration_ms=10))
        frames[1] = replace(frames[1], seq=2)
        with self.assertRaisesRegex(rx.CaptureError, "Out-of-order"):
            self.receive(ScriptedSerial(frames))

    def test_truncated_capture_times_out(self):
        pcm = bytes(960)
        frames = recording_frames(pcm, info=metadata(pcm, duration_ms=10))[:-1]
        with self.assertRaisesRegex(rx.CaptureError, "timed out"):
            self.receive(ScriptedSerial(frames))

    def test_mcu_error_rejected(self):
        frames = [rx.Frame(rx.ERROR, 0, struct.pack("<i", -22))]
        port = ScriptedSerial(frames)
        with self.assertRaisesRegex(rx.CaptureError, "errno -22"):
            self.receive(port)
        self.assertIn((rx.ACK, 0), port.controls)

    def test_corrupt_write_preserves_existing_file(self):
        pcm = bytes(960)
        info = rx.CaptureInfo.parse(metadata(pcm, duration_ms=10))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "capture.wav"
            output.write_bytes(b"existing file")
            with self.assertRaises(rx.CaptureError):
                rx.write_wav(output, info, pcm[:-1])
            self.assertEqual(output.read_bytes(), b"existing file")

    def test_xiao_bridge_dtr_gates_uart_and_is_set_before_open(self):
        pcm = bytes(960)

        class Bridge(ScriptedSerial):
            def __init__(self, **kwargs):
                super().__init__(recording_frames(pcm, info=metadata(pcm, duration_ms=10)))
                self.pending.clear()
                self.dtr = False
                self.rts = False
                self.settings = kwargs
                self.is_open = False

            def open(self):
                self.open_dtr = self.dtr
                self.is_open = True

            def write(self, data):
                # The USB write succeeds even while the SAMD11 UART is closed.
                return super().write(data) if self.dtr else len(data)

        bridge = Bridge()
        with self.assertRaisesRegex(rx.CaptureError, "0 serial bytes"):
            self.receive(bridge)  # Reproduce the original receiver's timeout.
        module = SimpleNamespace(Serial=lambda **kwargs: bridge)
        opened = rx.open_serial_port(module, "/dev/cu.test")
        self.assertIs(opened, bridge)
        self.assertTrue(bridge.open_dtr)
        self.assertFalse(bridge.rts)
        _, received, _ = self.receive(bridge)
        self.assertEqual(received, pcm)

    def test_probe_reports_status_without_consuming_recording(self):
        pcm = bytes(960)
        status = rx.STATUS_FIELDS.pack(4, 0, 400, 16, 0, 0, 0, 0, 0)

        class ProbeSender(ScriptedSerial):
            def write(self, data):
                if rx.HEADER.unpack(data)[2] == rx.PING:
                    self.controls.append((rx.PING, 0))
                    self.pending.extend(rx.Frame(rx.STATUS, 0, status).encode())
                    return len(data)
                return super().write(data)

        port = ProbeSender(recording_frames(pcm, info=metadata(pcm, duration_ms=10)))
        messages = []
        rx.probe_device(port, timeout=0.05, report=messages.append)
        self.assertIn("waiting for laptop", messages[0])
        self.assertIn("saved_blocks=400", messages[0])
        self.assertEqual(port.current, -1)
        self.assertNotIn((rx.HELLO, 0), port.controls)
        _, received, _ = self.receive(port)
        self.assertEqual(received, pcm)

    def test_diagnostic_frame_before_info_is_reported(self):
        pcm = bytes(960)
        port = ScriptedSerial(recording_frames(pcm, info=metadata(pcm, duration_ms=10)))
        status = rx.STATUS_FIELDS.pack(4, 0, 1, 16, 0, 0, 0, 0, 1)
        port.pending.extend(rx.Frame(rx.STATUS, 0, status).encode())
        messages = []
        _, received, _ = self.receive(port, debug=messages.append)
        self.assertEqual(received, pcm)
        self.assertTrue(any("waiting for laptop" in message for message in messages))

    def test_bad_status_size_is_rejected(self):
        with self.assertRaisesRegex(rx.CaptureError, "diagnostic status"):
            rx.describe_status(b"short")

    def test_deleted_names_are_not_reused_and_new_audio_is_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "recordings" / "capture_fixed.wav"
            output.parent.mkdir()
            counter = Path(folder) / "counter.sqlite3"
            original = {output: b"original WAV", output.with_suffix(".json"): b"original JSON",
                        output.with_suffix(".s16le"): b"original PCM"}
            for path, content in original.items():
                path.write_bytes(content)
            # Even an old command with --overwrite must create a fresh identity.
            args = ["receive_audio.py", "--port", "TEST", "--output", str(output), "--raw", "--overwrite"]
            prior_paths = set()
            for number in (1, 2):
                pcm = number.to_bytes(2, "little") * 480
                info = rx.CaptureInfo.parse(metadata(pcm, duration_ms=10))
                with patch.object(sys, "argv", args), \
                     patch.object(rx, "open_serial_port", return_value=MagicMock()), \
                     patch.object(rx, "receive_capture", return_value=(info, pcm, 0)), \
                     patch.object(rx, "COUNTER_FILE", counter), \
                     patch.object(rx.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(rx.main(), 0)
                saved, = output.parent.glob("capture_fixed_*.wav")
                self.assertEqual(saved.name, f"capture_fixed_{number:03d}.wav")
                self.assertNotIn(saved, prior_paths)
                prior_paths.add(saved)
                with wave.open(str(saved), "rb") as wav:
                    self.assertEqual(wav.readframes(480), pcm)
                self.assertEqual(saved.with_suffix(".s16le").read_bytes(), pcm)
                details = json.loads(saved.with_suffix(".json").read_text())
                self.assertEqual(details["sample_count"], 480)
                self.assertEqual(details["pcm_sha256"], hashlib.sha256(pcm).hexdigest())
                for path in (saved, saved.with_suffix(".json"), saved.with_suffix(".s16le")):
                    path.unlink()
            for path, content in original.items():
                self.assertEqual(path.read_bytes(), content)

    def test_unique_names_respect_orphaned_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "capture_fixed.wav"
            counter = Path(folder) / "counter.sqlite3"
            candidate = output.with_name("capture_fixed_001.wav")
            candidate.with_suffix(".json").write_text("existing metadata")
            alternate = output.with_name("capture_fixed_002.wav")
            alternate.with_suffix(".s16le").write_bytes(b"existing raw audio")
            self.assertEqual(rx.available_output_path(output, counter_file=counter),
                             output.with_name("capture_fixed_003.wav"))

    def test_counter_survives_deleting_entire_recordings_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "recordings" / "capture.wav"
            counter = Path(folder) / "counter.sqlite3"
            output.parent.mkdir()
            first = rx.available_output_path(output, counter_file=counter)
            first.write_bytes(b"old audio")
            shutil.rmtree(output.parent)
            self.assertTrue(counter.exists())
            second = rx.available_output_path(output, counter_file=counter)
            self.assertEqual(first.name, "capture_001.wav")
            self.assertEqual(second.name, "capture_002.wav")

    def test_concurrent_receivers_reserve_distinct_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "recordings" / "capture.wav"
            counter = Path(folder) / "counter.sqlite3"
            with ThreadPoolExecutor(max_workers=4) as pool:
                paths = list(pool.map(lambda _: rx.available_output_path(output, counter_file=counter),
                                      range(12)))
            self.assertEqual({path.name for path in paths},
                             {f"capture_{number:03d}.wav" for number in range(1, 13)})

    def test_corrupt_counter_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            counter = Path(folder) / "counter.sqlite3"
            counter.write_bytes(b"damaged counter")
            with self.assertRaisesRegex(rx.CaptureError, "counter was not reset"):
                rx.available_output_path(Path(folder) / "capture.wav", counter_file=counter)
            self.assertEqual(counter.read_bytes(), b"damaged counter")


if __name__ == "__main__":
    unittest.main()

