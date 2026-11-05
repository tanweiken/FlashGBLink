/*
 * Host-side harness for payload.c.
 *
 * Build with -DPAYLOAD_SIM and feed it one command frame per line of hex
 * words on stdin. Each input word is clocked through the real dispatch loop;
 * the words the payload would send back are printed as hex on stdout.
 *
 * Cartridge and save accesses go to plain arrays. This checks the command and
 * plan logic, not flash timing or erase algorithms.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../payload/payload.c"

#define SIM_CART_SIZE 0x2000000u
#define SIM_SAVE_SIZE 0x20000u
#define SIM_MAX_WORDS 4096u

static u8 sim_cart[SIM_CART_SIZE];
static u8 sim_save[SIM_SAVE_SIZE];
static u32 sim_in[SIM_MAX_WORDS];
static u32 sim_out[SIM_MAX_WORDS];
static u32 sim_in_len;
static u32 sim_in_pos;

static u8 *sim_map(u32 address, u32 *offset)
{
	if (address >= 0x0E000000u && address < 0x0E000000u + SIM_SAVE_SIZE) {
		*offset = address - 0x0E000000u;
		return sim_save;
	}
	if (address >= 0x08000000u && address < 0x08000000u + SIM_CART_SIZE) {
		*offset = address - 0x08000000u;
		return sim_cart;
	}
	*offset = 0;
	return NULL;
}

u32 sim_sio_xfer(u32 out)
{
	u32 value = sim_in_pos < sim_in_len ? sim_in[sim_in_pos]
					    : 0xFFFFFFFFu;
	if (sim_in_pos < SIM_MAX_WORDS)
		sim_out[sim_in_pos] = out;
	sim_in_pos++;
	return value;
}

u16 sim_rd16(u32 address)
{
	u32 offset;
	u8 *base = sim_map(address, &offset);

	if (base == NULL || offset + 1u >= (base == sim_save ? SIM_SAVE_SIZE
							     : SIM_CART_SIZE))
		return 0xFFFFu;
	return (u16)(base[offset] | ((u16)base[offset + 1u] << 8));
}

void sim_wr16(u32 address, u16 value)
{
	u32 offset;
	u8 *base = sim_map(address, &offset);

	if (base == NULL || offset + 1u >= (base == sim_save ? SIM_SAVE_SIZE
							     : SIM_CART_SIZE))
		return;
	base[offset] = (u8)value;
	base[offset + 1u] = (u8)(value >> 8);
}

u8 sim_rd8(u32 address)
{
	u32 offset;
	u8 *base = sim_map(address, &offset);

	if (base == NULL)
		return 0xFFu;
	return base[offset];
}

void sim_wr8(u32 address, u8 value)
{
	u32 offset;
	u8 *base = sim_map(address, &offset);

	if (base != NULL)
		base[offset] = value;
}

static void sim_init(void)
{
	memset(sim_cart, 0xFF, sizeof(sim_cart));
	memset(sim_save, 0xFF, sizeof(sim_save));
	sio_init();
	screen_init();
	crc32_init();
	set_default_plan();
	reply = ST_OK;
}

int main(void)
{
	char line[1u << 16];

	sim_init();
	while (fgets(line, sizeof(line), stdin) != NULL) {
		char *cursor = line;
		char *end;
		u32 i;

		sim_in_len = 0;
		sim_in_pos = 0;
		while (sim_in_len < SIM_MAX_WORDS) {
			unsigned long value = strtoul(cursor, &end, 16);

			if (end == cursor)
				break;
			sim_in[sim_in_len++] = (u32)value;
			cursor = end;
		}
		if (sim_in_len == 0)
			continue;
		while (sim_in_pos < sim_in_len)
			dispatch(sim_sio_xfer(reply));
		for (i = 0; i < sim_in_pos; i++)
			printf("%s%08X", i ? " " : "", sim_out[i]);
		printf("\n");
		fflush(stdout);
	}
	return 0;
}
