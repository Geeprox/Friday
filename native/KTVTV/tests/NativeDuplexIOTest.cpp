// Build with -Itests/native_duplex_fakes. These deterministic host stand-ins
// exercise the real JNI implementation, not Android/AAudio physical behavior.
#include "../android/cpp/NativeDuplexIO.cpp"
#include <cassert>
#include <chrono>
#include <future>
#include <thread>

std::mutex* fridayPolicyMutexFromOtherTranslationUnit();
using namespace std::chrono_literals;

namespace {
JNIEnv environment;
jlong openTest(int mode = 0) {
    const auto h = Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, mode);
    assert(h > 0);
    return h;
}
void startTest(jlong h) {
    assert(Java_local_friday_ktv_NativeDuplexIO_startNative(&environment, nullptr, h) == 0);
}
void closeTest(jlong h) {
    Java_local_friday_ktv_NativeDuplexIO_closeNative(&environment, nullptr, h);
    assert(!handles.acquire(h));
}
std::array<jlong, 91> metricsTest(jlong h) {
    FakeLongArray array(91);
    Java_local_friday_ktv_NativeDuplexIO_metricsNative(&environment, nullptr, h, &array);
    std::array<jlong, 91> result{};
    std::copy(array.values.begin(), array.values.end(), result.begin());
    return result;
}
void checkNoLiveStreams() {
    assert(oboe::fakeStreamsAlive == 0);
    assert(fakeUseAfterClose == 0 && fakeCloseDuringRead == 0);
}
}

