#pragma once

// Callback delivery bounds, not a claim about microphone/DAC acoustic latency.
// An ordinary USB input must not force an otherwise low-latency output to Java.
inline bool callbackRouteAllowed(bool input, bool lowLatency, bool ordinary,
                                 int burstFrames, int bufferFrames) {
    if (burstFrames <= 0 || bufferFrames <= 0) return false;
    if (!input) return lowLatency && burstFrames <= 480;
    return (lowLatency || ordinary) && burstFrames <= 960 && bufferFrames <= 3840;
}
