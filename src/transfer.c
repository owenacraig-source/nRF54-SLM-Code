#include <errno.h>
#include <string.h>

#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/util.h>

#include "transfer.h"
#include "diagnostics.h"

#define HEADER_SIZE 16U
#define ACK_TIMEOUT_MS 2000
#define MAX_ATTEMPTS 10U

static const uint8_t magic[4] = { 'P', 'C', 'M', '1' };
K_MSGQ_DEFINE(control_rx, sizeof(uint8_t), 256, 4);

static void rx_interrupt(const struct device *uart, void *user_data)
{
	ARG_UNUSED(user_data);
	int errors = uart_err_check(uart);
	if (errors > 0) {
		audio_debug.uart_error_mask |= (uint32_t)errors;
	}
	while (uart_irq_update(uart) && uart_irq_rx_ready(uart)) {
		uint8_t bytes[32];
		int count = uart_fifo_read(uart, bytes, sizeof(bytes));
		if (count <= 0) {
			break;
		}
		for (int i = 0; i < count; ++i) {
			++audio_debug.rx_bytes;
			/* A full queue drops controls; CRC + timeout + retransmission
			 * recover. It cannot corrupt the separately stored PCM.
			 */
			if (k_msgq_put(&control_rx, &bytes[i], K_NO_WAIT) != 0) {
				++audio_debug.rx_dropped;
			}
		}
	}
}

int audio_transfer_init(const struct device *uart)
{
	int ret = uart_irq_callback_user_data_set(uart, rx_interrupt, NULL);
	if (ret == 0) {
		uart_irq_rx_enable(uart);
	}
	return ret;
}

static uint32_t frame_crc(const uint8_t *header, const uint8_t *payload, size_t size)
{
	uint32_t crc = crc32_ieee(header, 12);
	return size ? crc32_ieee_update(crc, payload, size) : crc;
}

static void write_bytes(const struct device *uart, const uint8_t *data, size_t size)
{
	for (size_t i = 0; i < size; ++i) {
		uart_poll_out(uart, data[i]);
	}
}

static void write_frame(const struct device *uart, uint8_t type, uint32_t seq,
			const uint8_t *payload, uint16_t size)
{
	uint8_t header[HEADER_SIZE] = { 'P', 'C', 'M', '1', 1, type };
	sys_put_le16(size, header + 6);
	sys_put_le32(seq, header + 8);
	sys_put_le32(frame_crc(header, payload, size), header + 12);
	++audio_debug.tx_frames;
	write_bytes(uart, header, sizeof(header));
	write_bytes(uart, payload, size);
}

static void send_status(const struct device *uart)
{
	const uint32_t fields[] = {
		audio_debug.stage, (uint32_t)audio_debug.error, audio_debug.saved_blocks,
		audio_debug.rx_bytes, audio_debug.rx_dropped, audio_debug.uart_error_mask,
		audio_debug.retries, audio_debug.tx_frames, audio_debug.hellos,
	};
	uint8_t payload[sizeof(fields)];
	for (size_t i = 0; i < ARRAY_SIZE(fields); ++i) {
		sys_put_le32(fields[i], payload + 4 * i);
	}
	/* Diagnostic replies are best effort and need no ACK; PING is repeatable. */
	write_frame(uart, AUDIO_STATUS, 0, payload, sizeof(payload));
}

/* Controls have no payload. Sliding window recovers from partial commands,
 * line noise, and stale HELLO/ACK bytes without needing a serial console.
 */