int main() {
    assert(fridayPolicyMutexFromOtherTranslationUnit() == &fridayAudioOpenPolicyMutex());
    {
        std::unique_lock<std::mutex> hold(fridayAudioOpenPolicyMutex());
        auto other = std::async(std::launch::async, [] {
            return fridayPolicyMutexFromOtherTranslationUnit()->try_lock();
        });
        assert(!other.get());
    }
    // Initial/invalid/failure evidence is exactly 91 items and unknown xruns are -1.
    auto initial = Java_local_friday_ktv_NativeDuplexIO_openMetricsNative(&environment, nullptr);
    assert(initial->values.size() == 91 && initial->values[12] == -1 && initial->values[63] == 1);
    assert(initial->values[52] == 0 && initial->values[84] == 0 && initial->values[75] == 0);
    delete initial;
    assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 0, 2, 2, 0) == 0);
    assert(lastCode == 50004);
    assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, 2) == 0);
    assert(lastCode == 50004);
    fakeSymbolsAvailable = false;
    assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, 0) == 0);
    assert(lastCode == 50001);
    fakeSymbolsAvailable = true;
    fakePolicy = 2;
    oboe::fakeOutputDevice = 7;
    assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, 0) == 0);
    assert(lastCode == 20001 && fakePolicy == 2); // Default route was not forced to builtin 2.
    oboe::fakeOutputDevice = 2;
    fakeMMapUsed = true;
    assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, 0) == 0);
    assert(lastCode == 50005 && fakePolicy == 2);
    fakeMMapUsed = false;
    checkNoLiveStreams();

    jlong firstHandle;
    {
        firstHandle = openTest();
        auto owner = handles.acquire(firstHandle);
        FakeShortArray input(480), output(960);
        std::fill(output.values.begin(), output.values.end(), 1234);
        assert(Java_local_friday_ktv_NativeDuplexIO_writeNative(&environment, nullptr,
                firstHandle, &output, 0, 960) == -1); // Preflight cannot emit mixed PCM.
        startTest(firstHandle);
        assert(owner->drainedFrames == 480 && owner->drainCompleted);
        startTest(firstHandle); // Idempotent; do not discard a second interval of voice.
        assert(owner->drainedFrames == 480);
        auto m = metricsTest(firstHandle);
        assert(m[2] == 2 && m[3] == 9 && m[5] == 4096 && m[7] == 240);
        assert(m[16] == 48000 && m[17] == 48000 && m[37] == 48000 && m[38] == 96000);
        assert(m[36] == 1200 && m[47] == 1 && m[48] == 1 && m[49] == 1 && m[56] == 0);
        assert(m[57] == 1 && m[58] == 2 && m[60] == 1 && m[63] == 1 && fakePolicy == 2);
        assert(m[25] > 0 && m[50] == 0 && m[52] == 0); // Timestamp unavailable, not measured zero.
        assert(m[64] == 0 && m[65] == 480 && m[66] <= m[25] && m[68] == 895 && m[84] == 1);
        assert(m[88] == 96000 && m[89] == 1 && m[90] == 0); // Normal production stays Medium.
        assert(m[45] == m[46] && m[46] / 2 - m[64] - m[65] == 0);
        // A lifecycle lock held by input/close must not stall fast metrics or output producer.
        {
            std::unique_lock<std::mutex> held(owner->lifecycle);
            const auto queries = fakePlatformCalls.load();
            auto fast = std::async(std::launch::async, [&] {
                JNIEnv env; FakeLongArray result(91);
                for (int i = 0; i < 1000; ++i)
                    Java_local_friday_ktv_NativeDuplexIO_metricsNative(&env, nullptr, firstHandle, &result);
                return Java_local_friday_ktv_NativeDuplexIO_writeNative(&env, nullptr,
                        firstHandle, &output, 0, 960);
            });
            assert(fast.wait_for(1s) == std::future_status::ready && fast.get() == 960);
            assert(fakePlatformCalls == queries);
        }
        assert(Java_local_friday_ktv_NativeDuplexIO_writeNative(&environment, nullptr,
                firstHandle, &output, 0, 960) == 960);
        assert(Java_local_friday_ktv_NativeDuplexIO_writeNative(&environment, nullptr,
                firstHandle, &output, 0, 960) == 480); // Exactly 1200 frames, no HAL-capacity target.
        assert(owner->queue.size() == 1200);
        std::array<int16_t, 1920> rendered{};
        assert(owner->onAudioReady(nullptr, rendered.data(), 960) == oboe::DataCallbackResult::Continue);
        for (auto sample : rendered) assert(sample == 1234);
        assert(owner->queue.size() == 240);
        owner->input->reader = [&](void* destination, int32_t requested, int64_t timeout) {
            assert(timeout == 0);
            const int count = std::min(8, requested);
            std::fill_n(static_cast<int16_t*>(destination), count * 2, int16_t(4321));
            return oboe::ResultWithValue<int32_t>(count);
        };
        assert(Java_local_friday_ktv_NativeDuplexIO_readNative(&environment, nullptr,
                firstHandle, &input, 240) == 480);
        for (auto sample : input.values) assert(sample == 4321);
        const auto fresh = metricsTest(firstHandle);
        assert(fresh[27] == 240 && fresh[72] == 1 && fresh[75] == 1 && fresh[86] == 240 && fresh[87] == 240);
        // A fast snapshot may refresh old [27], but cannot corrupt the paired
        // sample counters or raw read snapshot. No new platform getter is used.
        for (int index : {25, 45, 46, 64, 65, 66, 67, 68, 69, 70, 71, 84})
            assert(fresh[index] == m[index]);
        assert(fresh[46] / 2 - fresh[64] - fresh[65] == 0);
        assert(fresh[46] / 2 - fresh[27] - fresh[28] == -240); // Old cross-time arithmetic is invalid.
        owner->output->native.device = 7;
        assert(Java_local_friday_ktv_NativeDuplexIO_routeNative(&environment, nullptr, firstHandle) == 7);
        assert(owner->error == 60002 && owner->stopped);
        closeTest(firstHandle);
        assert(owner->input == nullptr && owner->output == nullptr);
    }
    checkNoLiveStreams();

    // Only explicit mode 1 requests no SRC. A wrong raw or known hardware rate
    // fails before start and retains that exact evidence, without a mode-0 retry.
    {
        const auto before = oboe::fakeInputOpenCount.load();
        oboe::fakeStrictInputRawRate = 96000;
        assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, 1) == 0);
        assert(lastCode == 40007 && lastEvidence[38] == 96000 && lastEvidence[90] == 1);
        assert(oboe::fakeInputOpenCount == before + 1);
        oboe::fakeStrictInputRawRate = 48000;
        oboe::fakeStrictInputHardwareRate = 96000;
        assert(Java_local_friday_ktv_NativeDuplexIO_openNative(&environment, nullptr, 9, 2, 2, 1) == 0);
        assert(lastCode == 40008 && lastEvidence[38] == 48000 && lastEvidence[88] == 96000);
        assert(lastEvidence[89] == 1 && lastEvidence[90] == 1 && lastEvidence[29] == 0);
        assert(oboe::fakeInputOpenCount == before + 2);
        oboe::fakeStrictInputHardwareRate = 48000;
    }
    checkNoLiveStreams();
    for (const int hardwareRate : {48000, 0}) {
        oboe::fakeStrictInputHardwareRate = hardwareRate;
        oboe::fakeStrictInputPerformance = 10; // Stable transport is tested independently of FAST.
        const auto h = openTest(1); startTest(h);
        auto owner = handles.acquire(h);
        auto m = metricsTest(h);
        assert(m[17] == 48000 && m[38] == 48000 && m[9] == 10 && m[90] == 1);
        assert(m[88] == hardwareRate && m[89] == (hardwareRate > 0 ? 1 : 0));
        assert(m[45] == m[46] && m[46] == m[64] + m[65]);
        assert(m[84] == 1 && fakePolicy == 2);
        owner->input->reader = [](void* destination, int32_t frames, int64_t timeout) {
            assert(timeout == 0);
            std::fill_n(static_cast<int16_t*>(destination), frames * 2, int16_t(123));
            return oboe::ResultWithValue<int32_t>(frames);
        };
        FakeShortArray input(480);
        assert(Java_local_friday_ktv_NativeDuplexIO_readNative(&environment, nullptr, h, &input, 240) == 480);
        Java_local_friday_ktv_NativeDuplexIO_routeNative(&environment, nullptr, h);
        m = metricsTest(h);
        assert(m[64] == 240 && m[46] == m[64] + m[65]);
        closeTest(h);
    }
    oboe::fakeStrictInputHardwareRate = 48000; oboe::fakeStrictInputPerformance = 12;
    checkNoLiveStreams();

    // Sampling remains low frequency and separates unavailable/invalid/future
    // timestamps from same-window counter evidence. Timings include lifecycle
    // contention that the existing inner read-wait histogram cannot see.
    {
        const auto h = openTest(); startTest(h);
        auto owner = handles.acquire(h);
        owner->input->reader = [](void* destination, int32_t frames, int64_t timeout) {
            assert(timeout == 0);
            std::fill_n(static_cast<int16_t*>(destination), frames * 2, int16_t(321));
            return oboe::ResultWithValue<int32_t>(frames);
        };
        std::unique_lock<std::mutex> held(owner->lifecycle);
        auto capture = std::async(std::launch::async, [h] {
            JNIEnv env; FakeShortArray input(480);
            return Java_local_friday_ktv_NativeDuplexIO_readNative(&env, nullptr, h, &input, 240);
        });
        const auto waitDeadline = std::chrono::steady_clock::now() + 1s;
        while (owner->readCalls.load() == 0 && std::chrono::steady_clock::now() < waitDeadline)
            std::this_thread::yield();
        assert(owner->readCalls == 1);
        std::this_thread::sleep_for(25ms);
        held.unlock();
        assert(capture.wait_for(1s) == std::future_status::ready && capture.get() == 480);
        auto m = metricsTest(h);
        assert(m[73] >= 20000 && m[74] >= m[73] && m[75] == 1);
        assert(m[34] < m[73]); // Fast native reads, but a real application-side lock wait.

        auto& in = owner->input->native;
        in.read += 480; in.written = in.read.load(); // Model one converted 240-frame prefetch block.
        in.timestampResult = 0; in.timestampPosition = in.read.load();
        in.timestampNs = clockNs() - 1000000; in.timestampDelayNs = 2000000;
        const auto refreshes = m[80];
        Java_local_friday_ktv_NativeDuplexIO_routeNative(&environment, nullptr, h);
        m = metricsTest(h);
        assert(m[84] == 1 && m[64] == 240 && m[65] == 480);
        assert(m[46] / 2 - m[64] - m[65] == 240);
        assert(m[68] == 0 && m[52] == 1 && m[55] == 1 && m[69] == in.timestampPosition);
        assert(m[70] >= 1000 && m[71] == 0 && m[67] >= 2000);
        assert(m[80] == refreshes + 1 && m[81] >= m[82] && m[82] == m[67]);
        const auto calls = fakePlatformCalls.load();
        for (int i = 0; i < 1000; ++i) {
            const auto fast = metricsTest(h);
            assert(fast[64] == m[64] && fast[66] == m[66] && fast[70] == m[70]);
            assert(fast[80] == m[80] && fast[82] == m[82]);
        }
        assert(fakePlatformCalls == calls);

        // Successful delivery counts and thresholds exclude zero-data reads.
        owner->lastDeliveryAt.store(clockNs() - 60000000);
        FakeShortArray input(480);
        assert(Java_local_friday_ktv_NativeDuplexIO_readNative(&environment, nullptr, h, &input, 120) == 240);
        auto delivery = metricsTest(h);
        assert(delivery[75] == 2 && delivery[76] >= 60000);
        assert(delivery[77] == 1 && delivery[78] == 1 && delivery[79] == 1);
        assert(delivery[86] == 120 && delivery[87] == 240 && delivery[85] < 1000000);
        assert(delivery[64] == m[64] && delivery[46] == m[46]);
        owner->input->reader = [](void*, int32_t, int64_t) { return oboe::ResultWithValue<int32_t>(0); };
        assert(Java_local_friday_ktv_NativeDuplexIO_readNative(&environment, nullptr, h, &input, 240) == 0);
        const auto empty = metricsTest(h);
        assert(empty[72] == 3 && empty[75] == 2 && empty[76] == delivery[76] && empty[86] == 120);

        in.timestampNs = clockNs() + 1000000000;
        Java_local_friday_ktv_NativeDuplexIO_routeNative(&environment, nullptr, h);
        const auto future = metricsTest(h);
        assert(future[52] == 1 && future[55] == 0 && future[68] == 0 && future[71] == 1);
        in.timestampResult = -895;
        Java_local_friday_ktv_NativeDuplexIO_routeNative(&environment, nullptr, h);
        const auto unavailable = metricsTest(h);
        assert(unavailable[52] == 0 && unavailable[55] == 0 && unavailable[68] == 895);
        assert(unavailable[69] == 0 && unavailable[70] == 0 && unavailable[71] == 0);
        closeTest(h);
        assert(owner->finalMetrics[84] == 0 && owner->finalMetrics[72] == 3);
        const auto finalCalls = fakePlatformCalls.load();
        assert(owner->fastSnapshot() == owner->finalMetrics && fakePlatformCalls == finalCalls);
    }
    checkNoLiveStreams();

    // Failed start evidence survives handle close and a subsequent fallback.
    {
        const auto h = openTest();
        auto owner = handles.acquire(h);
        owner->output->native.device = 7;
        assert(Java_local_friday_ktv_NativeDuplexIO_startNative(&environment, nullptr, h) == -60002);
        assert(lastCode == 60002 && owner->finalMetrics[14] == 60002);
        closeTest(h);
        auto evidence = Java_local_friday_ktv_NativeDuplexIO_openMetricsNative(&environment, nullptr);
        assert(evidence->values.size() == 91 && evidence->values[14] == 60002 && evidence->values[2] == 7);
        assert(evidence->values[84] == 0);
        delete evidence;
    }
    // With no initial input, a zero read cannot falsely complete the startup drain.
    oboe::fakeInputStartupFrames = 0;
    {
        const auto h = openTest();
        assert(Java_local_friday_ktv_NativeDuplexIO_startNative(&environment, nullptr, h) == -60004);
        closeTest(h);
    }
    oboe::fakeInputStartupFrames = 20000;
    {
        const auto h = openTest();
        auto owner = handles.acquire(h);
        assert(Java_local_friday_ktv_NativeDuplexIO_startNative(&environment, nullptr, h) == -60005);
        assert(owner->drainedFrames == 8192);
        closeTest(h);
    }
    oboe::fakeInputStartupFrames = 480;
    checkNoLiveStreams();

    // Repeated real-JNI races: read holds its stream lease, fast metrics/write
    // proceed independently, stop cancels, and close removes the token before
    // releasing streams. Alternate concurrent Oboe error cleanup with user close.
    for (int iteration = 0; iteration < 80; ++iteration) {
        const auto h = openTest((iteration / 2) % 2); assert(h != firstHandle);
        startTest(h);
        auto owner = handles.acquire(h);
        std::atomic<int> reads{0};
        owner->input->reader = [&](void*, int32_t, int64_t timeout) {
            assert(timeout == 0); ++reads; return oboe::ResultWithValue<int32_t>(0);
        };
        auto capture = std::async(std::launch::async, [&] {
            JNIEnv env; FakeShortArray samples(480);
            return Java_local_friday_ktv_NativeDuplexIO_readNative(&env, nullptr, h, &samples, 240);
        });
        while (!reads.load()) std::this_thread::yield();
        auto telemetry = std::async(std::launch::async, [&] {
            JNIEnv env; FakeLongArray report(91); FakeShortArray pcm(480);
            for (int i = 0; i < 100; ++i)
                Java_local_friday_ktv_NativeDuplexIO_metricsNative(&env, nullptr, h, &report);
            return Java_local_friday_ktv_NativeDuplexIO_writeNative(&env, nullptr, h, &pcm, 0, 480);
        });
        std::future<bool> error;
        if (iteration % 2) {
            auto stream = owner->input;
            error = std::async(std::launch::async, [owner, stream] {
                return owner->onError(stream.get(), oboe::Result::ErrorDisconnected);
            });
        }
        Java_local_friday_ktv_NativeDuplexIO_stopNative(&environment, nullptr, h);
        closeTest(h);
        assert(capture.wait_for(1s) == std::future_status::ready);
        assert(capture.get() <= 0);
        assert(telemetry.wait_for(1s) == std::future_status::ready);
        assert(telemetry.get() <= 480);
        if (error.valid()) assert(error.get());
        assert(!handles.acquire(h));
        Java_local_friday_ktv_NativeDuplexIO_closeNative(&environment, nullptr, firstHandle);
        auto cached = owner->fastSnapshot();
        assert(cached[25] > 0 && owner->closed);
    }
    checkNoLiveStreams();
}
