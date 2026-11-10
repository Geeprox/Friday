#include "../android/cpp/AudioMonitorTelemetry.h"
#include <cassert>

int main() {
    constexpr int64_t start = 1000000000LL, deadline = 20000000000LL;
    assert(!audioMonitorSteadyWindow(start, start + 999999999, deadline, true, false, false, 0));
    assert(audioMonitorSteadyWindow(start, start + 1000000000, deadline, true, false, false, 0));
    assert(!audioMonitorSteadyWindow(start, deadline, deadline, true, false, false, 0));
    assert(!audioMonitorSteadyWindow(start, start + 2000000000, deadline, true, true, false, 0));
    assert(!audioMonitorSteadyWindow(start, start + 2000000000, deadline, true, false, true, 0));
    assert(!audioMonitorSteadyWindow(start, start + 2000000000, deadline, true, false, false, 885));

    AudioMonitorPlatformStats stats;
    AudioMonitorRawCounters output{48000, 45600, 48000, 5};
    AudioMonitorRawCounters input{96000, 92160, 96000, 3};
    AudioTimestampEstimate outTiming, inTiming;
    outTiming.pendingAvailable = true; outTiming.pendingFrames = 1920;
    inTiming.pendingAvailable = true; inTiming.pendingFrames = 2880;
    stats.add(2000000000, output, &input, outTiming, inTiming);
    assert(stats.count == 1 && stats.outputCounter.minimum == 50000);
    assert(stats.inputCounter.minimum == 40000); // Raw 96k domain, not 48k.
    assert(stats.outputPending.last == 40000 && stats.inputPending.last == 30000);
    assert(!stats.outputXruns.available() && stats.outputXruns.delta() == 0);
    assert(stats.clockSpanMs() == 0); // One point cannot establish a rate window.

    output = {96000, 94080, 48000, 7};
    input = {192000, 187200, 96000, 3};
    outTiming.pendingAvailable = false; // Missing timestamps must not add zero.
    inTiming.pendingFrames = 1920;
    stats.add(3000000000, output, &input, outTiming, inTiming);
    assert(stats.count == 2 && stats.outputPending.count == 1);
    assert(stats.outputPending.minimum == 40000 && stats.outputPending.maximum == 40000);
    assert(stats.inputPending.count == 2 && stats.inputPending.minimum == 20000);
    assert(stats.inputPending.maximum == 30000 && stats.inputPending.last == 20000);
    assert(stats.outputCounter.minimum == 40000 && stats.outputCounter.maximum == 50000);
    assert(stats.inputCounter.minimum == 40000 && stats.inputCounter.maximum == 50000);
    assert(stats.outputXruns.available() && stats.outputXruns.delta() == 2);
    assert(stats.inputXruns.available() && stats.inputXruns.delta() == 0);
    assert(stats.clockSpanMs() == 1000 && stats.inputProduced() == 96000);
    assert(stats.outputPresented() == 48480);

    auto invalid = input; invalid.read = invalid.written + 1;
    stats.add(4000000000, output, &invalid, outTiming, inTiming);
    assert(stats.count == 2); // Crossed asynchronous counters stay unknown.
    invalid = input; invalid.rate = 0;
    stats.add(4000000000, output, &invalid, outTiming, inTiming);
    assert(stats.count == 2);

    // Availability is separate from the numeric zero sentinel after API failure.
    output.written += 48000; output.read += 48000; output.xruns = -889;
    input.written += 96000; input.read += 96000;
    stats.add(4000000000, output, &input, outTiming, inTiming);
    assert(!stats.outputXruns.available() && stats.outputXruns.delta() == 0);
    input.written = 10000; input.read = 9000;
    stats.add(5000000000, output, &input, outTiming, inTiming);
    assert(stats.clockSpanMs() == 0 && stats.inputProduced() == 0 && stats.outputPresented() == 0);
    AudioXRunDelta reset;
    reset.add(9); reset.add(2);
    assert(!reset.available() && reset.delta() == 0);

    AudioMonitorPlatformStats outputOnly;
    outputOnly.add(2000000000, output, nullptr, outTiming, inTiming);
    assert(outputOnly.count == 1 && outputOnly.inputCounter.count == 0);
    assert(outputOnly.inputPending.count == 0 && !outputOnly.inputXruns.available());
}
