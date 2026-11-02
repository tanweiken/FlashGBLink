/*
 * GBA cart bus helper, loaded into EWRAM through the BIOS multiboot loader.
 *
 * Transport: GBA SIO normal mode, 32-bit, external clock (slave). Each word the
 * host clocks in is exchanged for a word this program preloaded. The host is
 * gbasend-pico behaving as a plain SPI bridge; it paces one word at a time, so
 * a blocking exchange is safe.
 *
 * This program knows nothing about flash chips. It performs 16-bit cartridge
 * stores and loads, byte-wide save stores and loads, a progress bar, and a soft
 * reset. The host composes the chip command sequences (for example from a
 * FlashGBX profile).
 *
 * Addresses below 0x08000000 are relative to the cartridge base. Anything else
 * is used verbatim, which is how the host reaches save memory at 0x0E000000
 * with the same commands.
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
#define OP_PROGRESS 0x60u
#define OP_READ8 0x70u
#define OP_WRITE8 0x80u
#define OP_SET_PLAN 0x90u
#define OP_DONE 0xA0u

#define BLOCK_WORDS 1024u /* packed 32-bit words, 4 KiB of cartridge data */
#define PLAN_MAX_COMMANDS 12u

#define ADDR_ABS 0u
#define ADDR_SA 1u
#define ADDR_PA 2u

#define VAL_CONST 0u
#define VAL_BS 1u
#define VAL_PD 2u

#define WAIT_NONE 0u
#define WAIT_ABS 1u
#define WAIT_SA 2u
#define WAIT_PD 3u

#define PING 0x5A5A5A5Au

#define ST_OK 0xA5A50000u
#define ST_ECHO 0xA5A5E000u
#define ST_BAD 0xA5A500E1u

struct write_cmd {
	u32 addr_kind;
	u32 addr;
	u32 val_kind;
	u32 val;
	u32 wait_kind;
	u32 wait_addr;
	u32 wait_exp;
	u32 wait_mask;
};

static u32 reply = ST_OK;
static u32 block_buffer[BLOCK_WORDS];
static struct write_cmd plan[PLAN_MAX_COMMANDS];
static u32 plan_count;
static u32 plan_chunk = 64u;

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

static inline u32 bus_addr(u32 addr)
{
	return addr >= CART_BASE ? addr : CART_BASE + addr;
}

static inline void wr16(u32 addr, u16 value)
{
	*(volatile u16 *)bus_addr(addr) = value;
}

