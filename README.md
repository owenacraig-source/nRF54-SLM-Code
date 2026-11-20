# One audio capture per reset

Zephyr / PlatformIO firmware for the **Seeed Studio XIAO nRF54LM20A** and an
external **Infineon IM72D128V01** microphone. It captures one continuous,
four-second, mono 48 kHz recording, applies +20 dB in the PDM hardware, retains
192,000 signed 16-bit PCM samples in RAM, and then transfers them to a laptop.
The Python receiver saves a normal PCM WAV plus a JSON metadata file.

**Clock correction:** the documented LM20A clock tree cannot generate exactly
3.072 MHz using its PDM peripheral. This project uses **2.400 MHz PDM / 50 =
48,000 PCM samples/s**. It does not substitute a nearby rate and label it 48 kHz.
The microphone supports this clock. See the documentation checks below.

## Wiring

| IM72D128V01 connection | XIAO connection |
| --- | --- |
| VDD | 3V3 |
| GND | GND |
| CLOCK | **D5 / P1.07** |
| DATA | **D1 / P1.31** |
| SELECT / L/R | GND |

Use **D5**, correcting the earlier D0 suggestion: P1.07 is clock-capable, while
P1.00 is not. The overlay disables I2C22, which would otherwise own D5 as SCL.
It routes PDM20 to the external microphone; onboard microphone pins are unused.

