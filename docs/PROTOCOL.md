# Serial protocol version 1

UART20 through the XIAO SAMD11 bridge: 115200 baud, 8N1, no RTS/CTS. All
multi-byte integers are little endian. No console text shares this UART.
Host DTR must be asserted to enable the bridge's UART.

Every frame has this 16-byte header followed by up to 1024 payload bytes:

| Offset | Width | Meaning |
| --- | --- | --- |
| 0 | 4 bytes | ASCII `PCM1` |
| 4 | uint8 | Version, 1 |
| 5 | uint8 | Type |
| 6 | uint16 | Payload byte count |
| 8 | uint32 | Sequence number |
| 12 | uint32 | CRC32 of header bytes 0–11 followed by payload |

CRC32 is IEEE reflected CRC32, as returned by `zlib.crc32()` and Zephyr's
`crc32_ieee()` / `crc32_ieee_update()`. The stored CRC field is excluded. A
known zero-payload HELLO frame is `50434d3101100000000000005c277ef1` in hex.

| Type | Direction | Payload and sequence |
| --- | --- | --- |
| 16 HELLO | Laptop → MCU | Empty, sequence 0 |
| 17 ACK | Laptop → MCU | Empty, sequence being acknowledged |
| 18 PING | Laptop → MCU | Empty, sequence 0; diagnostic query, no transfer |
| 1 INFO | MCU → laptop | 48-byte metadata, sequence 0 |
| 2 DATA | MCU → laptop | PCM bytes, sequences 1, 2, … |
| 3 END | MCU → laptop | uint32 whole-PCM CRC32, next sequence |
| 4 ERROR | MCU → laptop | int32 negative Zephyr errno, sequence 0 |
| 5 STATUS | MCU → laptop | 36-byte diagnostic snapshot, sequence 0; no ACK |

INFO consists of 12 32-bit fields, in this order: sample rate, sample count,
PCM byte count, PDM clock, **signed** gain in millidecibels, channel count,
bits per sample, saved duration in milliseconds, discarded warmup milliseconds,
whole-PCM CRC32, decimation ratio, flags. Flag bit 0 means the optional PDM HPF
is disabled. The remaining fields are unsigned. Version 1 has no other flags.

Diagnostic firmware answers PING while awaiting HELLO and sends one STATUS
before INFO or ERROR. STATUS has nine 32-bit fields: stage, **signed** error,
saved block count, UART RX bytes, dropped RX bytes, UART error mask, packet
retry count, transmitted frame count, accepted HELLO count. All but error are
unsigned. See `include/diagnostics.h` for stages. Snapshots may span adjacent
ISR events. PING does not consume a recording or start another capture.

The laptop repeats HELLO once per second until INFO or ERROR arrives. The MCU
performs capture before consuming HELLO; HELLO never starts another capture.
The MCU sends one frame at a time and waits for its ACK. It retries after two
seconds, for at most ten transmissions of that frame. All but the last DATA
payload are 1024 bytes. Duplicate INFO/DATA/END frames are acknowledged without
appending samples again. The laptop validates the complete sample count and
whole-PCM CRC before saving a file, and lingers 2.5 seconds to acknowledge a
retried END. An abandoned transfer requires a reset to start again.

CRC32 detects accidental transport corruption; it is not authentication. PCM
is never sent during capture. The buffer remains intact until reset, but the
firmware implements only one transfer session per application start.
