# Transfer timeout and diagnostics

The original Python receiver forced `port.dtr = False`. In the XIAO SAMD11
bridge's [CDC control handler](https://github.com/baorepo/free-dap/blob/master/platform/samd11/main.c#L224-L237),
DTR (`USB_CDC_CTRL_SIGNAL_DTE_PRESENT`) enables the UART; clearing it calls
`uart_close()`. A USB port can enumerate and accept host writes while the UART
behind it is closed. This explains the timeout and agrees with the reported
four-second green LED / PDM clock followed by silence.

The corrected receiver asserts DTR before opening, waits 100 ms for bridge
initialization, and holds DTR on. Automatic hardware/software flow control stays
disabled. The board's actual bridge firmware version has not been read back;
the complete live transfer still needs confirmation on that board.

## Try the receiver fix first, without reflashing

Close other serial monitors. From the project folder:

```sh
.venv/bin/python tools/receive_audio.py --port /dev/cu.usbmodem55AEFA263 --output recordings/capture_fixed.wav --debug
```

Startup should print `DTR=ON`. This works with the original audio firmware,
which retains the capture until HELLO arrives. If a transfer already ran or
exhausted its retries, press RESET once for a new capture. Existing output names
get a persistent sequence suffix: `capture_fixed_001.wav`, `_002.wav`, etc.
The counter is stored in `.capture-counter.sqlite3` in the project root, outside
the recordings folder. Keep this file so deleting recordings never resets it.
`--overwrite` is now ignored for compatibility with old commands. Open the exact
filename printed by `Saved`; JSON metadata and `--debug` include the received
PCM's SHA-256 so actual byte changes can be distinguished from playback caching.
`--debug` reports HELLO attempts, received byte
counts, rejected frames and capture metadata. No text logs share the MCU UART.

## Install the diagnostic firmware

```sh
pio run -e xiao_nrf54lm20a -t upload
```

After RESET, blue blinks during the configured delay. With the default setting,
the microphone warms up during the final 100 ms, then green turns steady about
10 seconds after reset for the saved four-second interval. Subsequent indicators are:

| Indicator | Meaning |
| --- | --- |
| Slow blue, 0.5 s on / 0.5 s off | Recording retained; awaiting laptop |
| Fast blue, 0.1 s on / 0.1 s off | Transferring / waiting for ACKs |
| Blue steady | Complete transfer acknowledged |
| 1 red flash per repeating group | UART initialization failed |
| 2 red flashes | Microphone device, configuration or start failed |
| 3 red flashes | Microphone read, block length or DMA length check failed |
| 4 red flashes | Microphone STOP / disable wait failed |
| 5 red flashes | Transfer exhausted packet retries |
| 6 red flashes | CPU fatal error / assertion / fault |

Groups have a pause between them. Fatal flashing uses busy waits so it can
operate even if interrupts or the scheduler stop. Normal indicators use the
existing system workqueue and never trigger another recording.

## Query the retained recording

To query the saved recording, run this after green turns off and slow blue resumes:

```sh
.venv/bin/python tools/receive_audio.py --port /dev/cu.usbmodem55AEFA263 --probe
```

Expected output resembles:

```text
MCU: waiting for laptop (stage=4, error=0), saved_blocks=400, uart_rx_bytes=16, rx_dropped=0, uart_error_mask=0x0, retries=0, tx_frames=0, HELLOs=0
```

Counts can differ after earlier requests. For four seconds, 400 saved blocks
means 192,000 samples. PING neither captures again nor starts an audio transfer.
Run the normal receiver afterward to save the same block. Diagnostic requests
are serviced after capture, including capture errors. They are not serviced
after the one transfer session ends, during transfer, or after a CPU fatal
error. Use LEDs or SWD in those cases. Original firmware does not support PING.

## Locate remaining failures

Slow blue plus zero received bytes means no valid HELLO has reached the
application, or its response has not reached the laptop. Check the port and
close other serial programs. Bytes arriving with rejected frames suggest baud
or framing mismatch, or unexpected console output. A valid diagnostic reply
establishes both directions of the command/response path.

Nonzero `rx_dropped` means the MCU control queue filled. `uart_error_mask`
records UART errors observed by the RX callback. Increasing retries means
packets were sent without a matching valid acknowledgement.

The standard board connects nRF UART20 TX=P1.11 and RX=P1.10 to the SAMD11.
D6/D7 is a separate header UART, not the USB bridge. If accessible test pads
are available, decode P1.10 at 115200 8N1 while the receiver runs. HELLO is:

```text
50 43 4d 31 01 10 00 00 00 00 00 00 5c 27 7e f1
```

P1.11 should respond with a `PCM1` frame. These are the internal bridge
connections, separate from microphone CLOCK/DATA.

For SWD debugging, inspect the ELF's global volatile `audio_debug` structure:
stage, error, saved_blocks, rx_bytes, rx_dropped, uart_error_mask, retries,
tx_frames and hellos. It distinguishes a CPU fault, missing UART RX, failed
control parsing, and ACK timeout when serial is unavailable. Halting the CPU
during acquisition prevents timely DMA servicing and can disrupt the capture.

## Validation

The updated firmware compiles with the same pinned Seeed/Zephyr packages. It
uses **399,752 bytes** of linked RAM, leaving **123,512 bytes**, and passes the
32 KiB margin. All **13 receiver tests** pass, including a DTR-gated timeout and
recovery with DTR on, non-consuming PING, and STATUS before a complete capture.
No updated firmware was flashed or live transfer performed by the assistant.
