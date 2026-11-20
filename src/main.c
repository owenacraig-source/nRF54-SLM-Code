#include <errno.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/audio/dmic.h>
#include <zephyr/device.h>
#include <zephyr/fatal.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>
#include <hal/nrf_pdm.h>

#include "transfer.h"
#include "diagnostics.h"

#define MIC_NODE DT_ALIAS(audio_mic)
#define UART_NODE DT_ALIAS(audio_uart)
#define BLOCK_MS 10U
#define BLOCK_SAMPLES (AUDIO_RATE_HZ * BLOCK_MS / 1000U)
#define BLOCK_BYTES (BLOCK_SAMPLES * sizeof(int16_t))
#define DMA_BLOCKS 6U
#define CAPTURE_SAMPLES (AUDIO_RATE_HZ * CONFIG_APP_CAPTURE_DURATION_MS / 1000U)
#define CAPTURE_BLOCKS (CONFIG_APP_CAPTURE_DURATION_MS / BLOCK_MS)
#define WARMUP_BLOCKS (AUDIO_WARMUP_MS / BLOCK_MS)

BUILD_ASSERT(CONFIG_SOC_NRF54LM20A_CPUAPP, "This configuration targets nRF54LM20A CPUAPP");
BUILD_ASSERT(CONFIG_APP_CAPTURE_DURATION_MS % BLOCK_MS == 0, "Duration must be a multiple of 10 ms");
BUILD_ASSERT(BLOCK_BYTES <= UINT16_MAX, "DMIC API block_size is 16 bits");
BUILD_ASSERT(BLOCK_BYTES <= PDM_SAMPLE_MAXCNT_BUFFSIZE_Max, "PDM DMA byte counter limit exceeded");
BUILD_ASSERT(CAPTURE_SAMPLES * sizeof(int16_t) + DMA_BLOCKS * BLOCK_BYTES + 32768U
	     <= DT_REG_SIZE(DT_CHOSEN(zephyr_sram)), "PCM, DMA and spare RAM cannot fit");

static const struct device *const mic = DEVICE_DT_GET(MIC_NODE);
static const struct device *const audio_uart = DEVICE_DT_GET(UART_NODE);
static NRF_PDM_Type *const pdm = (NRF_PDM_Type *)DT_REG_ADDR(MIC_NODE);

/* No 32-bit duplicate or heap allocation of the recording. */
static int16_t pcm[CAPTURE_SAMPLES] __aligned(4);
K_MEM_SLAB_DEFINE_STATIC(dma_slab, BLOCK_BYTES, DMA_BLOCKS, 4);

static int configure_mic(void)
{
	struct pcm_stream_cfg stream = {
		.pcm_rate = AUDIO_RATE_HZ,
		.pcm_width = 16,
		.block_size = BLOCK_BYTES,
		.mem_slab = &dma_slab,
	};
	struct dmic_cfg cfg = {
		.io = {
			.min_pdm_clk_freq = AUDIO_PDM_HZ,
			.max_pdm_clk_freq = AUDIO_PDM_HZ,
			.min_pdm_clk_dc = 40,
			.max_pdm_clk_dc = 60,
		},
		.streams = &stream,
		.channel = {
			.req_num_streams = 1,
			.req_num_chan = 1,
			/* IM72D128V01 SELECT=GND drives after FALLING; sample RISING.
			 * Nordic's RIGHT mapping selects EDGE=LeftRising even in mono.
			 */
			.req_chan_map_lo = dmic_build_channel_map(0, 0, PDM_CHAN_RIGHT),
		},
	};
	int ret = dmic_configure(mic, &cfg);
	if (ret < 0) {
		return ret;
	}

	/* This pinned Zephyr 4.4 DMIC API has no gain field. Apply the Nordic
	 * HAL setting once AFTER configure (which resets gain), BEFORE START.
	 * 0x28 is 0 dB, each register step is 0.5 dB: +20 dB -> 0x50.
	 * Hardware applies gain in its filter path before the 16-bit DMA output.
	 */
	const nrf_pdm_gain_t gain = NRF_PDM_GAIN_DEFAULT + 2 * CONFIG_APP_GAIN_DB;
	nrf_pdm_gain_set(pdm, gain, gain);
	/* Preserve the microphone's LF response for subsequent laptop filtering.
	 * The mandatory decimation low-pass remains; disable the optional HPF.
	 */
	nrf_pdm_filter_hp_pole_enable(pdm, false);

	nrf_pdm_gain_t gain_l, gain_r;
	nrf_pdm_mode_t mode;
	nrf_pdm_edge_t edge;
	nrf_pdm_gain_get(pdm, &gain_l, &gain_r);
	nrf_pdm_mode_get(pdm, &mode, &edge);
	if (nrf_pdm_prescaler_get(pdm) != 10U ||
	    pdm->RATIO != NRF_PDM_RATIO_50X ||
	    pdm->CLKSELECT != NRF_PDM_MCLKSRC_ACLK ||
	    gain_l != gain || gain_r != gain ||
	    mode != NRF_PDM_MODE_MONO || edge != NRF_PDM_EDGE_LEFTRISING ||
	    pdm->FILTER.HPDISABLE != PDM_FILTER_HPDISABLE_DISABLE_Disable) {
		return -EINVAL; /* Never silently label an approximate rate as 48 kHz. */
	}
	return 0;
}

