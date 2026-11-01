	.syntax unified
	.cpu arm7tdmi
	.arm

	.section .text.entry, "ax", %progbits
	.global _header
	.type _header, %function

@ GBA cartridge header as required by the BIOS multiboot loader.
@ The logo and header checksum are filled in by gbafix.
_header:
	b	_start			@ 0x00 cartridge entry
	.space	156, 0			@ 0x04 Nintendo logo
	.space	12, 0			@ 0xA0 title
	.space	4, 0			@ 0xAC game code
	.space	2, 0			@ 0xB0 maker code
	.byte	0x96			@ 0xB2 fixed value
	.byte	0x00			@ 0xB3 device code
	.byte	0x00			@ 0xB4 device type
	.space	7, 0			@ 0xB5 reserved
	.byte	0x00			@ 0xBC version
	.byte	0x00			@ 0xBD checksum (gbafix)
	.space	2, 0			@ 0xBE reserved
	b	_start			@ 0xC0 multiboot entry
	.space	28, 0			@ 0xC4
	b	_start			@ 0xE0 joybus entry
	.space	4, 0			@ 0xE4

	.global _start
	.type _start, %function
_start:
	@ Interrupts off.
	mov	r0, #0x04000000
	mov	r1, #0
	add	r0, r0, #0x200		@ REG_IE / REG_IME block
	strh	r1, [r0, #0x08]		@ REG_IME
	strh	r1, [r0, #0x00]		@ REG_IE

	@ Wait states, no Game Pak prefetch (stale reads after flash writes).
	ldr	r1, =0x0317
	strh	r1, [r0, #0x04]		@ REG_WAITCNT

	@ Supervisor stack inside IWRAM, well clear of the BIOS work area.
	msr	cpsr_c, #0xD3
	ldr	sp, =0x03007E00

	@ Zero .bss.
	ldr	r0, =__bss_start
	ldr	r1, =__bss_end
	mov	r2, #0
1:	cmp	r0, r1
	strlo	r2, [r0], #4
	blo	1b

	bl	main

2:	b	2b

	.pool
