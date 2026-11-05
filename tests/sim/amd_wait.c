/*
 * Regression test for the AMD write-buffer wait in payload.c.
 *
 * Data# polling is only valid at the last word loaded into the write buffer.
 * The profile's wait row names the sector/program address (SA/PA), so the
 * payload must not use that address for WAIT_PD; it has to poll the final
 * data address of the current chunk instead.
 */

#define PAYLOAD_SIM
#include "payload/payload.c"

static u32 poll_addr;
static u16 poll_data;
static int poll_done;

u32 sim_sio_xfer(u32 out)
{
	(void)out;
	return 0;
}

u16 sim_rd16(u32 addr)
{
	if (addr != CART_BASE + poll_addr)
		return 0x0000u;
	if (!poll_done) {
		poll_done = 1;
		return (u16)(poll_data ^ 0x80u);
	}
	return poll_data;
}

void sim_wr16(u32 addr, u16 value)
{
	(void)addr;
	(void)value;
}

u8 sim_rd8(u32 addr)
{
	(void)addr;
	return 0;
}

void sim_wr8(u32 addr, u8 value)
{
	(void)addr;
	(void)value;
}

int main(void)
{
	struct write_cmd cmd;

	cmd.addr_kind = ADDR_SA;
	cmd.addr = 0;
	cmd.val_kind = VAL_CONST;
	cmd.val = 0x29;
	cmd.wait_kind = WAIT_PD;
	cmd.wait_addr = 0;
	cmd.wait_exp = 0;
	cmd.wait_mask = 0xFFFFu;

	poll_addr = 0x1236;
	poll_data = 0x1234;
	poll_done = 0;
	if (!plan_wait(0x1000, &cmd, poll_data, poll_addr))
		return 1;

	/* The old code polled the sector/program address (base + wait_addr).
	 * Make sure that address does not satisfy the wait. */
	poll_addr = 0x1000;
	poll_data = 0x1234;
	poll_done = 0;
	if (plan_wait(0x1000, &cmd, poll_data, 0x1236))
		return 2;

	return 0;
}