static int stop_mic(void)
{
	int ret = dmic_trigger(mic, DMIC_TRIGGER_STOP);
	if (ret < 0) {
		return ret;
	}

	/* STOP is asynchronous. Do not start serial transfer while DMA runs. */
	int64_t deadline = k_uptime_get() + 1000;
	while (nrf_pdm_enable_check(pdm)) {
		if (k_uptime_get() >= deadline) {
			return -ETIMEDOUT;
		}
		k_sleep(K_MSEC(1));
	}
	/* Full blocks that were queued just before STOP still belong to us. */
	void *buffer;
	size_t size;
	while (dmic_read(mic, 0, &buffer, &size, 0) == 0) {
		k_mem_slab_free(&dma_slab, buffer);
	}
	return 0;
}

static int capture_once(void)
{
	if (!device_is_ready(mic)) {
		audio_stage_set(AUDIO_STAGE_CONFIG_ERROR, -ENODEV);
		return -ENODEV;
	}
	int ret = configure_mic();
	if (ret < 0) {
		audio_stage_set(AUDIO_STAGE_CONFIG_ERROR, ret);
		return ret;
	}

	/* Start the clock one warmup interval before the first saved sample.
	 * Boot, supply settling and configuration already count toward the delay.
	 */
	int64_t wait_ms = (int64_t)CONFIG_APP_RECORD_DELAY_MS - AUDIO_WARMUP_MS - k_uptime_get();
	if (wait_ms > 0) {
		k_sleep(K_MSEC(wait_ms));
	}
	ret = dmic_trigger(mic, DMIC_TRIGGER_START);
	if (ret < 0) {
		audio_stage_set(AUDIO_STAGE_CONFIG_ERROR, ret);
		return ret;
	}
	audio_stage_set(AUDIO_STAGE_WARMUP, 0);

	/* The mic needs CLOCK running to settle. Discard 100 ms continuously,
	 * without stopping/restarting it; then retain exactly 192000 samples.
	 */
	for (size_t block = 0; block < WARMUP_BLOCKS + CAPTURE_BLOCKS; ++block) {
		void *buffer = NULL;
		size_t size = 0;
		ret = dmic_read(mic, 0, &buffer, &size, 1000);
		if (ret < 0) {
			break;
		}
		if (size != BLOCK_BYTES) {
			k_mem_slab_free(&dma_slab, buffer);
			ret = -EIO;
			break;
		}
		/* LM20A MAXCNT counts BYTES; the nrfx HAL converts the driver's
		 * 480-word buffer length to 960 bytes on this SoC.
		 */
		if (pdm->SAMPLE.MAXCNT != BLOCK_BYTES) {
			k_mem_slab_free(&dma_slab, buffer);
			ret = -EINVAL;
			break;
		}
		if (block >= WARMUP_BLOCKS) {
			memcpy(&pcm[(block - WARMUP_BLOCKS) * BLOCK_SAMPLES], buffer, size);
			++audio_debug.saved_blocks;
		} else if (block == WARMUP_BLOCKS - 1U) {
			audio_stage_set(AUDIO_STAGE_RECORDING, 0);
		}
		k_mem_slab_free(&dma_slab, buffer);
	}
	if (ret < 0) {
		audio_stage_set(AUDIO_STAGE_READ_ERROR, ret);
	}
	int stop_ret = stop_mic();
	if (ret == 0 && stop_ret < 0) {
		audio_stage_set(AUDIO_STAGE_STOP_ERROR, stop_ret);
	}
	return ret < 0 ? ret : stop_ret;
}

int main(void)
{
	audio_diagnostics_init();
	if (!device_is_ready(audio_uart)) {
		audio_stage_set(AUDIO_STAGE_UART_ERROR, -ENODEV);
		return -ENODEV;
	}
	int ret = audio_transfer_init(audio_uart);
	if (ret < 0) {
		audio_stage_set(AUDIO_STAGE_UART_ERROR, ret);
		return ret;
	}
	/* External mic is already powered from 3V3; allow supply ramp settling. */
	k_sleep(K_MSEC(100));
	int capture_result = capture_once();
	ret = audio_transfer(audio_uart, pcm, CAPTURE_SAMPLES, capture_result);
	if (capture_result == 0 && ret == 0) {
		audio_stage_set(AUDIO_STAGE_DONE, 0);
	} else if (capture_result == 0) {
		audio_stage_set(AUDIO_STAGE_TRANSFER_ERROR, ret);
	}
	/* Zephyr terminates main; no capture/retry timer, reboot, or outer loop.
	 * Only a reset (including a power cycle) starts another capture.
	 */
	return 0;
}

void k_sys_fatal_error_handler(unsigned int reason, const struct arch_esf *esf)
{
	ARG_UNUSED(esf);
	audio_diagnostics_fatal(reason);
}

