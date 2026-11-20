#include "amz_uncompress.h"

#include <string.h>
#include <zlib.h>

int amz_zlib_uncompress(const uint8_t *src, size_t src_len,
                        uint8_t *dst, size_t dst_cap, size_t *out_len) {
    z_stream strm;
    int rc;
    if (src == NULL || dst == NULL || out_len == NULL || dst_cap == 0) {
        return -1;
    }
    memset(&strm, 0, sizeof(strm));
    if (inflateInit(&strm) != Z_OK) {
        return -1;
    }
    strm.next_in = (Bytef *)src;
    strm.avail_in = (uInt)src_len;
    strm.next_out = dst;
    strm.avail_out = (uInt)dst_cap;
    rc = inflate(&strm, Z_FINISH);
    *out_len = (size_t)strm.total_out;
    inflateEnd(&strm);
    if (rc != Z_STREAM_END || *out_len == 0) {
        return -1;
    }
    return 0;
}

int amz_zlib_compress(const uint8_t *src, size_t src_len,
                      uint8_t *dst, size_t dst_cap, size_t *out_len) {
    uLongf dest = (uLongf)dst_cap;
    int rc;
    if (src == NULL || dst == NULL || out_len == NULL) {
        return -1;
    }
    rc = compress2(dst, &dest, src, (uLong)src_len, 8);
    if (rc != Z_OK) {
        return -1;
    }
    *out_len = (size_t)dest;
    return 0;
}
