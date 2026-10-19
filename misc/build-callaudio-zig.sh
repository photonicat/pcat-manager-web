#!/bin/sh
# Cross-build src/callaudio/pcat-call-audio for the photonicat2 (aarch64 musl)
# from a Mac, using zig as the cross compiler.
#
# Why this exists instead of `make CROSS=aarch64-openwrt-linux-`: there is no
# OpenWrt staging dir with alsa-lib/mbedTLS headers on any dev box here, and the
# libraries on the device are sstripped (no section headers), so ld.lld cannot
# link against them. So: headers from upstream tarballs, and stub .so files that
# carry nothing but the right soname and symbol names. The real libraries
# resolve every symbol at load time on the device.
#
# One-time inputs, both under /tmp (recreate per the comments if missing):
#   /tmp/ca-sysroot/include/{alsa,mbedtls,psa}
#       alsa   <- Debian libasound2-dev_1.2.8-1+b1_arm64.deb (bsdtar x data.tar)
#       mbedtls, psa <- mbedtls v2.28.5 tarball include/
#   /tmp/ca-stubs/  generated below from STUB_* lists
#
# Usage:  misc/build-callaudio-zig.sh [output-path]
set -e

SRC=$(cd "$(dirname "$0")/../src/callaudio" && pwd)
OUT=${1:-$SRC/pcat-call-audio}
SYSROOT=/tmp/ca-sysroot
STUBS=/tmp/ca-stubs
TARGET=aarch64-linux-musl

[ -d "$SYSROOT/include/alsa" ] || { echo "missing $SYSROOT/include/alsa"; exit 1; }
[ -d "$SYSROOT/include/mbedtls" ] || { echo "missing $SYSROOT/include/mbedtls"; exit 1; }

# ---- stub libraries -------------------------------------------------------
# Sonames must match the device exactly (it runs mbedTLS 2.28 = libmbedtls12,
# with these .so version suffixes). Which mbed stub a symbol lands in does not
# matter: all three end up in NEEDED and the loader resolves globally.
mkdir -p "$STUBS"
gen() { # gen <file> <symbols...>
    f=$1; shift
    : > "$f"
    for s in "$@"; do echo "void $s(void) {}" >> "$f"; done
}

gen "$STUBS/stub_asound.c" \
    snd_pcm_close snd_pcm_delay snd_pcm_drop snd_pcm_hw_params \
    snd_pcm_hw_params_any snd_pcm_hw_params_get_buffer_size \
    snd_pcm_hw_params_get_period_size snd_pcm_hw_params_set_access \
    snd_pcm_hw_params_set_buffer_size_near snd_pcm_hw_params_set_channels \
    snd_pcm_hw_params_set_format snd_pcm_hw_params_set_period_size_near \
    snd_pcm_hw_params_set_rate_near snd_pcm_hw_params_sizeof snd_pcm_open \
    snd_pcm_prepare snd_pcm_readi snd_pcm_resume snd_pcm_sw_params \
    snd_pcm_sw_params_current snd_pcm_sw_params_set_avail_min \
    snd_pcm_sw_params_set_start_threshold snd_pcm_sw_params_set_stop_threshold \
    snd_pcm_sw_params_sizeof snd_pcm_writei snd_strerror

gen "$STUBS/stub_tls.c" \
    mbedtls_net_accept mbedtls_net_bind mbedtls_net_free mbedtls_net_init \
    mbedtls_net_recv mbedtls_net_recv_timeout mbedtls_net_send \
    mbedtls_ssl_close_notify mbedtls_ssl_config_defaults \
    mbedtls_ssl_config_free mbedtls_ssl_config_init mbedtls_ssl_conf_own_cert \
    mbedtls_ssl_conf_read_timeout mbedtls_ssl_conf_rng mbedtls_ssl_free \
    mbedtls_ssl_handshake mbedtls_ssl_init mbedtls_ssl_read \
    mbedtls_ssl_set_bio mbedtls_ssl_setup mbedtls_ssl_write

gen "$STUBS/stub_x509.c" mbedtls_x509_crt_free mbedtls_x509_crt_init \
    mbedtls_x509_crt_parse_file

gen "$STUBS/stub_crypto.c" \
    mbedtls_ctr_drbg_free mbedtls_ctr_drbg_init mbedtls_ctr_drbg_random \
    mbedtls_ctr_drbg_seed mbedtls_entropy_free mbedtls_entropy_func \
    mbedtls_entropy_init mbedtls_md mbedtls_md_info_from_type \
    mbedtls_md_hmac mbedtls_pk_free mbedtls_pk_init mbedtls_pk_parse_keyfile \
    mbedtls_strerror

zig cc -target $TARGET -shared -Wl,-soname,libasound.so.2 \
    -o "$STUBS/libasound.so" "$STUBS/stub_asound.c"
zig cc -target $TARGET -shared -Wl,-soname,libmbedtls.so.14 \
    -o "$STUBS/libmbedtls.so" "$STUBS/stub_tls.c"
zig cc -target $TARGET -shared -Wl,-soname,libmbedx509.so.1 \
    -o "$STUBS/libmbedx509.so" "$STUBS/stub_x509.c"
zig cc -target $TARGET -shared -Wl,-soname,libmbedcrypto.so.7 \
    -o "$STUBS/libmbedcrypto.so" "$STUBS/stub_crypto.c"

# ---- the daemon ----------------------------------------------------------
# One -c per file: a multi-file `zig cc -c` silently drops objects.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
for f in main audio tls auth; do
    zig cc -target $TARGET -O2 -std=gnu99 -pthread -Wall -Wextra \
        -I "$SYSROOT/include" -c -o "$WORK/$f.o" "$SRC/$f.c"
done
zig cc -target $TARGET -pthread -o "$OUT" \
    "$WORK"/main.o "$WORK"/audio.o "$WORK"/tls.o "$WORK"/auth.o \
    -L "$STUBS" -lasound -lmbedtls -lmbedx509 -lmbedcrypto

echo "built $OUT"
ls -la "$OUT"
