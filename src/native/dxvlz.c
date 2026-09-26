/* dxvlz.c —— DXV3 的「中间压缩」解压：把帧负载还原成 BC(DXT) 压缩块流。
 *
 * 为什么要它：DXV 的帧里不是裸的 BC 块，FFmpeg 又在外面套了一层自己的 LZ 压缩。
 * 只有先把这层解掉，才能拿到 BC 块并原样上传成显卡纹理，让 GPU 硬件解压
 * （这就是 Resolume Arena 的做法）。
 *
 * 格式依据：FFmpeg `libavcodec/dxv.c` 所公开的 DXV3 算法（LGPL-2.1+）重写。
 * 本文件独立编译成 DLL、以动态链接方式使用，不链接也不修改 FFmpeg 本体。
 *
 * 编译（zig 自带 C 编译器，无需 VS）：
 *   zig cc -O2 -shared -o src/_dxvlz.dll src/native/dxvlz.c
 *
 * 导出：
 *   int dxv_header(const uint8_t* src, int len,
 *                  int* fmt, int* raw, int* off, int* plen);
 *       → 解析 12 字节帧头。fmt: 1=DXT1, 5=DXT5；raw: 0/1。
 *         返回 0 成功；3=格式不支持(YCG6/YG10)；4=旧式帧头；1=数据不足。
 *
 *   int dxv_unpack(const uint8_t* src, int src_len,
 *                  uint8_t* dst, int dst_len, int fmt, int raw);
 *       → 把负载还原成 BC 块流（dst_len 必须 = (w/4)*(h/4)*(fmt==1?8:16)）。
 *         返回 0 成功，非 0 表示负载非法（调用方应回退到 FFmpeg 软解）。
 */

#include <stdint.h>
#include <string.h>

#define TAG4(a, b, c, d) \
    (((uint32_t)(a) << 24) | ((uint32_t)(b) << 16) | ((uint32_t)(c) << 8) | (uint32_t)(d))

#define TAG_DXT1 TAG4('D', 'X', 'T', '1')   /* 磁盘上按 LE32 读 = '1','T','X','D' */
#define TAG_DXT5 TAG4('D', 'X', 'T', '5')
#define TAG_YCG6 TAG4('Y', 'C', 'G', '6')
#define TAG_YG10 TAG4('Y', 'G', '1', '0')

#define FMT_DXT1 1
#define FMT_DXT5 5

typedef struct {
    const uint8_t *p;
    const uint8_t *end;
} BS;

static inline int bs_left(const BS *b) { return (int)(b->end - b->p); }

/* 取 n 字节到 out（in 可为 NULL 表示丢弃）；数据不足返回 0 */
static inline int bs_take(BS *b, uint8_t *out, int n)
{
    if (n < 0 || bs_left(b) < n) {
        b->p = b->end;
        return 0;
    }
    if (out) memcpy(out, b->p, (size_t)n);
    b->p += n;
    return 1;
}

static inline uint8_t bs_u8(BS *b)
{
    if (bs_left(b) < 1) return 0;
    return *b->p++;
}

static inline uint32_t bs_le16(BS *b)
{
    if (bs_left(b) < 2) {
        b->p = b->end;
        return 0;
    }
    uint32_t v = (uint32_t)b->p[0] | ((uint32_t)b->p[1] << 8);
    b->p += 2;
    return v;
}

static inline uint32_t bs_le32(BS *b)
{
    if (bs_left(b) < 4) {
        b->p = b->end;
        return 0;
    }
    uint32_t v = (uint32_t)b->p[0] | ((uint32_t)b->p[1] << 8) |
                 ((uint32_t)b->p[2] << 16) | ((uint32_t)b->p[3] << 24);
    b->p += 4;
    return v;
}

/* FFmpeg 的 CHECKPOINT(x) 宏：从 2-bit 操作码流取出下一个操作码 op 和回退距离 idx。
 * x = 每个「元素」的 dword 数（DXT1 是 2，DXT5 是 4）。
 * 返回非 0 表示数据非法。 */
static inline int ckpt(BS *gb, uint32_t *value, int *state, int *op, int *idx,
                       int x, int pos)
{
    if (*state == 0) {
        if (bs_left(gb) < 4) return 1;
        *value = bs_le32(gb);
        *state = 16;
    }
    *op = (int)(*value & 3u);
    *value >>= 2;
    (*state)--;
    switch (*op) {
    case 1:
        *idx = x;
        break;
    case 2: {
        int b = (int)bs_u8(gb);
        *idx = (b + 2) * x;
        if (*idx > pos) return 1;
        break;
    }
    case 3: {
        uint32_t v = bs_le16(gb);
        *idx = (int)(v + 0x102u) * x;
        if (*idx > pos) return 1;
        break;
    }
    default:
        break;   /* op == 0：idx 保持上一次的值，但调用方不会用它 */
    }
    return 0;
}

/* ---------------------------------------------------------------- DXT1 */
static int dxv_dxt1(BS *gb, uint8_t *d, int dlen)
{
    uint32_t value = 0;
    int op = 0, idx = 0, state = 0;
    int pos = 2;
    const int n_dw = dlen / 4;

    if (dlen < 8 || (dlen & 3)) return 2;
    if (!bs_take(gb, d, 8)) return 2;          /* 前两个 dword 原样拷贝 */

    while (pos + 2 <= n_dw) {
        if (ckpt(gb, &value, &state, &op, &idx, 2, pos)) return 2;
        if (op) {
            memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
            memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
        } else {
            if (ckpt(gb, &value, &state, &op, &idx, 2, pos)) return 2;
            if (op) {
                memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
            } else {
                if (!bs_take(gb, d + 4 * pos, 4)) return 2;
                pos++;
            }
            if (ckpt(gb, &value, &state, &op, &idx, 2, pos)) return 2;
            if (op) {
                memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
            } else {
                if (!bs_take(gb, d + 4 * pos, 4)) return 2;
                pos++;
            }
        }
    }
    return 0;
}

