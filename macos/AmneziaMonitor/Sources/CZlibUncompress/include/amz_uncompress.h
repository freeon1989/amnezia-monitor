#ifndef AMZ_UNCOMPRESS_H
#define AMZ_UNCOMPRESS_H

#include <stddef.h>
#include <stdint.h>

/* zlib-обёртка (RFC 1950), как Qt qUncompress / Java Inflater. 0 — успех. */
int amz_zlib_uncompress(const uint8_t *src, size_t src_len,
                        uint8_t *dst, size_t dst_cap, size_t *out_len);

/* Только для тестов кругового пути. 0 — успех. */
int amz_zlib_compress(const uint8_t *src, size_t src_len,
                      uint8_t *dst, size_t dst_cap, size_t *out_len);

#endif
