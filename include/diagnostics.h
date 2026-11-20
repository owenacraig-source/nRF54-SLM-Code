#ifndef AUDIO_DIAGNOSTICS_H
#define AUDIO_DIAGNOSTICS_H

#include <stdint.h>

enum audio_stage {
	AUDIO_STAGE_BOOT = 1,
	AUDIO_STAGE_WARMUP = 2,
	AUDIO_STAGE_RECORDING = 3,
	AUDIO_STAGE_WAIT_HOST = 4,
	AUDIO_STAGE_TRANSFER = 5,
	AUDIO_STAGE_DONE = 6,
	AUDIO_STAGE_UART_ERROR = 10,
	AUDIO_STAGE_CONFIG_ERROR = 11,
	AUDIO_STAGE_READ_ERROR = 12,
	AUDIO_STAGE_STOP_ERROR = 13,
	AUDIO_STAGE_TRANSFER_ERROR = 14,
	AUDIO_STAGE_FATAL = 15,
};

/* Also visible as the 'audio_debug' symbol through an SWD debugger.
 * RX fields have one ISR writer; snapshots may span adjacent RX events.
 */
struct audio_diagnostics {
	uint32_t stage;
	int32_t error;
	uint32_t saved_blocks;
	uint32_t rx_bytes;
	uint32_t rx_dropped;
	uint32_t uart_error_mask;
	uint32_t retries;
	uint32_t tx_frames;
	uint32_t hellos;
};

extern volatile struct audio_diagnostics audio_debug;
void audio_diagnostics_init(void);
void audio_stage_set(enum audio_stage stage, int error);
void audio_diagnostics_fatal(unsigned int reason);

#endif
