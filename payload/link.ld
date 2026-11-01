ENTRY(_start)

SECTIONS
{
	. = 0x02000000;

	.text : {
		*(.text.entry)
		*(.text*)
		*(.rodata*)
	}

	.data : {
		*(.data*)
	}

	.bss (NOLOAD) : {
		__bss_start = .;
		*(.bss*)
		*(COMMON)
		__bss_end = .;
	}

	/DISCARD/ : {
		*(.ARM.exidx*)
		*(.comment)
		*(.note*)
	}
}
