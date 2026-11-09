#pragma once
#include <algorithm>
#include <cstdint>

constexpr int fridayDuplexClientRate = 48000;
constexpr int fridayDuplexQueueBudget = 1200; // 25 ms, independent of HAL capacity.
constexpr int64_t fridayDuplexReadTimeoutNs = 10000000LL;
constexpr int64_t fridayDuplexStartupNs = 1500000000LL;
constexpr int64_t fridayDuplexDrainNs = 250000000LL;

inline int64_t fridayClientFrames(int64_t rawFrames, int rawRate) {
    if (rawFrames < 0 || rawFrames > 1000000000000LL || rawRate <= 0 || rawRate > 384000) return 0;
    return (rawFrames * fridayDuplexClientRate + rawRate - 1) / rawRate;
}

inline int fridayDrainFrameBudget(int64_t clientCapacity) {
    return static_cast<int>(std::max<int64_t>(2400, std::min<int64_t>(9600, clientCapacity)) * 2);
}

// Caller provides nonblocking reads. A zero before the first delivered frame is
// not a successful drain; it may just mean the asynchronous input has not begun.
struct FridayStartupDrain {
    int64_t deadline = 0, discarded = 0;
    int budget = 0;
    bool observedInput = false, complete = false;
    FridayStartupDrain(int64_t now, int64_t overallDeadline, int frameBudget)
        : deadline(std::min(overallDeadline, now + fridayDuplexDrainNs)), budget(frameBudget) {}
    bool mayRead(int64_t now, bool stopped) const {
        return !stopped && !complete && now < deadline && discarded < budget;
    }
    void observe(int frames) {
        if (frames > 0) { observedInput = true; discarded += frames; }
        else if (frames == 0 && observedInput) complete = true;
    }
};

struct FridayDuplexRead {
    int frames = 0, error = 0;
    bool cancelled = false;
};

// FilterAudioStream passes a timeout to every child read, not one outer-read
// deadline. Use nonblocking outer reads so resampler storage stays consumable,
// and enforce one aggregate budget with stop checks between attempts.
template<class Reader, class Clock, class Stopped, class Wait>
FridayDuplexRead fridayReadBounded(int frames, Reader read, Clock now,
        Stopped stopped, Wait wait) {
    FridayDuplexRead result;
    const int64_t deadline = now() + fridayDuplexReadTimeoutNs;
    while (result.frames < frames && now() < deadline) {
        if (stopped()) { result.cancelled = true; return result; }
        const auto part = read(result.frames, frames - result.frames);
        if (part.error) { result.error = part.error; return result; }
        if (part.frames < 0 || part.frames > frames - result.frames) {
            result.error = 60009;
            return result;
        }
        result.frames += part.frames;
        if (!part.frames) wait();
    }
    result.cancelled = stopped();
    return result;
}