Power the microphone at the same 3.3 V logic level as the XIAO. Place a 1 µF
bypass capacitor close to VDD, and keep CLOCK/DATA/GND connections short.
If using a microphone breakout, check its regulator and existing bypass parts.
Grounded SELECT means this microphone drives DATA after the falling edge;
the firmware samples on the **rising** edge. Nordic names this mono setting
`EDGE=LeftRising`, selected through Zephyr's `PDM_CHAN_RIGHT` mapping. The WAV
still contains one mono channel. These details follow the
[IM72D128V01 datasheet, sections 3.2–3.5](https://www.infineon.com/assets/row/public/documents/30/49/infineon-im72d128-datasheet-en.pdf).

## Build and flash

Open this folder as a PlatformIO project. In a terminal with `pio` installed:

```sh
pio run -e xiao_nrf54lm20a
pio run -e xiao_nrf54lm20a -t upload
```

The platform URL is pinned to Seeed commit
`a6fd41debbbcda13b5340fff50c9092ed3289194`; its LM20A package supplies Zephyr
4.4.0 (`framework-zephyr-nrf54lm20` version `3.40400.260428`). The board ID is
`seeed-xiao-nrf54lm20a`. The supplied upload method uses the onboard CMSIS-DAP
debugger. This follows [Seeed's PlatformIO platform](https://github.com/Seeed-Studio/platform-seeedboards/tree/a6fd41debbbcda13b5340fff50c9092ed3289194).

The binary transport uses **UART20 through the board's SAMD11 USB-to-UART bridge**,
115200 baud, 8 data bits, no parity, 1 stop bit. Connect the board's USB data
cable to the laptop. The application disables logging and console output so
text cannot contaminate PCM packets. Close PlatformIO Monitor and other serial
terminals before starting the receiver.

This targets Seeed's standard board definition, whose default hardware revision
is 1.0.0. If a different board lacks the SAMD11 bridge, its native USB transport
requires a different configuration; changing the board ID alone is insufficient.

Zephyr's PlatformIO generators mishandle some build paths containing spaces.
`tools/pio_build_dir.py` automatically selects a unique temporary build directory
when needed and records its path in `.pio/audio-build-dir.txt`. `firmware.elf`,
`firmware.hex`, the map, `.config` and generated devicetree are in that directory.

## Receive a WAV

macOS / Linux, from the project folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.txt
.venv/bin/python tools/receive_audio.py --list-ports
.venv/bin/python tools/receive_audio.py --port /dev/cu.usbmodemXXXX --output recordings/capture.wav
```

Replace the example port with the port reported for the XIAO. Linux commonly
uses `/dev/ttyACM0`. On Windows, create the environment with `py -3 -m venv .venv`
and use `.venv\Scripts\python.exe` in place of `.venv/bin/python`, with a port
such as `COM5`.

The receiver can start before reset or after recording is complete. The MCU
retains the block and waits for its request, so opening the port late does not
lose the recording. Transfer typically takes about 35–45 seconds at 115200 baud.
The firmware sends numbered packets with CRC32, waits for each acknowledgement,
and retries unacknowledged packets up to ten times. The receiver ignores exact
duplicates and checks a CRC32 for the whole recording before publishing the WAV.

Successful output is `capture_001.wav` and `capture_001.json`, then
`capture_002.wav`, etc. `--output capture_fixed.wav` produces
`capture_fixed_001.wav`, `capture_fixed_002.wav`, and so on. Pass `--raw` to also
save matching `.s16le` headerless signed 16-bit little-endian mono samples.
The next number is stored in `.capture-counter.sqlite3` in the project root,
outside the recordings folder. **Keep that state file**: deleting recordings,
including their whole directory, does not reset numbering. One counter is
shared across output basenames/folders. Numbers grow beyond 999 automatically;
interrupted saves can leave gaps. Existing WAV/JSON/raw names are skipped.
Keep using the same receiver command; open the path printed after `Saved`.
`--overwrite` remains accepted but ignored. The JSON
includes receipt time, recording ID and a SHA-256 of the received PCM bytes;
`--debug` also prints that checksum. Receipt time is the laptop's save time,
not the microphone's acquisition time. A standard
four-second WAV is **384,044 bytes**, including its 44-byte header.

For a new recording, **press and release RESET**. The blue LED blinks during
the delay. About **10 seconds after reset**, the green LED turns steady and the
four-second recording begins. The firmware counts microphone warmup within
those 10 seconds. Change `CONFIG_APP_RECORD_DELAY_MS=10000` in
`zephyr/prj.conf` and reflash to adjust the delay. Use `0` to start as soon as
the microphone is ready. If the delay plus recording time approaches the
receiver's 60-second wait, increase `--start-timeout` (in seconds) or start
the receiver after the recording.
The firmware records only once each time it starts, transfers once, and then
terminates its main thread. Packet retries never trigger another capture.
Power cycling or reflashing also starts the application again.

Green steady means recording. Slow blue blinking after green turns off means
the capture is retained and waiting for the laptop. Fast blue blinking means
transferring; steady blue means complete. Repeated groups of red flashes identify
a failure; see [debugging instructions](docs/DEBUGGING.md) for the counts.
Before the saved interval, CLOCK runs for 100 ms of discarded startup
audio. After the last saved block, a brief DMA pipeline tail is discarded while
STOP completes. The WAV itself contains exactly 192,000 consecutive samples.

**DTR must be on.** The SAMD11 bridge closes its UART when DTR is off, even
though the USB serial port still appears. The original receiver's
`port.dtr = False` was a bug. The updated receiver asserts DTR and prints
`DTR=ON`. Use `--debug` for handshake details. With updated firmware, `--probe`
queries status without consuming the recording. See [debugging](docs/DEBUGGING.md).

## Memory and longer recordings

PCM uses `48,000 × 2 = 96,000 bytes/s`. Four seconds requires **384,000 bytes
(375 KiB)**. Six 10 ms DMA buffers require another **5,760 bytes**; the code
copies them into the recording and immediately releases each DMA buffer. There
is no second recording-sized array and no capture heap allocation.

The chip has 512 KiB RAM. This Zephyr board exposes 511 KiB (523,264 bytes) to
the application. Actual linked RAM usage, including allocated thread stacks,
kernel data, DMA and PCM, is recorded in [docs/VERIFICATION.md](docs/VERIFICATION.md).
Run this after rebuilding to inspect the actual ELF rather than estimating only
the sample array:

```sh
python3 tools/report_memory.py
```

The linker rejects any build leaving less than **32 KiB unallocated RAM**, in
addition to the already allocated stacks. A **4.9-second** capture is also
build-checked. Set `CONFIG_APP_CAPTURE_DURATION_MS=4900` in `zephyr/prj.conf` and
rebuild to select it. Durations must be multiples of 10 ms. The default remains
4000 ms. About 4.94 seconds is the calculated limit with this layout and margin;
5 seconds fits physically but does not retain the chosen 32 KiB margin.
Recheck the ELF whenever drivers, stacks or configuration change.

Longer recordings need storage or a faster streaming transport. A future version
could write PCM blocks to the board's external SPI flash during capture, then
send them afterward. That storage path is not implemented here. The current
115200-baud link cannot stream 96,000 bytes/s in real time.

The saved data is **decimated PCM**, not the raw PDM bitstream. Four seconds of
packed raw 3.072 MHz PDM would require 1,536,000 bytes and cannot fit in this RAM.

## Gain and processing

The firmware configures the PDM filter's gain **before** starting capture. The
pinned Zephyr driver has no public gain field, so a small Nordic HAL call sets
GAINL and GAINR after `dmic_configure()` and before `DMIC_TRIGGER_START`.
`0x28 + 2 × 20 = 0x50` selects +20 dB relative to the peripheral's default gain.
The hardware produces the final 16-bit samples; multiplying already truncated
PCM in software would not meet the requested gain ordering.

The optional PDM high-pass filter is disabled so laptop processing can control
low-frequency filtering. The mandatory hardware decimation filter remains.
There is no automatic peak normalization, resampling or extra software filtering.
The JSON records the clock, gain, sample count, duration, CRC and signal statistics.

For floating-point DSP, divide signed samples by 32768.0. If the receiver reports
samples at the PCM rails, reduce `CONFIG_APP_GAIN_DB` in `zephyr/prj.conf` and
rebuild; +20 dB is a 10× amplitude adjustment and can clip loud input.

## Documentation checks

The flowchart's I²S block is implemented using the **PDM peripheral**, which
provides the microphone clock, decimation and 16-bit PCM output. See
[Nordic's PDM operation](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-concept_eqb_fw2_yr).

The [instance configuration](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-topic)
and [CLKSELECT register](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-register.clkselect)
specify fixed 24/32 MHz sources;
[PRESCALER](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-register.prescaler)
is an integer divisor. Neither `24,000,000 / 3,072,000` nor
`32,000,000 / 3,072,000` is an integer. This conflicts with the general feature
summary listing 3.072 MHz. The implementation follows the register-level model:
ACLK / 10 = 2.4 MHz, with the supported
[50× ratio](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-register.ratio).
If 3.072 MHz itself is mandatory, obtain Nordic clarification or change the
clock/capture hardware; this code does not claim to generate it.

[GAINL](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-register.gainl)
defines the 0.5 dB steps and +20 dB endpoint.
[Clock-pin requirements](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/chapters/pin.html-clock_pins)
and the [CSP98 pin table](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/chapters/pin.html-csp98)
establish the corrected D5 clock selection. The firmware reads back clock,
decimation, gain, mono mode, edge and high-pass settings and fails the capture if
they differ from the intended configuration.

Software compilation and protocol tests have been performed. **Physical microphone
capture, clock measurement and USB transfer on the board have not been tested.**
See [docs/VERIFICATION.md](docs/VERIFICATION.md) for the exact checks and limits.
