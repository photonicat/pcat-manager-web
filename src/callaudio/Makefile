# pcat-call-audio -- build for the OpenWrt aarch64 target.
#
# Cross-compiling (the usual case; see the notes in CLAUDE.md about the
# aarch64 toolchain host):
#
#   make CROSS=aarch64-openwrt-linux- SYSROOT=/path/to/staging_dir/target-...
#
# The OpenWrt package (pcat-addons, net/pcat-manager-web) instead passes the
# buildroot's own toolchain through directly:
#
#   make CC="$(TARGET_CC)" CFLAGS="$(TARGET_CFLAGS) $(TARGET_CPPFLAGS)" \
#        LDFLAGS="$(TARGET_LDFLAGS)"
#
# Native build on the device, or on any Linux box with the -dev packages, is
# just `make`. The daemon links libasound (already present, alsa-utils pulls
# it) and mbedTLS (present, OpenWrt's default TLS stack).

CROSS   ?=
CC      := $(CROSS)gcc
SYSROOT ?=

# The flags this code actually needs, kept separate from CFLAGS/LDFLAGS. A
# command-line assignment (`make CFLAGS=...`, which is how the OpenWrt package
# passes TARGET_CFLAGS) overrides a `CFLAGS +=` in the makefile entirely, so
# appending there would silently drop -pthread and -std=gnu99 in exactly the
# cross-build that matters. These are appended after $(CFLAGS) instead.
CFLAGS  ?= -O2
ALL_CFLAGS  := $(CFLAGS) -Wall -Wextra -std=gnu99 -pthread
ALL_LDFLAGS := $(LDFLAGS) -pthread
LIBS    := -lasound -lmbedtls -lmbedx509 -lmbedcrypto

ifneq ($(SYSROOT),)
ALL_CFLAGS  += --sysroot=$(SYSROOT) -I$(SYSROOT)/usr/include
ALL_LDFLAGS += --sysroot=$(SYSROOT) -L$(SYSROOT)/usr/lib
endif

BIN  := pcat-call-audio
SRCS := main.c audio.c tls.c auth.c
OBJS := $(SRCS:.c=.o)

all: $(BIN)

$(BIN): $(OBJS)
	$(CC) $(ALL_LDFLAGS) -o $@ $(OBJS) $(LIBS)

%.o: %.c
	$(CC) $(ALL_CFLAGS) -c -o $@ $<

# Static analysis / syntax only -- useful on a dev box without the libs.
check:
	$(CC) -fsyntax-only $(ALL_CFLAGS) $(SRCS)

clean:
	rm -f $(OBJS) $(BIN)

.PHONY: all check clean
