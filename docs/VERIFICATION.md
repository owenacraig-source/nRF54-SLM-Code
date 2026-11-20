# Verification record

## Transfer fix and diagnostics update

The reported timeout was traced to the receiver explicitly deasserting DTR.
The SAMD11 bridge source closes its UART in that state. The corrected receiver
asserts DTR before opening. See [debugging](DEBUGGING.md) for evidence, diagnostic
commands and LED patterns. The user's four-second green LED and clock activity
are consistent with completed acquisition before the USB handshake failed.

The updated diagnostic firmware builds with 399,752 bytes of linked RAM and
123,512 bytes of headroom. All 13 receiver tests pass, including the DTR
regression and non-consuming probe. No live board transfer was performed by
the assistant. The tables below record the original build, before the 88-byte
increase in static RAM for diagnostic state and work scheduling.

Checked on November 16, 2026. Firmware was compiled for the standard Seeed
Studio XIAO nRF54LM20A target. No board was flashed, and no physical microphone
recording or USB transfer was performed. The connected serial-port inventory
did not contain a XIAO.

## Builds actually run

`pio run -e xiao_nrf54lm20a` succeeded for 4000 ms and 4900 ms captures. The
project configuration and final built firmware were restored to **4000 ms**.
The final four-second build also includes the DMA MAXCNT readback check.

The build used these installed packages:

| Component | Version / location |
| --- | --- |
| Seeed platform | `a6fd41debbbcda13b5340fff50c9092ed3289194`, version `1.1.1+sha.a6fd41d` |
| Zephyr framework | `3.40400.260428` / Zephyr `4.4.0` |
| Framework directory | `/Users/owencraig/.platformio/packages/framework-zephyr-nrf54lm20` |
| Arm compiler | PlatformIO GCC Arm Embedded `8.2.1`, package `1.80201.181220` |
| Board / qualifier | `seeed-xiao-nrf54lm20a`, `xiao_nrf54lm20a/nrf54lm20a/cpuapp` |
| Build artifacts | Directory recorded in `.pio/audio-build-dir.txt` |

Build paths with spaces needed a project-local workaround: relative overlay
and configuration paths in `zephyr/CMakeLists.txt`, plus a canonical temporary
build directory selected by `tools/pio_build_dir.py`. No manual SDK source
patches were made for this application. Seeed's normal builder refreshes its
board definitions and compatibility files in the framework cache.

## Linked memory

The RAM boundary comes from the generated devicetree / `.config`, and used RAM
comes from `_image_ram_end` in the linked ELF. It includes PCM, DMA, static
driver/kernel state, and the allocated thread and interrupt stacks. The tool
`python3 tools/report_memory.py` independently reads the ELF symbols and checks
the PCM array size against the configured duration.

| Quantity | Four seconds | 4.9 seconds |
| --- | ---: | ---: |
| Physical SRAM | 524,288 B | 524,288 B |
| SRAM exposed to this application | 523,264 B | 523,264 B |
| PCM sample count | 192,000 | 235,200 |
| PCM array | 384,000 B | 470,400 B |
| Six DMA buffers, included below | 5,760 B | 5,760 B |
| All non-PCM linked RAM, including DMA and stacks | 15,664 B | 15,664 B |
| Total linked RAM | **399,664 B** | **486,064 B** |
| Unallocated linked headroom | **123,600 B** | **37,200 B** |
| Required minimum headroom | 32,768 B | 32,768 B |

The final four-second link used 54,032 bytes in the FLASH region. PlatformIO's
size summary uses different section accounting and the full physical RAM size;
the table uses the application's actual 511 KiB RAM region and image boundary.

The linker assertion in `zephyr/ram_guard.ld` passes for both builds. It uses
`_image_ram_end`, since Zephyr's `__kernel_ram_end` describes the entire MPU RAM
region rather than the end of allocated objects on this target. The firmware
uses no recording-sized heap allocation; recording storage and DMA buffers are
static. This is a static memory budget, not a measurement of stack high-water
marks on hardware.

For the same layout, a calculated 4940 ms capture uses 474,240 bytes of PCM and
leaves 33,360 bytes of headroom. At 4950 ms only 32,400 bytes remain, below the
32,768-byte guard. A 5000 ms capture physically fits but leaves 27,600 bytes,
also below the guard. These latter limits are calculations from the measured
layout, not additional duration builds. The conservative selectable extension
that was compiled is **4900 ms**.

## Source and documentation cross-checks

