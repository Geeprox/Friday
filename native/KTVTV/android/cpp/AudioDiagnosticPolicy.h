#pragma once

#include <algorithm>
#include <cstdint>

// Diagnostic variants only. A request is not evidence of the negotiated path.
enum class AudioDiagnosticMode : int {
    Shared = 0,
    SharedWithoutMMap = 1,
    Exclusive = 2,
};

inline bool validAudioDiagnosticMode(int mode) {
    return mode >= 0 && mode <= 2;
}

// AAudio's test policy is process-local. Preserve its exact original enum,
// including Unspecified (0); restoring a bool would silently change that policy.
// The caller serializes opens. No audio callback uses this helper.
class ScopedMMapPolicy {
public:
    using Getter = int (*)();
    using Setter = int (*)(int);
    static constexpr int unavailableError = 889;
    static constexpr int readbackError = 1003;

    ScopedMMapPolicy(Getter get, Setter set, bool disable)
        : get_(get), set_(set) {
        available = get_ && set_;
        if (available) {
            before = get_();
            available = before >= 0 && before <= 3;
        }
        if (!available) {
            before = 0;
            if (disable) setError = unavailableError;
            ready = !disable;
            return;
        }
        restored = true; // The default and exclusive variants do not mutate policy.
        if (!disable) {
            ready = true;
            return;
        }
        attempted_ = true;
        restored = false;
        const int result = set_(1); // AAUDIO_POLICY_NEVER
        setError = magnitude(result);
        if (!setError && get_() != 1) setError = readbackError;
        ready = setError == 0;
    }

    ~ScopedMMapPolicy() { restore(); }
    ScopedMMapPolicy(const ScopedMMapPolicy&) = delete;
    ScopedMMapPolicy& operator=(const ScopedMMapPolicy&) = delete;

    bool restore() {
        if (!attempted_) return !available || restored;
        attempted_ = false;
        const int result = set_(before);
        restoreError = magnitude(result);
        if (!restoreError && get_() != before) restoreError = readbackError;
        restored = restoreError == 0;
        return restored;
    }

    bool available = false, ready = false, restored = false;
    int before = 0, setError = 0, restoreError = 0;

private:
    static int magnitude(int value) {
        return static_cast<int>(std::min<int64_t>(100000,
                value < 0 ? -static_cast<int64_t>(value) : value));
    }
    Getter get_ = nullptr;
    Setter set_ = nullptr;
    bool attempted_ = false;
};

// Timestamp estimates are platform evidence, never an acoustic measurement.
// Both ends of a duplex sample use the same monotonic now. Invalid, future or
// stale timestamps stay visible but cannot produce a misleading pending count.
struct AudioTimestampEstimate {
    bool timestampAvailable = false, future = false, pendingAvailable = false;
    int64_t framePosition = 0, ageUs = 0, pendingFrames = 0;
};

inline AudioTimestampEstimate estimateAudioTimestamp(bool success, bool input,
        int64_t framePosition, int64_t timeNs, int64_t nowNs,
        int64_t framesTransferred, int sampleRate) {
    AudioTimestampEstimate result;
    constexpr int64_t counterLimit = 1000000000000LL;
    if (!success || framePosition < 0 || framePosition > counterLimit || timeNs <= 0
            || nowNs <= 0 || sampleRate <= 0 || sampleRate > 384000) return result;
    result.timestampAvailable = true;
    result.framePosition = framePosition;
    result.future = timeNs > nowNs;
    const int64_t ageNs = result.future ? timeNs - nowNs : nowNs - timeNs;
    result.ageUs = std::min<int64_t>(counterLimit, ageNs / 1000);
    if (result.future || ageNs > 1000000000LL || framesTransferred < 0
            || framesTransferred > counterLimit) return result;
    const int64_t estimatedHardwareFrame = framePosition + ageNs * sampleRate / 1000000000LL;
    const int64_t pending = input ? estimatedHardwareFrame - framesTransferred
                                 : framesTransferred - estimatedHardwareFrame;
    result.pendingAvailable = true;
    result.pendingFrames = std::max<int64_t>(0, std::min(counterLimit, pending));
    return result;
}
