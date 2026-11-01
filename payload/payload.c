/*
 * GBA cart bus helper, loaded into EWRAM through the BIOS multiboot loader.
 *
 * Transport: GBA SIO normal mode, 32-bit, external clock (slave). Each word the
 * host clocks in is exchanged for a word this program preloaded. The host is
 * gbasend-pico behaving as a plain SPI bridge; it paces one word at a time, so
 * a blocking exchange is safe.
 *
 * This program knows nothing about flash chips. It only performs 16-bit
 * cartridge stores, 16-bit cartridge loads, and a soft reset. The host composes
 * the chip command sequences (for example from a FlashGBX profile).
 */

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#define REG_SIODATA32 (*(volatile u32 *)0x04000120)
#define REG_SIOCNT (*(volatile u16 *)0x04000128)
#define REG_RCNT (*(volatile u16 *)0x04000134)

#define SIOCNT_32BIT 0x1000u
#define SIOCNT_START 0x0080u

#define CART_BASE 0x08000000u

#define HDR_MAGIC 0x5Au
#define HEADER(op, n) ((u32)(HDR_MAGIC << 24) | ((u32)(op) << 8) | ((n) & 0xFFu))

#define OP_WRITE_SEQ 0x20u
#define OP_READ 0x30u
#define OP_BOOT 0x40u
#define OP_WRITE_BLOCK 0x50u

#define BLOCK_WORDS 1024u /* packed 32-bit words, 4 KiB of cartridge data */
#define CHUNK_WORDS 32u   /* 16-bit cartridge words per buffer write, 64 B */

#define PING 0x5A5A5A5Au

#define ST_OK 0xA5A50000u
#define ST_ECHO 0xA5A5E000u
#define ST_BAD 0xA5A500E1u

static u32 reply = ST_OK;
static u32 block_buffer[BLOCK_WORDS];

static inline u32 sio_xfer(u32 out)
{
	REG_SIODATA32 = out;
	REG_SIOCNT = (u16)(REG_SIOCNT | SIOCNT_START);
	while (REG_SIOCNT & SIOCNT_START)
		;
	return REG_SIODATA32;
}

static inline void sio_init(void)
{
	/* Toggle Joybus mode and back to reset the SIO circuitry, then run as a
	 * 32-bit normal-mode slave with the clock supplied by the host. */
	REG_RCNT = 0xC000u;
	REG_RCNT = 0x0000u;
	REG_SIOCNT = SIOCNT_32BIT;
}

static inline void wr16(u32 addr, u16 value)
{
	*(volatile u16 *)(CART_BASE + addr) = value;
}

static inline u16 rd16(u32 addr)
{
	return *(volatile u16 *)(CART_BASE + addr);
}

static inline u32 rd32(u32 addr)
{
	u16 lo = rd16(addr);
	u16 hi = rd16(addr + 2);
	return (u32)lo | ((u32)hi << 16);
}

static void cmd_write_seq(u32 count)
{
	u32 i;

	for (i = 0; i < count; i++) {
		u32 addr = sio_xfer(ST_ECHO);
		u32 value = sio_xfer(ST_ECHO);
		wr16(addr, (u16)value);
	}
	reply = ST_OK;
}

static void cmd_read(void)
{
	u32 addr = sio_xfer(ST_ECHO);
	u32 count = sio_xfer(ST_ECHO);
	u32 i;

	for (i = 0; i < count; i++)
		(void)sio_xfer(rd32(addr + i * 4u));
	reply = ST_OK;
}

static void poll_ready(u32 addr)
{
	u32 spins = 0;

	/* Intel status register: SR.7 is set when the operation completes. */
	while ((rd16(addr) & 0x80) == 0 && ++spins < 10000000u)
		;
}

static void buffer_write(u32 sector, const u16 *words, u32 count)
{
	u32 i;

	wr16(sector, 0xE8);
	wr16(sector, (u16)(count * 2u - 1u));
	for (i = 0; i < count; i++)
		wr16(sector + i * 2u, words[i]);
	wr16(sector, 0xD0);
	poll_ready(sector);
	wr16(sector, 0xFF);
}

static void cmd_write_block(void)
{
	u32 addr = sio_xfer(ST_ECHO);
	u32 packed = sio_xfer(ST_ECHO);
	u32 i, j;

	if (packed > BLOCK_WORDS || (packed % (CHUNK_WORDS / 2u)) != 0) {
		reply = ST_BAD;
		return;
	}
	for (i = 0; i < packed; i++)
		block_buffer[i] = sio_xfer(ST_ECHO);

	for (i = 0; i < packed; i += CHUNK_WORDS / 2u) {
		u16 chunk[CHUNK_WORDS];

		for (j = 0; j < CHUNK_WORDS / 2u; j++) {
			chunk[j * 2u] = (u16)(block_buffer[i + j] & 0xFFFFu);
			chunk[j * 2u + 1u] = (u16)(block_buffer[i + j] >> 16);
		}
		buffer_write(addr + i * 4u, chunk, CHUNK_WORDS);
	}
	reply = ST_OK;
}

static void cmd_boot(void)
{
	/* A zero soft-reset flag makes the BIOS boot the cartridge after the
	 * reset; a non-zero value jumps to 0x02000000 instead. */
	*(volatile u8 *)0x03007FFA = 0;
	__asm__ volatile("swi #0x000000" ::: "memory");
	for (;;)
		;
}

static void dispatch(u32 hdr)
{
	u32 op;

	if (hdr == PING) {
		reply = ST_OK;
		return;
	}
	if ((hdr >> 24) != HDR_MAGIC) {
		reply = ST_BAD;
		return;
	}

	op = (hdr >> 8) & 0xFFu;
	switch (op) {
	case OP_WRITE_SEQ:
		cmd_write_seq(hdr & 0xFFu);
		break;
	case OP_READ:
		cmd_read();
		break;
	case OP_WRITE_BLOCK:
		cmd_write_block();
		break;
	case OP_BOOT:
		cmd_boot();
		break;
	default:
		reply = ST_BAD;
		break;
	}
}

int main(void)
{
	sio_init();
	reply = ST_OK;

	for (;;)
		dispatch(sio_xfer(reply));
}
