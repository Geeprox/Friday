#include "LGTVDSP.h"
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#define LGTV_RING_FRAMES 65536u
static _Atomic uint64_t routeCallbacks=0,routeFrames=0,routeNonzero=0;
void LGTVResetRouteStats(void) { atomic_store(&routeCallbacks,0);atomic_store(&routeFrames,0);atomic_store(&routeNonzero,0); }
LGTVRouteStats LGTVGetRouteStats(void) { return (LGTVRouteStats){atomic_load(&routeCallbacks),atomic_load(&routeFrames),atomic_load(&routeNonzero)}; }
struct LGTVAudioRing {
    _Atomic uint64_t stamps[LGTV_RING_FRAMES];
    _Atomic uint64_t samples[LGTV_RING_FRAMES];
};

LGTVAudioRing *LGTVRingCreate(void) { return calloc(1, sizeof(LGTVAudioRing)); }
void LGTVRingDestroy(LGTVAudioRing *ring) { free(ring); }
void LGTVRingReset(LGTVAudioRing *ring) {
    if (!ring) return;
    for (uint32_t i = 0; i < LGTV_RING_FRAMES; ++i) atomic_store(&ring->stamps[i], 0);
}
void LGTVRingWrite(LGTVAudioRing *ring, int64_t time, uint32_t frames, const float *data) {
    if (!ring || !data || time < 0 || frames > LGTV_RING_FRAMES || time > INT64_MAX / 4) return;
    for (uint32_t i = 0; i < frames; ++i) {
        uint64_t sample = (uint64_t)time + i, slot = sample % LGTV_RING_FRAMES, bits;
        memcpy(&bits, data + i * 2, sizeof(bits));
        // Atomic seqlock: an overwrite must never pair new PCM with an old timestamp.
        atomic_store(&ring->stamps[slot], sample * 2 + 1);
        atomic_store(&ring->samples[slot], bits);
        atomic_store(&ring->stamps[slot], sample * 2 + 2);
    }
}
void LGTVRingRead(LGTVAudioRing *ring, int64_t time, uint32_t frames, float *data) {
    if (!data || frames > LGTV_RING_FRAMES) return;
    memset(data, 0, frames * 2 * sizeof(float));
    if (!ring || time < 0 || time > INT64_MAX / 4) return;
    for (uint32_t i = 0; i < frames; ++i) {
        uint64_t sample = (uint64_t)time + i, slot = sample % LGTV_RING_FRAMES;
        uint64_t expected = sample * 2 + 2;
        if (atomic_load(&ring->stamps[slot]) != expected) continue;
        uint64_t bits = atomic_load(&ring->samples[slot]);
        if (atomic_load(&ring->stamps[slot]) == expected) memcpy(data + i * 2, &bits, sizeof(bits));
    }
}

static const float *channel(const AudioBufferList *list, uint32_t index, uint32_t *stride, uint32_t *frames) {
    if (!list) return NULL;
    for (uint32_t b = 0; b < list->mNumberBuffers; ++b) {
        const AudioBuffer *buffer = &list->mBuffers[b];
        if (index < buffer->mNumberChannels) {
            if (!buffer->mData || !buffer->mNumberChannels) return NULL;
            *stride = buffer->mNumberChannels;
            *frames = buffer->mDataByteSize / (sizeof(float) * *stride);
            return (const float *)buffer->mData + index;
        }
        index -= buffer->mNumberChannels;
    }
    return NULL;
}

bool LGTVRouteAudio(const AudioBufferList *input, AudioBufferList *output, uint32_t offset) {
    if (!output) return false;
    atomic_fetch_add(&routeCallbacks,1);
    for (uint32_t b = 0; b < output->mNumberBuffers; ++b)
        if (output->mBuffers[b].mData) memset(output->mBuffers[b].mData, 0, output->mBuffers[b].mDataByteSize);
    uint32_t channelIndex = 0;
    bool routed = false;
    for (uint32_t b = 0; b < output->mNumberBuffers; ++b) {
        AudioBuffer *buffer = &output->mBuffers[b];
        if (!buffer->mData || !buffer->mNumberChannels) { channelIndex += buffer->mNumberChannels; continue; }
        uint32_t frames = buffer->mDataByteSize / (sizeof(float) * buffer->mNumberChannels);
        float *destination = buffer->mData;
        for (uint32_t c = 0; c < buffer->mNumberChannels; ++c, ++channelIndex) {
            if (channelIndex < offset || channelIndex >= offset + 2) continue;
            uint32_t stride = 0, sourceFrames = 0;
            const float *source = channel(input, channelIndex - offset, &stride, &sourceFrames);
            if (!source) continue;
            uint32_t count = frames < sourceFrames ? frames : sourceFrames;
            uint64_t nonzero=0;
            for (uint32_t f = 0; f < count; ++f) {
                float sample=source[f * stride];destination[f * buffer->mNumberChannels + c] = sample;
                if(sample!=0) ++nonzero;
            }
            atomic_fetch_add(&routeNonzero,nonzero);
            if(channelIndex==offset) atomic_fetch_add(&routeFrames,count);
            routed = true;
        }
    }
    return routed;
}
