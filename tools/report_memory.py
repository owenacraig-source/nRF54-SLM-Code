#!/usr/bin/env python3
"""Report actual linked RAM and capture limits using Python's standard library.

Run after pio run: python3 tools/report_memory.py
Or: python3 tools/report_memory.py --build-dir /path/printed/by/platformio
"""

import argparse
import json
from pathlib import Path
import struct


def elf_symbols(path):
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x01\x01":
        raise ValueError("Expected a 32-bit little-endian ARM ELF")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    section_offset, section_size, section_count = header[6], header[11], header[12]
    sections = [struct.unpack_from("<10I", data, section_offset + i * section_size)
                for i in range(section_count)]
    symbols = {}
    for section in sections:
        if section[1] != 2:  # SHT_SYMTAB
            continue
        strings = sections[section[6]]
        names = data[strings[4]:strings[4] + strings[5]]
        for offset in range(section[4], section[4] + section[5], section[9]):
            name, address, size, _, _, index = struct.unpack_from("<IIIBBH", data, offset)
            if name and index:
                end = names.index(0, name)
                symbols[names[name:end].decode()] = (address, size)
    return symbols


def report(build_dir):
    config = {}
    for line in (build_dir / "zephyr" / ".config").read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            config[key] = value
    symbols = elf_symbols(build_dir / "firmware.elf")
    ram_start = int(config["CONFIG_SRAM_BASE_ADDRESS"], 0)
    ram_size = int(config["CONFIG_SRAM_SIZE"]) * 1024
    used = symbols["_image_ram_end"][0] - ram_start
    pcm = symbols["pcm"][1]
    duration = int(config["CONFIG_APP_CAPTURE_DURATION_MS"])
    overhead = used - pcm
    spare = ram_size - used
    guard = 32768
    # Buffer is linear in duration; fixed overhead includes slab and stacks.
    max_ms = (ram_size - overhead - guard) // 960 * 10
    result = {
        "build_dir": str(build_dir),
        "configured_duration_ms": duration,
        "physical_ram_bytes": 524288,
        "application_ram_bytes": ram_size,
        "pcm_buffer_bytes": pcm,
        "dma_slab_bytes": 6 * 960,
        "other_ram_including_dma_and_stacks_bytes": overhead,
        "linked_ram_bytes": used,
        "unallocated_ram_bytes": spare,
        "minimum_unallocated_ram_bytes": guard,
        "estimated_max_duration_ms_with_same_layout_and_guard": max_ms,
    }
    if spare < guard or pcm != duration * 96:
        raise ValueError("Linked image failed RAM or PCM size verification")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path)
    args = parser.parse_args()
    if args.build_dir is None:
        locator = Path(__file__).resolve().parents[1] / ".pio" / "audio-build-dir.txt"
        args.build_dir = Path(locator.read_text().strip())
    print(json.dumps(report(args.build_dir), indent=2))


if __name__ == "__main__":
    main()
