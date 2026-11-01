DEVKITARM ?= /opt/devkitpro/devkitARM

# Prefer devkitARM, but fall back to an arm-none-eabi- toolchain on PATH.
ifneq ($(wildcard $(DEVKITARM)/bin/arm-none-eabi-gcc),)
PREFIX ?= $(DEVKITARM)/bin/arm-none-eabi-
else
PREFIX ?= arm-none-eabi-
endif

PYTHON ?= python3

CC := $(PREFIX)gcc
OBJCOPY := $(PREFIX)objcopy

CFLAGS := -mcpu=arm7tdmi -marm -O2 -Wall -Wextra -ffreestanding -fno-builtin \
	-fno-common -nostdlib
LDFLAGS := -T link.ld -nostdlib -Wl,--gc-sections -Wl,-Map=flasher.map

all: flasher.gba

check:
	@command -v $(CC) >/dev/null || { \
		echo "$(CC) not found: install arm-none-eabi-gcc or set DEVKITARM" >&2; \
		exit 1; \
	}

flasher.elf: crt0.S payload.c link.ld
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ crt0.S payload.c

flasher.gba: flasher.elf fix_header.py | check
	$(OBJCOPY) -O binary $< $@
	$(PYTHON) fix_header.py $@ -t GBAFLASH -c GBAF -m 00 -r 00

clean:
	rm -f flasher.elf flasher.gba flasher.map

.PHONY: all check clean
