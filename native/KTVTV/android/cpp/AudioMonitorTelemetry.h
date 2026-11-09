#pragma once
#include <algorithm>
#include <cstdint>
#include "AudioDiagnosticPolicy.h"

// Aggregate diagnostic observations only; no platform calls, PCM retention,
// allocation, or synchronization. The owner calls platform aggregation off RT.
constexpr int64_t audioMetricLimit = 1000000000000LL;

inline bool audioMonitorSteadyWindow(int64_t start, int64_t now, int64_t deadline,
        bool armed, bool cancelled, bool expired, int error) {
    return start > 0 && now >= start && now - start >= 1000000000LL
            && now < deadline && armed && !cancelled && !expired && error == 0;
}

inline int64_t audioFramesToMicros(int64_t frames, int rate) {
    if (frames < 0 || frames > audioMetricLimit || rate <= 0 || rate > 384000) return 0;
    return std::min<int64_t>(audioMetricLimit, frames * 1000000LL / rate);
}

struct AudioMetricRange {
    int64_t count = 0, minimum = 0, maximum = 0, last = 0;
    void add(int64_t value) {
        value = std::max<int64_t>(0, std::min(audioMetricLimit, value));
        if (count == 0) minimum = maximum = value;
        else { minimum = std::min(minimum, value); maximum = std::max(maximum, value); }
        last = value;
        ++count;
    }
};

struct AudioXRunDelta {
    int64_t observations = 0, first = 0, last = 0;
    bool valid = true;
    void add(int64_t value) {
        if (value < 0 || value > audioMetricLimit
                || (observations > 0 && value < last)) { valid = false; return; }
        if (observations == 0) first = value;
        last = value;
        ++observations;
    }
    bool available() const { return valid && observations >= 2; }
    int64_t delta() const { return available() ? last - first : 0; }
};

struct AudioMonitorRawCounters {
    int64_t written = 0, read = 0;
    int rate = 0, xruns = -1;
    bool valid() const {
        return rate > 0 && rate <= 384000 && read >= 0 && written >= read
                && written <= audioMetricLimit;
    }
};

// Raw input/output counters have independent frame domains (e.g. 96k and 48k).
// Counter queue ranges and timestamp-derived estimates are kept separate.
// count==0 means unavailable. A zero xrun delta is meaningful only with available().
struct AudioMonitorPlatformStats {
    int64_t count = 0;
    AudioMetricRange outputPending, inputPending, outputCounter, inputCounter;
    AudioXRunDelta outputXruns, inputXruns;
    int64_t firstAt = 0, lastAt = 0, firstInput = 0, lastInput = 0,
            firstOutput = 0, lastOutput = 0;
    int firstInputRate = 0, firstOutputRate = 0;
    bool clockValid = true, hasInput = false;

    void add(int64_t now, const AudioMonitorRawCounters& out,
            const AudioMonitorRawCounters* in, const AudioTimestampEstimate& outTiming,
            const AudioTimestampEstimate& inTiming) {
        // Invalid/crossed counter snapshots are not relabeled as a measured zero.
        if (now <= 0 || !out.valid() || (in && !in->valid())) return;
        outputCounter.add(audioFramesToMicros(out.written - out.read, out.rate));
        outputXruns.add(out.xruns);
        if (outTiming.pendingAvailable)
            outputPending.add(audioFramesToMicros(outTiming.pendingFrames, out.rate));
        if (in) {
            inputCounter.add(audioFramesToMicros(in->written - in->read, in->rate));
            inputXruns.add(in->xruns);
            if (inTiming.pendingAvailable)
                inputPending.add(audioFramesToMicros(inTiming.pendingFrames, in->rate));
        }
        if (count == 0) {
            firstAt = now;
            firstInput = in ? in->written : 0;
            firstOutput = out.read;
            firstInputRate = in ? in->rate : 0;
            firstOutputRate = out.rate;
            hasInput = in != nullptr;
        } else if (now <= lastAt || (in != nullptr) != hasInput
                || out.rate != firstOutputRate || out.read < lastOutput
                || (in && (in->rate != firstInputRate || in->written < lastInput))) {
            clockValid = false;
        }
        lastAt = now;
        lastInput = in ? in->written : 0;
        lastOutput = out.read;
        ++count;
    }
    int64_t clockSpanMs() const {
        return clockValid && count >= 2 ? (lastAt - firstAt) / 1000000 : 0;
    }
    int64_t inputProduced() const {
        return clockSpanMs() > 0 && hasInput ? lastInput - firstInput : 0;
    }
    int64_t outputPresented() const {
        return clockSpanMs() > 0 ? lastOutput - firstOutput : 0;
    }
};
