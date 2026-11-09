#pragma once
#include <cstdint>
#include <algorithm>

// A diagnostic-only, fixed-gain voice path. It never mixes music and does not
// claim to measure ADC or DAC latency. Production DSP is not changed.
inline int directVoiceMono(int16_t a, int16_t b) {
    return (static_cast<int>(a)+static_cast<int>(b))/2;
}
inline int directVoiceUnclamped(int mono) {
    // 0.08 stays within the existing peak ceiling for every I16 input.
    // The former 0.10 gain clipped the highest 20% of the mono input range.
    return mono*8/100;
}
inline int16_t directVoiceSample(int16_t a, int16_t b) {
    return static_cast<int16_t>(std::max(-2621,std::min(2621,
            directVoiceUnclamped(directVoiceMono(a,b)))));
}
inline bool directVoiceMayPlay(bool armed, bool cancelled, int64_t now, int64_t deadline) {
    return armed&&!cancelled&&deadline>0&&now<deadline;
}