| Concern | Check and implementation |
| --- | --- |
| Peripheral | PDM20 performs PDM decimation and outputs signed 16-bit PCM; no I²S driver is used. |
| Clock | The LM20A-specific instance table and CLKSELECT define fixed ACLK=24 MHz and PCLK=32 MHz. PRESCALER is integer. The code requires ACLK / 10 / 50 = 48,000 Hz. |
| Clock startup | The pinned DMIC driver requests `CLOCK_CONTROL_NRF_SUBSYS_HF24M` for ACLK. nrfx maps this to XO24MSTART, which requests the crystal clock for PCLK24M. |
| 3.072 MHz request | Neither documented source divides to exactly 3.072 MHz. Nordic's general PDM feature list conflicts with the detailed clock/register tables. This implementation uses the detailed tables and explicitly reports 2.4 MHz. |
| PDM pins | PDM20 accepts P1/P3. The package table identifies P1.07 as clock-capable. I2C22 is disabled to release that pin. D1 maps to P1.31 in Seeed's connector definition. |
| Microphone edge | IM72D128V01 SELECT=GND drives after the falling edge; the receiver samples on rising. The pinned Zephyr driver maps mono `PDM_CHAN_RIGHT` to `NRF_PDM_EDGE_LEFTRISING`. |
| Hardware gain | Nordic GAINL/R use 0.5 dB steps: 0x28=default, 0x50=+20 dB adjustment. The HAL writes both after DMIC configure and before START. |
| Gain ordering | Gain occurs inside the hardware filter path before 16-bit PCM is written to RAM. No post-quantization software multiplier is used. |
| DMA lengths | LM20A SAMPLE.MAXCNT counts bytes and has a maximum of 32767. The pinned nrfx SoC fixup enables unified byte access, and its HAL converts the DMIC driver's 480 words to 960 bytes. The firmware checks this at runtime. |
| Optional HPF | Disabled with the Nordic HAL before START, then read back. Hardware decimation remains active. |
| Continuous capture | A 100 ms continuous preroll is discarded; exactly 400 consecutive 10 ms blocks are copied for the saved four seconds. DMA blocks are released promptly. |
| Stop | STOP is asynchronous; the application waits for peripheral disable, then frees queued full blocks before sending audio. |
| Serial RX | UART IRQ reception queues control bytes; the driver polling interface provides only a single-byte receive buffer and is unsuitable for acknowledging a burst while the thread sleeps. |
| One capture | There is one call to `capture_once()`, no outer capture loop, and no application reboot call. HELLO and packet retries only transfer the existing buffer. |

Primary references are [Nordic's PDM instance and register definitions](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-topic),
[master clock generator](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-concept_yyl_vv2_yr),
[module operation / gain](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/pdm.html-concept_eqb_fw2_yr),
[clock-pin requirements](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/chapters/pin.html-clock_pins),
[CSP98 pin assignments](https://docs.nordicsemi.com/r/bundle/ps_nrf54lm20a/page/chapters/pin.html-csp98),
and the [Infineon microphone datasheet](https://www.infineon.com/assets/row/public/documents/30/49/infineon-im72d128-datasheet-en.pdf).
The actual installed Zephyr DMIC driver, nrfx PDM/clock HAL, MDK types/fixups,
CRC implementation, serial driver and board devicetree were inspected as well.

## Laptop tests actually run

```sh
python3 -m unittest discover -s tests -v
```

All nine tests passed. The full capture test transfers 384,000 synthetic bytes,
fragments serial reads, inserts noise and a corrupt packet, simulates lost INFO,
DATA and END acknowledgements, and compares the saved WAV's complete payload to
the original bytes. It verifies a mono 48 kHz WAV with 192,000 frames and a
384,044-byte file. Other tests check CRC reference values, inconsistent metadata,
whole-recording / END checksum failures, sequence errors, timeout, MCU errors,
and preservation of an existing file when PCM validation fails.

`--help`, `--list-ports` and rejection of a non-`.wav` output filename were also
checked. Tests use a simulated sender; they do not exercise a physical MCU.

## First hardware run

Confirm a 2.4 MHz clock at D5 with suitable edge quality, then receive one WAV
while supplying a known tone. Check the JSON's rate, sample count and rail
statistics, and inspect the waveform for microphone signal and continuity.
After the blue completion LED, verify that no second recording starts until
RESET. Crystal tolerance determines the real-time accuracy of the nominal
48 kHz / four-second interval. Acoustic performance, electrical timing, clipping
behavior, and silicon-specific operation still require this physical check.