static inline u16 rd16(u32 addr)
{
	return *(volatile u16 *)bus_addr(addr);
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

static int plan_wait(u32 base, const struct write_cmd *cmd, u16 last)
{
	u32 addr;
	u32 spins = 0;
	u16 expected;

	if (cmd->wait_kind == WAIT_NONE)
		return 1;
	addr = cmd->wait_kind == WAIT_ABS ? cmd->wait_addr
					  : base + cmd->wait_addr;
	expected = cmd->wait_kind == WAIT_PD ? last : (u16)cmd->wait_exp;
	while (++spins < 20000000u) {
		u16 value = rd16(addr);

		if ((value & (u16)cmd->wait_mask) == expected)
			return 1;
	}
	return 0;
}

static void plan_execute(u32 base, const u16 *words, u32 count)
{
	u32 i;
	u32 last = 0;

	for (i = 0; i < plan_count; i++) {
		const struct write_cmd *cmd = &plan[i];
		u32 addr = cmd->addr_kind == ADDR_ABS ? cmd->addr
						      : base + cmd->addr;

		if (cmd->val_kind == VAL_PD) {
			u32 j;

			for (j = 0; j < count; j++) {
				wr16(addr + j * 2u, words[j]);
				last = words[j];
			}
		} else {
			u16 value = cmd->val_kind == VAL_BS
				  ? (u16)(plan_chunk - 1u) : (u16)cmd->val;

			wr16(addr, value);
		}
		if (!plan_wait(base, cmd, (u16)last)) {
			reply = ST_BAD;
			return;
		}
	}
}

static void set_default_plan(void)
{
	struct write_cmd *cmd = plan;

	plan_count = 5u;
	plan_chunk = 64u;

	cmd[0].addr_kind = ADDR_SA;
	cmd[0].addr = 0;
	cmd[0].val_kind = VAL_CONST;
	cmd[0].val = 0xE8;
	cmd[0].wait_kind = WAIT_NONE;

	cmd[1].addr_kind = ADDR_SA;
	cmd[1].addr = 0;
	cmd[1].val_kind = VAL_BS;
	cmd[1].val = 0;
	cmd[1].wait_kind = WAIT_NONE;

	cmd[2].addr_kind = ADDR_PA;
	cmd[2].addr = 0;
	cmd[2].val_kind = VAL_PD;
	cmd[2].val = 0;
	cmd[2].wait_kind = WAIT_NONE;

	cmd[3].addr_kind = ADDR_SA;
	cmd[3].addr = 0;
	cmd[3].val_kind = VAL_CONST;
	cmd[3].val = 0xD0;
	cmd[3].wait_kind = WAIT_SA;
	cmd[3].wait_addr = 0;
	cmd[3].wait_exp = 0x80;
	cmd[3].wait_mask = 0xFFFF;

	cmd[4].addr_kind = ADDR_SA;
	cmd[4].addr = 0;
	cmd[4].val_kind = VAL_CONST;
	cmd[4].val = 0xFF;
	cmd[4].wait_kind = WAIT_NONE;
}

static void cmd_set_plan(void)
{
	u32 chunk = sio_xfer(ST_ECHO);
	u32 count = sio_xfer(ST_ECHO);
	u32 i;
	int bad = 0;

	if (count > PLAN_MAX_COMMANDS || chunk == 0u || (chunk & 1u) ||
	    chunk > BLOCK_WORDS * 4u)
		bad = 1;
	for (i = 0; i < count && i < PLAN_MAX_COMMANDS; i++) {
		u32 w0 = sio_xfer(ST_ECHO);
		u32 w1 = sio_xfer(ST_ECHO);
		u32 w2 = sio_xfer(ST_ECHO);
		u32 w3 = sio_xfer(ST_ECHO);

		plan[i].addr_kind = w0 >> 30;
		plan[i].addr = w0 & 0x3FFFFFFFu;
		plan[i].val_kind = w1 >> 30;
		plan[i].val = w1 & 0xFFFFu;
		plan[i].wait_kind = w2 >> 30;
		plan[i].wait_addr = w2 & 0x3FFFFFFFu;
		plan[i].wait_exp = w3 >> 16;
		plan[i].wait_mask = w3 & 0xFFFFu;
		if (plan[i].addr_kind > ADDR_PA || plan[i].val_kind > VAL_PD ||
		    plan[i].wait_kind > WAIT_PD)
			bad = 1;
	}
	if (count > PLAN_MAX_COMMANDS)
		bad = 1;
	if (bad) {
		reply = ST_BAD;
		return;
	}
	plan_count = count;
	plan_chunk = chunk;
	reply = ST_OK;
}

static inline u16 data_word(const u32 *words, u32 index)
{
	u32 packed = words[index >> 1];

	return (u16)((index & 1u) ? (packed >> 16) : (packed & 0xFFFFu));
}

static void cmd_write_block(void)
{
	u32 addr = sio_xfer(ST_ECHO);
	u32 packed = sio_xfer(ST_ECHO);
	u32 chunk_words;
	u32 i, j;

	reply = ST_OK;
	if (packed > BLOCK_WORDS || plan_count == 0u ||
	    packed * 4u < plan_chunk) {
		reply = ST_BAD;
		return;
	}
	for (i = 0; i < packed; i++)
		block_buffer[i] = sio_xfer(ST_ECHO);

	chunk_words = plan_chunk / 2u;
	for (i = 0; i < packed * 4u; i += plan_chunk) {
		u16 chunk[BLOCK_WORDS * 2u];

		for (j = 0; j < chunk_words; j++)
			chunk[j] = data_word(block_buffer, (i / 2u) + j);
		plan_execute(addr + i, chunk, chunk_words);
		if (reply != ST_OK)
			return;
	}
	reply = ST_OK;
}

static void cmd_read8(void)
{
	/* Save media is 8 bits wide: a 16-bit load would return the same byte in
	 * both halves of the lane, so each byte is read individually and packed
	 * four to a word. */
	u32 addr = sio_xfer(ST_ECHO);
	u32 count = sio_xfer(ST_ECHO);
	u32 i, j;

	if (count > BLOCK_WORDS * 4u) {
		reply = ST_BAD;
		return;
	}
	for (i = 0; i < count; i += 4u) {
		u32 word = 0;

		for (j = 0; j < 4u && i + j < count; j++)
			word |= (u32)(*(volatile u8 *)bus_addr(addr + i + j))
				<< (j * 8u);
		(void)sio_xfer(word);
	}
	reply = ST_OK;
}

static void cmd_write8(void)
{
	u32 addr = sio_xfer(ST_ECHO);
	u32 count = sio_xfer(ST_ECHO);
	u32 words = (count + 3u) / 4u;
	u32 i;

	if (words > BLOCK_WORDS) {
		reply = ST_BAD;
		return;
	}
	for (i = 0; i < words; i++)
		block_buffer[i] = sio_xfer(ST_ECHO);
	for (i = 0; i < count; i++)
		*(volatile u8 *)bus_addr(addr + i) =
			(u8)(block_buffer[i / 4u] >> ((i % 4u) * 8u));
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

/* -- screen ---------------------------------------------------------------- */

#define REG_DISPCNT (*(volatile u16 *)0x04000000)
#define VRAM ((volatile u16 *)0x06000000)

#define SCREEN_W 240u
#define SCREEN_H 160u

#define COL_BG 0x0000u
#define COL_TRACK 0x18C6u
#define COL_DIM 0x39E7u
#define COL_WRITE 0x03E0u
#define COL_VERIFY 0x7FE0u
#define COL_TEXT 0x7FFFu

#define BAR_X 20u
#define BAR_W 200u
#define BAR_H 18u
#define BAR_PAD 2u
#define BAR_WRITE_Y 54u
#define BAR_VERIFY_Y 97u
#define LABEL_WRITE_Y 43u
#define LABEL_VERIFY_Y 86u
#define LABEL_H 10u

static u32 write_percent = 0;
static u32 verify_percent = 0;
static u32 verify_started = 0;

static void fill(u32 x, u32 y, u32 w, u32 h, u16 colour)
{
	u32 row, col;

	for (row = 0; row < h; row++)
		for (col = 0; col < w; col++)
			VRAM[(y + row) * SCREEN_W + x + col] = colour;
}

/* A small 5x7 font: space, %, -, digits, A-Z. */
static const char font_chars[] = " %-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
static const u8 font_glyphs[][5] = {
	{ 0x00, 0x00, 0x00, 0x00, 0x00 }, /* space */
	{ 0x23, 0x13, 0x08, 0x64, 0x62 }, /* % */
	{ 0x08, 0x08, 0x08, 0x08, 0x08 }, /* - */
	{ 0x3E, 0x51, 0x49, 0x45, 0x3E }, /* 0 */
	{ 0x00, 0x42, 0x7F, 0x40, 0x00 }, /* 1 */
	{ 0x42, 0x61, 0x51, 0x49, 0x46 }, /* 2 */
	{ 0x21, 0x41, 0x45, 0x4B, 0x31 }, /* 3 */
	{ 0x18, 0x14, 0x12, 0x7F, 0x10 }, /* 4 */
	{ 0x27, 0x45, 0x45, 0x45, 0x39 }, /* 5 */
	{ 0x3C, 0x4A, 0x49, 0x49, 0x30 }, /* 6 */
	{ 0x01, 0x71, 0x09, 0x05, 0x03 }, /* 7 */
	{ 0x36, 0x49, 0x49, 0x49, 0x36 }, /* 8 */
	{ 0x06, 0x49, 0x49, 0x29, 0x1E }, /* 9 */
	{ 0x7E, 0x11, 0x11, 0x11, 0x7E }, /* A */
	{ 0x7F, 0x49, 0x49, 0x49, 0x36 }, /* B */
	{ 0x3E, 0x41, 0x41, 0x41, 0x22 }, /* C */
	{ 0x7F, 0x41, 0x41, 0x22, 0x1C }, /* D */
	{ 0x7F, 0x49, 0x49, 0x49, 0x41 }, /* E */
	{ 0x7F, 0x09, 0x09, 0x09, 0x01 }, /* F */
	{ 0x3E, 0x41, 0x49, 0x49, 0x7A }, /* G */
	{ 0x7F, 0x08, 0x08, 0x08, 0x7F }, /* H */
	{ 0x00, 0x41, 0x7F, 0x41, 0x00 }, /* I */
	{ 0x20, 0x40, 0x41, 0x3F, 0x01 }, /* J */
	{ 0x7F, 0x08, 0x14, 0x22, 0x41 }, /* K */
	{ 0x7F, 0x40, 0x40, 0x40, 0x40 }, /* L */
	{ 0x7F, 0x02, 0x0C, 0x02, 0x7F }, /* M */
	{ 0x7F, 0x04, 0x08, 0x10, 0x7F }, /* N */
	{ 0x3E, 0x41, 0x41, 0x41, 0x3E }, /* O */
	{ 0x7F, 0x09, 0x09, 0x09, 0x06 }, /* P */
	{ 0x3E, 0x41, 0x51, 0x21, 0x5E }, /* Q */
	{ 0x7F, 0x09, 0x19, 0x29, 0x46 }, /* R */
	{ 0x46, 0x49, 0x49, 0x49, 0x31 }, /* S */
	{ 0x01, 0x01, 0x7F, 0x01, 0x01 }, /* T */
	{ 0x3F, 0x40, 0x40, 0x40, 0x3F }, /* U */
	{ 0x1F, 0x20, 0x40, 0x20, 0x1F }, /* V */
	{ 0x3F, 0x40, 0x38, 0x40, 0x3F }, /* W */
	{ 0x63, 0x14, 0x08, 0x14, 0x63 }, /* X */
	{ 0x07, 0x08, 0x70, 0x08, 0x07 }, /* Y */
	{ 0x61, 0x51, 0x49, 0x45, 0x43 }  /* Z */
};

static u32 font_index(char c)
{
	u32 i;

	for (i = 0; font_chars[i] != '\0'; i++)
		if (font_chars[i] == c)
			return i;
	return 0;
}

static u32 text_width(const char *text, u32 scale)
{
	u32 length = 0;

	while (text[length] != '\0')
		length++;
	return length * 6u * scale;
}

static void draw_char(u32 x, u32 y, char c, u32 scale, u16 colour)
{
	const u8 *glyph = font_glyphs[font_index(c)];
	u32 col, row;

	for (col = 0; col < 5u; col++)
		for (row = 0; row < 7u; row++)
			if ((glyph[col] >> row) & 1u)
				fill(x + col * scale, y + row * scale,
				     scale, scale, colour);
}

static void draw_text(u32 x, u32 y, const char *text, u32 scale, u16 colour)
{
	u32 i;

	for (i = 0; text[i] != '\0'; i++)
		draw_char(x + i * 6u * scale, y, text[i], scale, colour);
}

static void draw_text_centered(u32 y, const char *text, u32 scale, u16 colour)
{
	u32 width = text_width(text, scale);

	draw_text((SCREEN_W - width) / 2u, y, text, scale, colour);
}

static void draw_row(u32 y, const char *name, u32 percent, u16 colour,
		     u32 active)
{
	char pct[5];
	u32 n = 0;
	u32 i;

	if (active) {
		if (percent >= 100u) {
			pct[n++] = '1';
			pct[n++] = '0';
			pct[n++] = '0';
		} else {
			if (percent >= 10u)
				pct[n++] = (char)('0' + percent / 10u);
			pct[n++] = (char)('0' + percent % 10u);
		}
		pct[n++] = '%';
	} else {
		pct[n++] = '-';
		pct[n++] = '-';
	}
	pct[n] = '\0';

	fill(BAR_X, y, BAR_W, LABEL_H, COL_BG);
	/* Phase name is deliberately smaller than the percentage. */
	draw_text(BAR_X, y, name, 1u, active ? colour : COL_DIM);
	draw_text(BAR_X + BAR_W - text_width(pct, 1u), y, pct, 1u,
		  active ? colour : COL_DIM);
	(void)i;
}

static void draw_bar(u32 y, u16 colour, u32 percent, u32 active)
{
	u16 frame = active ? colour : COL_DIM;

	fill(BAR_X, y, BAR_W, BAR_H, COL_BG);
	fill(BAR_X, y, BAR_W, BAR_H, frame);
	fill(BAR_X + 1u, y + 1u, BAR_W - 2u, BAR_H - 2u, COL_TRACK);
	if (active && percent > 0u) {
		fill(BAR_X + 2u, y + 2u,
		     (BAR_W - 4u) * percent / 100u, BAR_H - 4u, colour);
	}
}

static void screen_init(void)
{
	REG_DISPCNT = 0x0403u; /* mode 3, BG2 enabled */
	fill(0, 0, SCREEN_W, SCREEN_H, COL_BG);
	draw_text_centered(26u, "FLASHGBLINK", 1u, COL_TEXT);
	draw_row(LABEL_WRITE_Y, "WRITE", 0u, COL_WRITE, 0u);
	draw_bar(BAR_WRITE_Y, COL_WRITE, 0u, 0u);
	draw_row(LABEL_VERIFY_Y, "VERIFY", 0u, COL_VERIFY, 0u);
	draw_bar(BAR_VERIFY_Y, COL_VERIFY, 0u, 0u);
	draw_text_centered(127u, "READY", 1u, COL_TEXT);
}

static void show_progress(u32 phase, u32 percent)
{
	if (percent > 100u)
		percent = 100u;
	if (phase == 0u) {
		write_percent = percent;
		draw_row(LABEL_WRITE_Y,
			 percent >= 100u ? "WRITE DONE" : "WRITING",
			 percent, COL_WRITE, 1u);
		draw_bar(BAR_WRITE_Y, COL_WRITE, percent, 1u);
	} else {
		verify_started = 1u;
		verify_percent = percent;
		draw_row(LABEL_WRITE_Y, "WRITE DONE", write_percent,
			 COL_WRITE, 1u);
		draw_bar(BAR_WRITE_Y, COL_WRITE, write_percent, 1u);
		draw_row(LABEL_VERIFY_Y, "VERIFYING", percent, COL_VERIFY, 1u);
		draw_bar(BAR_VERIFY_Y, COL_VERIFY, percent, 1u);
	}
	fill(0, 120u, SCREEN_W, 40u, COL_BG);
	draw_text_centered(127u, "DO NOT POWER OFF", 1u, COL_TEXT);
}

static void draw_check(u32 x, u32 y, u16 colour)
{
	static const u8 cx[] = { 0, 3, 6, 9, 12, 15, 18 };
	static const u8 cy[] = { 6, 9, 12, 9, 6, 3, 0 };
	u32 i;

	for (i = 0; i < 7u; i++)
		fill(x + cx[i], y + cy[i], 3u, 3u, colour);
}

static void show_done(void)
{
	fill(0, 0, SCREEN_W, SCREEN_H, COL_BG);
	draw_text_centered(41u, "FLASHGBLINK", 1u, COL_TEXT);
	draw_text_centered(60u, "FLASH COMPLETE", 2u, COL_WRITE);
	draw_check(109u, 84u, COL_WRITE);
	draw_text_centered(112u, "RESTART GBA TO PLAY", 1u, COL_TEXT);
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
	case OP_SET_PLAN:
		cmd_set_plan();
		break;
	case OP_PROGRESS:
		show_progress((hdr >> 16) & 0xFFu, hdr & 0xFFu);
		reply = ST_OK;
		break;
	case OP_DONE:
		show_done();
		reply = ST_OK;
		break;
	case OP_READ8:
		cmd_read8();
		break;
	case OP_WRITE8:
		cmd_write8();
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
	screen_init();
	set_default_plan();
	reply = ST_OK;

	for (;;)
		dispatch(sio_xfer(reply));
}
