#include "../android/cpp/AudioDiagnosticPolicy.h"
#include <cassert>
#include <cstdint>
#include <limits>

namespace {
int policy = 0, setCalls = 0, failAt = 0;
bool ignoreSet = false;
int getPolicy() { return policy; }
int setPolicy(int value) {
    ++setCalls;
    if (setCalls == failAt) return -895;
    if (!ignoreSet) policy = value;
    return 0;
}
void reset(int initial = 0) {
    policy = initial;
    setCalls = failAt = 0;
    ignoreSet = false;
}
}

int main() {
    assert(validAudioDiagnosticMode(0) && validAudioDiagnosticMode(1) && validAudioDiagnosticMode(2));
    assert(!validAudioDiagnosticMode(-1) && !validAudioDiagnosticMode(3));
    for (int original = 0; original <= 3; ++original) {
        reset(original);
        {
            ScopedMMapPolicy scope(getPolicy, setPolicy, true);
            assert(scope.ready && scope.available && scope.before == original && policy == 1);
            assert(scope.restore() && scope.restored && policy == original);
            assert(scope.restore() && setCalls == 2);
        }
        assert(policy == original && setCalls == 2);
    }
    reset();
    {
        ScopedMMapPolicy baseline(getPolicy, setPolicy, false);
        assert(baseline.ready && baseline.restored && baseline.before == 0 && policy == 0);
    }
    assert(setCalls == 0);
    {
        ScopedMMapPolicy unavailable(nullptr, nullptr, true);
        assert(!unavailable.ready && !unavailable.available && unavailable.setError == 889);
        ScopedMMapPolicy baseline(nullptr, nullptr, false);
        assert(baseline.ready && !baseline.available && !baseline.restored);
    }
    reset(3);
    failAt = 1;
    {
        ScopedMMapPolicy rejected(getPolicy, setPolicy, true);
        assert(!rejected.ready && rejected.setError == 895);
    }
    assert(policy == 3 && setCalls == 2); // Cleanup even if a setter reports failure.
    reset(2);
    ignoreSet = true;
    {
        ScopedMMapPolicy noReadback(getPolicy, setPolicy, true);
        assert(!noReadback.ready && noReadback.setError == ScopedMMapPolicy::readbackError);
    }
    assert(policy == 2);
    reset(0);
    failAt = 2;
    {
        ScopedMMapPolicy failedRestore(getPolicy, setPolicy, true);
        assert(failedRestore.ready && !failedRestore.restore());
        assert(!failedRestore.restored && failedRestore.restoreError == 895);
    }
    reset(17);
    {
        ScopedMMapPolicy invalidEnum(getPolicy, setPolicy, true);
        assert(!invalidEnum.available && !invalidEnum.ready && setCalls == 0);
    }

    constexpr int64_t now = 10000000000LL;
    auto output = estimateAudioTimestamp(true, false, 480000, now - 10000000, now, 480960, 48000);
    auto input = estimateAudioTimestamp(true, true, 480000, now - 10000000, now, 480400, 48000);
    assert(output.timestampAvailable && output.pendingAvailable && !output.future);
    assert(output.ageUs == 10000 && output.pendingFrames == 480);
    assert(input.timestampAvailable && input.pendingAvailable && input.pendingFrames == 80);
    auto future = estimateAudioTimestamp(true, false, 480000, now + 1000000, now, 480960, 48000);
    assert(future.timestampAvailable && future.future && future.ageUs == 1000 && !future.pendingAvailable);
    auto stale = estimateAudioTimestamp(true, false, 480000, now - 1000000001, now, 480960, 48000);
    assert(stale.timestampAvailable && !stale.pendingAvailable && stale.pendingFrames == 0);
    assert(!estimateAudioTimestamp(false, false, 480000, now, now, 480960, 48000).timestampAvailable);
    assert(!estimateAudioTimestamp(true, false, -1, now, now, 480960, 48000).timestampAvailable);
    assert(!estimateAudioTimestamp(true, false, 0, 0, now, 0, 48000).timestampAvailable);
    assert(!estimateAudioTimestamp(true, false, 0, now, now, -1, 48000).pendingAvailable);
    assert(!estimateAudioTimestamp(true, false, std::numeric_limits<int64_t>::max(), now, now, 0, 48000).timestampAvailable);
    auto veryOld = estimateAudioTimestamp(true, false, 0, 1, std::numeric_limits<int64_t>::max(), 0, 48000);
    assert(veryOld.timestampAvailable && !veryOld.pendingAvailable && veryOld.ageUs <= 1000000000000LL);
    auto underrun = estimateAudioTimestamp(true, false, 480000, now, now, 479000, 48000);
    assert(underrun.pendingAvailable && underrun.pendingFrames == 0);
}
