#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include "diagnostics.h"

volatile struct audio_diagnostics audio_debug;

static const struct gpio_dt_spec green = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);
static const struct gpio_dt_spec red = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec blue = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

static void set_led(const struct gpio_dt_spec *led, bool on)
{
	if (gpio_is_ready_dt(led)) {
		(void)gpio_pin_set_dt(led, on);
	}
}

static void show(bool g, bool r, bool b)
{
	set_led(&green, g);
	set_led(&red, r);
	set_led(&blue, b);
}

static void update_indicator(struct k_work *work);
K_WORK_DELAYABLE_DEFINE(indicator, update_indicator);

static void update_indicator(struct k_work *work)
{
	ARG_UNUSED(work);
	static uint32_t previous_stage, tick;
	uint32_t stage = audio_debug.stage;
	if (stage != previous_stage) {
		previous_stage = stage;
		tick = 0;
	}
	if (stage >= AUDIO_STAGE_UART_ERROR) {
		unsigned int count = stage - AUDIO_STAGE_UART_ERROR + 1;
		unsigned int position = tick % (2 * count + 12);
		show(false, position < 2 * count && position % 2 == 0, false);
	} else {
		switch (stage) {
		case AUDIO_STAGE_WARMUP: show(tick % 2 == 0, false, false); break;
		case AUDIO_STAGE_RECORDING: show(true, false, false); break;
		case AUDIO_STAGE_TRANSFER: show(false, false, tick % 2 == 0); break;
		case AUDIO_STAGE_DONE: show(false, false, true); break;
		default: show(false, false, tick % 10 < 5); break;
		}
	}
	++tick;
	(void)k_work_reschedule(&indicator, K_MSEC(100));
}

void audio_stage_set(enum audio_stage stage, int error)
{
	audio_debug.error = error;
	audio_debug.stage = stage;
}

void audio_diagnostics_init(void)
{
	const struct gpio_dt_spec *leds[] = { &green, &red, &blue };
	for (size_t i = 0; i < ARRAY_SIZE(leds); ++i) {
		if (gpio_is_ready_dt(leds[i])) {
			(void)gpio_pin_configure_dt(leds[i], GPIO_OUTPUT_INACTIVE);
		}
	}
	audio_stage_set(AUDIO_STAGE_BOOT, 0);
	(void)k_work_reschedule(&indicator, K_NO_WAIT);
}

void audio_diagnostics_fatal(unsigned int reason)
{
	audio_stage_set(AUDIO_STAGE_FATAL, (int)reason);
	/* Fatal handling may run with interrupts disabled. Show six red flashes
	 * using busy waits, independently of the scheduler/workqueue.
	 */
	for (;;) {
		for (unsigned int i = 0; i < 6; ++i) {
			show(false, true, false);
			k_busy_wait(100000);
			show(false, false, false);
			k_busy_wait(100000);
		}
		k_busy_wait(1200000);
	}
}
