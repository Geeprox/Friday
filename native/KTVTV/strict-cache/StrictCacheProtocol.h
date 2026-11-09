#ifndef FRIDAY_STRICT_CACHE_PROTOCOL_H
#define FRIDAY_STRICT_CACHE_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error Strict cache protocol v1 requires little endian
#endif
#define FRIDAY_CACHE_SOCKET "friday.ktv.strict-cache.v1"
#define FRIDAY_CACHE_MAGIC UINT32_C(0x46534331)
#define FRIDAY_CACHE_VERSION 1
#define FRIDAY_CACHE_SERVER_UID 1041
#define FRIDAY_CACHE_FRAMES 128
#define FRIDAY_CACHE_SAMPLES 256
#define FRIDAY_CACHE_PCM_BYTES 512
#define FRIDAY_CACHE_CAPABILITIES UINT32_C(31)
#define FRIDAY_CACHE_INLINE_CAPABILITIES UINT32_C(63)
enum FridayCacheCapability {FRIDAY_CACHE_SILENT_START=1,FRIDAY_CACHE_CACHE_ONLY=2,
    FRIDAY_CACHE_MEMORY_ONLY=4,FRIDAY_CACHE_BOUNDED_READ=8,FRIDAY_CACHE_VERIFIED_MAPPED_IMAGE=16,FRIDAY_CACHE_INLINE_VOICE=32};
#define FRIDAY_CACHE_OWNER_TTL_MS UINT64_C(750)
enum FridayCacheRole {FRIDAY_CACHE_OWNER=1,FRIDAY_CACHE_READER=2};
enum FridayCacheOpcode {FRIDAY_CACHE_HELLO=1,FRIDAY_CACHE_BEGIN=2,FRIDAY_CACHE_BIND=3,
    FRIDAY_CACHE_HEARTBEAT=4,FRIDAY_CACHE_END=5,FRIDAY_CACHE_READ=6,FRIDAY_CACHE_ATTACH=7,
    FRIDAY_CACHE_INLINE_CONFIGURE=8,FRIDAY_CACHE_INLINE_STATUS=9,FRIDAY_CACHE_INLINE_MUTE=10,FRIDAY_CACHE_INLINE_NOTCH=11};
/* Fixed LE wire layouts. No paths, strings, handles or arbitrary-size requests. */
typedef struct FridayCacheRequest {
    uint32_t magic;uint16_t version,opcode;
    uint64_t request_id,generation,lease;
    int32_t owner_pid,card,device,reader_pid;
    uint32_t frames,flags;uint64_t reserved;
} FridayCacheRequest;
typedef struct FridayCacheResponse {
    uint32_t magic;uint16_t version,opcode;
    uint64_t request_id,generation,lease,sequence;
    int32_t status,server_pid;uint32_t capabilities,sample_rate;
    uint16_t frames;uint8_t channels,pcm_bits;uint32_t reserved;
} FridayCacheResponse;
typedef struct FridayInlineMetrics {
    uint64_t requested_revision,applied_revision,callbacks,frames;
    int32_t profile,wet,monitor_gain;uint32_t peak,flags;int32_t fault;
} FridayInlineMetrics;
typedef struct FridayCachePacket {FridayCacheResponse header;union{int16_t pcm[FRIDAY_CACHE_SAMPLES];FridayInlineMetrics inline_metrics;};} FridayCachePacket;
#ifdef __cplusplus
static_assert(sizeof(FridayCacheRequest)==64 && sizeof(FridayCacheResponse)==64 && sizeof(FridayCachePacket)==576,"wire ABI");
static_assert(offsetof(FridayCacheResponse,status)==40 && offsetof(FridayCacheResponse,frames)==56,"wire offsets");
static_assert(sizeof(FridayInlineMetrics)==56,"inline scalar ABI");
#else
_Static_assert(sizeof(FridayCacheRequest)==64 && sizeof(FridayCacheResponse)==64 && sizeof(FridayCachePacket)==576,"wire ABI");
_Static_assert(sizeof(FridayInlineMetrics)==56,"inline scalar ABI");
#endif
static inline void friday_cache_wipe(void* pointer,size_t bytes){volatile unsigned char* p=(volatile unsigned char*)pointer;while(bytes--)*p++=0;}
static inline int friday_cache_request_size_valid(size_t bytes,int truncated){return bytes==sizeof(FridayCacheRequest)&&!truncated;}
static inline int friday_cache_capabilities_valid(uint32_t value){return value==FRIDAY_CACHE_CAPABILITIES||value==FRIDAY_CACHE_INLINE_CAPABILITIES;}
#endif
