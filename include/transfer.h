#ifndef AUDIO_TRANSFER_H
#define AUDIO_TRANSFER_H

#include <stddef.h>
#include <stdint.h>
#include <zephyr/device.h>

#define AUDIO_RATE_HZ 48000U
#define AUDIO_PDM_HZ 2400000U
#define AUDIO_DECIMATION 50U
#define AUDIO_WARMUP_MS 100U
#define AUDIO_FRAME_PAYLOAD 1024U

enum audio_frame_type {
	AUDIO_INFO = 1,
	AUDIO_DATA = 2,
	AUDIO_END = 3,
	AUDIO_ERROR = 4,
	AUDIO_STATUS = 5,
	AUDIO_HELLO = 16,
	AUDIO_ACK = 17,
	AUDIO_PING = 18,
};

/* Wait for the laptop after capture, send one retained recording, then return.
 * A negative capture_result sends an error instead of a partial recording.
 */
int audio_transfer_init(const struct device *uart);
int audio_transfer(const struct device *uart, const int16_t *pcm,
		   size_t sample_count, int capture_result);

#endif

