/*
 * Regression test for screen redraw between flash sessions.
 *
 * After the completion message is shown, the next progress update starts a
 * new write. It must rebuild the whole layout: leaving the completion text up
 * makes the screen look half-initialised while the next game is programmed.
 */

#include <stdio.h>
#include <string.h>

#define PAYLOAD_SIM
#include "payload/payload.c"

static u16 reference[SCREEN_W * SCREEN_H];

u32 sim_sio_xfer(u32 out)
{
	(void)out;
	return 0;
}

u16 sim_rd16(u32 addr)
{
	(void)addr;
	return 0xFFFFu;
}

void sim_wr16(u32 addr, u16 value)
{
	(void)addr;
	(void)value;
}

u8 sim_rd8(u32 addr)
{
	(void)addr;
	return 0xFFu;
}

void sim_wr8(u32 addr, u8 value)
{
	(void)addr;
	(void)value;
}

int main(void)
{
	u32 i;

	/* Pixels a brand-new write session shows at 0%. */
	screen_init();
	show_progress(0u, 0u);
	memcpy(reference, sim_vram, sizeof(reference));

	/* Finish a write, then start another one. */
	dispatch(HEADER(OP_DONE, 0));
	if (memcmp(reference, sim_vram, sizeof(reference)) == 0)
		return 1; /* completion screen never drew */

	dispatch(HEADER(OP_PROGRESS, 0));
	if (memcmp(reference, sim_vram, sizeof(reference)) != 0) {
		for (i = 0; i < SCREEN_W * SCREEN_H; i++)
			if (reference[i] != sim_vram[i]) {
				fprintf(stderr,
					"first stale pixel at (%u, %u): "
					"expected %04X, got %04X\n",
					i % SCREEN_W, i / SCREEN_W,
					reference[i], sim_vram[i]);
				break;
			}
		return 2;
	}

	return 0;
}