/* ---------------------------------------------------------------- DXT5 */
static int dxv_dxt5(BS *gb, uint8_t *d, int dlen)
{
    uint32_t value = 0;
    int op = 0, idx = 0, state = 0;
    int pos = 4, run = 0;
    const int n_dw = dlen / 4;

    if (dlen < 16 || (dlen & 3)) return 2;
    if (!bs_take(gb, d, 16)) return 2;         /* 前四个 dword 原样拷贝 */

    while (pos + 2 <= n_dw) {
        if (run) {
            run--;
            memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
            memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
        } else {
            if (bs_left(gb) < 1) return 2;
            if (state == 0) {
                value = bs_le32(gb);
                state = 16;
            }
            op = (int)(value & 3u);
            value >>= 2;
            state--;
            switch (op) {
            case 0: {   /* 长拷贝：按块数复制前面 4 个 dword 一组 */
                int check = (int)bs_u8(gb) + 1;
                if (check == 256) {
                    uint32_t probe;
                    do {
                        probe = bs_le16(gb);
                        check += (int)probe;
                    } while (probe == 0xFFFF);
                }
                while (check && pos + 4 <= n_dw) {
                    memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
                    memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
                    memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
                    memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
                    check--;
                }
                continue;
            }
            case 1: {   /* 读取一段 run，再复制两个 dword */
                run = (int)bs_u8(gb);
                if (run == 255) {
                    uint32_t probe;
                    do {
                        probe = bs_le16(gb);
                        run += (int)probe;
                    } while (probe == 0xFFFF);
                }
                memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
                memcpy(d + 4 * pos, d + 4 * (pos - 4), 4); pos++;
                break;
            }
            case 2: {   /* 从更早的位置复制两个 dword */
                idx = 8 + 4 * (int)bs_le16(gb);
                if (idx > pos || (pos - idx) + 2 > n_dw) return 2;
                memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
                memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
                break;
            }
            default: {  /* case 3：直接从输入读两个 dword */
                if (!bs_take(gb, d + 4 * pos, 4)) return 2;
                pos++;
                if (!bs_take(gb, d + 4 * pos, 4)) return 2;
                pos++;
                break;
            }
            }
        }

        if (ckpt(gb, &value, &state, &op, &idx, 4, pos)) return 2;
        if (pos + 2 > n_dw) return 2;

        if (op) {
            if (idx > pos || (pos - idx) + 2 > n_dw) return 2;
            memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
            memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
        } else {
            if (ckpt(gb, &value, &state, &op, &idx, 4, pos)) return 2;
            if (op) {
                if (idx > pos || (pos - idx) + 2 > n_dw) return 2;
                memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
            } else {
                if (!bs_take(gb, d + 4 * pos, 4)) return 2;
                pos++;
            }
            if (ckpt(gb, &value, &state, &op, &idx, 4, pos)) return 2;
            if (op) {
                if (idx > pos || (pos - idx) + 2 > n_dw) return 2;
                memcpy(d + 4 * pos, d + 4 * (pos - idx), 4); pos++;
            } else {
                if (!bs_take(gb, d + 4 * pos, 4)) return 2;
                pos++;
            }
        }
    }
    return 0;
}

/* ---------------------------------------------------------------- 导出 API */
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif

/* 解析帧头。成功返回 0；3=格式不支持；4=旧式帧头；1=数据不足 */
API int dxv_header(const uint8_t *src, int len, int *fmt, int *raw, int *off, int *plen)
{
    if (!src || len < 12) return 1;
    uint32_t tag = (uint32_t)src[0] | ((uint32_t)src[1] << 8) |
                   ((uint32_t)src[2] << 16) | ((uint32_t)src[3] << 24);
    int f;
    if (tag == TAG_DXT1) f = FMT_DXT1;
    else if (tag == TAG_DXT5) f = FMT_DXT5;
    else if (tag == TAG_YCG6 || tag == TAG_YG10) return 3;   /* 暂不支持（库内只有 1 个 YCG6） */
    else return 4;                                           /* 旧式帧头，交给 FFmpeg */

    int r = src[6] ? 1 : 0;
    int size = (int)((uint32_t)src[8] | ((uint32_t)src[9] << 8) |
                     ((uint32_t)src[10] << 16) | ((uint32_t)src[11] << 24));
    if (size <= 0 || size > len - 12) return 1;
    if (fmt)  *fmt = f;
    if (raw)  *raw = r;
    if (off)  *off = 12;
    if (plen) *plen = size;
    return 0;
}

/* 解出 BC 块流。fmt: 1=DXT1, 5=DXT5 */
API int dxv_unpack(const uint8_t *src, int src_len, uint8_t *dst, int dst_len,
                   int fmt, int raw)
{
    if (!src || !dst || src_len <= 0 || dst_len <= 0) return 2;
    if (raw) {                       /* 编码器判断压缩不划算时直接存裸块 */
        if (src_len < dst_len) return 2;
        memcpy(dst, src, (size_t)dst_len);
        return 0;
    }
    BS gb;
    gb.p = src;
    gb.end = src + src_len;
    if (fmt == FMT_DXT1) return dxv_dxt1(&gb, dst, dst_len);
    if (fmt == FMT_DXT5) return dxv_dxt5(&gb, dst, dst_len);
    return 2;
}