static int wait_control(const struct device *uart, uint8_t type, uint32_t seq, int timeout_ms)
{
	uint8_t window[HEADER_SIZE];
	size_t used = 0;
	int64_t deadline = timeout_ms < 0 ? INT64_MAX : k_uptime_get() + timeout_ms;

	while (k_uptime_get() < deadline) {
		uint8_t byte;
		k_timeout_t timeout = timeout_ms < 0 ? K_FOREVER :
			K_MSEC(MAX(1, deadline - k_uptime_get()));
		if (k_msgq_get(&control_rx, &byte, timeout) != 0) {
			return -ETIMEDOUT;
		}
		if (used == sizeof(window)) {
			memmove(window, window + 1, sizeof(window) - 1);
			--used;
		}
		window[used++] = byte;
		if (used == sizeof(window) && memcmp(window, magic, 4) == 0 &&
		    window[4] == 1 && (window[5] == type ||
		    (type == AUDIO_HELLO && window[5] == AUDIO_PING)) &&
		    sys_get_le16(window + 6) == 0 && sys_get_le32(window + 8) == seq &&
		    sys_get_le32(window + 12) == frame_crc(window, NULL, 0)) {
			if (window[5] == AUDIO_PING) {
				send_status(uart);
				used = 0;
				continue;
			}
			if (type == AUDIO_HELLO) {
				++audio_debug.hellos;
			}
			return 0;
		}
	}
	return -ETIMEDOUT;
}

static int send_frame(const struct device *uart, uint8_t type, uint32_t seq,
		      const uint8_t *payload, uint16_t size)
{
	for (unsigned int attempt = 0; attempt < MAX_ATTEMPTS; ++attempt) {
		if (attempt > 0) {
			++audio_debug.retries;
		}
		write_frame(uart, type, seq, payload, size);
		if (wait_control(uart, AUDIO_ACK, seq, ACK_TIMEOUT_MS) == 0) {
			return 0;
		}
	}
	return -ETIMEDOUT;
}

int audio_transfer(const struct device *uart, const int16_t *pcm,
		   size_t sample_count, int capture_result)
{
	/* Capture is already over. The laptop can connect now or much later.
	 * HELLO requests this retained block, never another microphone capture.
	 */
	if (capture_result == 0) {
		audio_stage_set(AUDIO_STAGE_WAIT_HOST, 0);
	}
	(void)wait_control(uart, AUDIO_HELLO, 0, -1);
	send_status(uart);
	if (capture_result < 0) {
		uint8_t error[4];
		sys_put_le32((uint32_t)capture_result, error);
		return send_frame(uart, AUDIO_ERROR, 0, error, sizeof(error));
	}
	audio_stage_set(AUDIO_STAGE_TRANSFER, 0);

	const uint8_t *data = (const uint8_t *)pcm; /* Cortex-M33 is little endian. */
	const size_t size = sample_count * sizeof(*pcm);
	const uint32_t pcm_crc = crc32_ieee(data, size);
	uint8_t info[48];
	const uint32_t fields[] = {
		AUDIO_RATE_HZ, sample_count, size, AUDIO_PDM_HZ,
		(uint32_t)(CONFIG_APP_GAIN_DB * 1000), 1, 16,
		CONFIG_APP_CAPTURE_DURATION_MS, AUDIO_WARMUP_MS, pcm_crc,
		AUDIO_DECIMATION, 1 /* flag bit 0: PDM high-pass filter disabled */,
	};
	BUILD_ASSERT(sizeof(info) == sizeof(fields));
	for (size_t i = 0; i < ARRAY_SIZE(fields); ++i) {
		sys_put_le32(fields[i], info + 4 * i);
	}
	int ret = send_frame(uart, AUDIO_INFO, 0, info, sizeof(info));
	if (ret < 0) {
		return ret;
	}
	uint32_t seq = 1;
	for (size_t offset = 0; offset < size; ++seq) {
		uint16_t count = MIN(size - offset, AUDIO_FRAME_PAYLOAD);
		ret = send_frame(uart, AUDIO_DATA, seq, data + offset, count);
		if (ret < 0) {
			return ret;
		}
		offset += count;
	}
	uint8_t end[4];
	sys_put_le32(pcm_crc, end);
	return send_frame(uart, AUDIO_END, seq, end, sizeof(end));
}

