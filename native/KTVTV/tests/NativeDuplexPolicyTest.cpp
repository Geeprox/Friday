#include "../android/cpp/NativeDuplexPolicy.h"
#include <cassert>
#include <limits>

int main() {
    assert(fridayClientFrames(480, 96000) == 240);
    assert(fridayClientFrames(8192, 96000) == 4096);
    assert(fridayClientFrames(2308, 48000) == 2308);
    assert(fridayClientFrames(96000LL * 60 * 60 * 12, 96000) == 48000LL * 60 * 60 * 12);
    assert(fridayClientFrames(-1, 48000) == 0);
    assert(fridayClientFrames(std::numeric_limits<int64_t>::max(), 48000) == 0);
    assert(fridayDrainFrameBudget(4096) == 8192);
    assert(fridayDrainFrameBudget(1) == 4800 && fridayDrainFrameBudget(100000) == 19200);
    assert(fridayDrainFrameBudget(std::numeric_limits<int64_t>::max()) == 19200);
    FridayStartupDrain drain(0, 1000000000, 4800);
    drain.observe(0); assert(!drain.complete && drain.mayRead(1, false));
    drain.observe(240); drain.observe(0);
    assert(drain.complete && drain.discarded == 240 && !drain.mayRead(2, false));
    FridayStartupDrain missing(0, 1000, 4800);
    assert(!missing.mayRead(1000, false) && !missing.mayRead(1, true));
    FridayStartupDrain continuous(0, 1000000000, 4800);
    for (int i = 0; i < 20; ++i) continuous.observe(240);
    assert(!continuous.complete && !continuous.mayRead(1, false));

    int64_t now = 0; int calls = 0; bool stopped = false;
    auto clock = [&]() { return now; };
    auto stop = [&]() { return stopped; };
    auto wait = [&]() { now += 100000; };
    auto cached = fridayReadBounded(240, [&](int offset, int remaining) {
        ++calls;
        // Converter cache can satisfy a partial read despite no raw availability.
        assert(offset == (calls == 1 ? 0 : 224));
        return FridayDuplexRead{calls == 1 ? 224 : remaining, 0, false};
    }, clock, stop, wait);
    assert(cached.frames == 240 && !cached.error && calls == 2 && now == 0);
    now = 0; calls = 0;
    auto empty = fridayReadBounded(240, [&](int, int) {
        ++calls; return FridayDuplexRead{};
    }, clock, stop, wait);
    assert(empty.frames == 0 && calls == 100 && now == fridayDuplexReadTimeoutNs);
    now = 0; calls = 0;
    auto cancelled = fridayReadBounded(240, [&](int, int) {
        ++calls; stopped = true; return FridayDuplexRead{};
    }, clock, stop, wait);
    assert(cancelled.cancelled && calls == 1);
    stopped = false; now = 0;
    auto error = fridayReadBounded(240, [](int, int) { return FridayDuplexRead{0, 899, false}; }, clock, stop, wait);
    assert(error.error == 899);
    auto invalid = fridayReadBounded(240, [](int, int) { return FridayDuplexRead{241, 0, false}; }, clock, stop, wait);
    assert(invalid.error == 60009);
}
