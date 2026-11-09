#include "../android/cpp/AudioRoutePolicy.h"
#include <cassert>

int main() {
    // The measured TV pairing: 20ms ordinary USB input, 4ms low-latency output.
    assert(callbackRouteAllowed(true, false, true, 960, 2880));
    assert(callbackRouteAllowed(false, true, false, 192, 1536));
    // Input acceptance never relaxes the output requirement or admits large queues.
    assert(!callbackRouteAllowed(false, false, true, 192, 1536));
    assert(!callbackRouteAllowed(true, false, true, 1920, 2880));
    assert(!callbackRouteAllowed(true, false, true, 960, 9600));
    assert(!callbackRouteAllowed(true, false, false, 240, 960));
    assert(!callbackRouteAllowed(true, true, false, 0, 960));
    assert(callbackRouteAllowed(true, true, false, 240, 960));
}
