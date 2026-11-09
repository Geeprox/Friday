#pragma once
#include <atomic>
#include <cstdint>
#include <time.h>

// Host-only lifecycle seam. It models API ownership and counters, not hardware.
inline std::atomic<int> fakePlatformCalls{0}, fakeUseAfterClose{0}, fakeCloseDuringRead{0};
constexpr int AAUDIO_OK = 0;
struct AAudioStream {
    int rate = 48000, capacity = 2308, buffer = 2308, burst = 960, performance = 10;
    std::atomic<int> state{2}, device{2}, activeReads{0};
    std::atomic<int64_t> written{0}, read{0};
    std::atomic<int> timestampResult{-895};
    std::atomic<int64_t> timestampPosition{0}, timestampNs{0}, timestampDelayNs{0};
    std::atomic<bool> closed{false};
};
inline void fakeCheck(AAudioStream* stream) {
    ++fakePlatformCalls;
    if (!stream || stream->closed.load()) ++fakeUseAfterClose;
}
inline int AAudioStream_getSampleRate(AAudioStream* s) { fakeCheck(s); return s->rate; }
inline int AAudioStream_getBufferCapacityInFrames(AAudioStream* s) { fakeCheck(s); return s->capacity; }
inline int AAudioStream_getBufferSizeInFrames(AAudioStream* s) { fakeCheck(s); return s->buffer; }
inline int AAudioStream_getFramesPerBurst(AAudioStream* s) { fakeCheck(s); return s->burst; }
inline int AAudioStream_getPerformanceMode(AAudioStream* s) { fakeCheck(s); return s->performance; }
inline int AAudioStream_getState(AAudioStream* s) { fakeCheck(s); return s->state; }
inline int AAudioStream_getDeviceId(AAudioStream* s) { fakeCheck(s); return s->device; }
inline int AAudioStream_getXRunCount(AAudioStream* s) { fakeCheck(s); return 0; }
inline int64_t AAudioStream_getFramesWritten(AAudioStream* s) { fakeCheck(s); return s->written; }
inline int64_t AAudioStream_getFramesRead(AAudioStream* s) { fakeCheck(s); return s->read; }
inline int AAudioStream_getTimestamp(AAudioStream* s, clockid_t, int64_t* position, int64_t* time) {
    fakeCheck(s);
    const auto delay = s->timestampDelayNs.load();
    if (delay > 0) { timespec wait{0, static_cast<long>(delay)}; nanosleep(&wait, nullptr); }
    if (s->timestampResult == 0) { *position = s->timestampPosition; *time = s->timestampNs; }
    return s->timestampResult;
}
