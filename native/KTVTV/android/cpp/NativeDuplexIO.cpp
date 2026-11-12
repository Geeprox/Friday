// Candidate production backend. Not added to the build until integration review.
#include <oboe/Oboe.h>
#include <aaudio/AAudio.h>
#include <jni.h>
#include <dlfcn.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <time.h>
#include "AudioDiagnosticPolicy.h"
#include "AudioHandleRegistry.h"
#include "NativeDuplexPolicy.h"
#include "PcmQueue.h"
#include "SharedAudioOpenPolicy.h"

namespace {
constexpr size_t metricCount = 91;
constexpr int64_t metricLimit = 1000000000000LL;
static_assert(std::atomic<int64_t>::is_always_lock_free, "Audio callback counters require lock-free atomics");

int64_t clockNs() {
    timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000000000LL + t.tv_nsec;
}
void briefWait() { timespec delay{0, 100000}; nanosleep(&delay, nullptr); }
int magnitude(int value) {
    return static_cast<int>(std::min<int64_t>(100000, value < 0 ? -static_cast<int64_t>(value) : value));
}
int64_t bounded(int64_t value) { return std::max<int64_t>(0, std::min(metricLimit, value)); }
void atomicMaximum(std::atomic<int64_t>& counter, int64_t value) {
    auto previous = counter.load(std::memory_order_relaxed);
    while (value > previous && !counter.compare_exchange_weak(previous, value,
            std::memory_order_relaxed)) {}
}
void atomicMinimumPositive(std::atomic<int64_t>& counter, int64_t value) {
    auto previous = counter.load(std::memory_order_relaxed);
    while ((previous == 0 || value < previous) && !counter.compare_exchange_weak(previous, value,
            std::memory_order_relaxed)) {}
}
template<size_t N> void erase(std::array<int16_t, N>& samples) {
    volatile int16_t* p = samples.data();
    for (size_t i = 0; i < N; ++i) p[i] = 0;
}

struct MMapSymbols {
    using Used = bool (*)(AAudioStream*);
    void* library = dlopen("libaaudio.so", RTLD_NOW | RTLD_LOCAL);
    ScopedMMapPolicy::Getter get = library
            ? reinterpret_cast<ScopedMMapPolicy::Getter>(dlsym(library, "AAudio_getMMapPolicy")) : nullptr;
    ScopedMMapPolicy::Setter set = library
            ? reinterpret_cast<ScopedMMapPolicy::Setter>(dlsym(library, "AAudio_setMMapPolicy")) : nullptr;
    Used used = library ? reinterpret_cast<Used>(dlsym(library, "AAudioStream_isMMapUsed")) : nullptr;
    ~MMapSymbols() { if (library) dlclose(library); }
};

AAudioStream* raw(const std::shared_ptr<oboe::AudioStream>& stream) {
    return stream && stream->getAudioApi() == oboe::AudioApi::AAudio
            ? static_cast<AAudioStream*>(stream->getUnderlyingStream()) : nullptr;
}
int route(const std::shared_ptr<oboe::AudioStream>& stream) {
    auto* s = raw(stream);
    return s ? std::max(0, AAudioStream_getDeviceId(s)) : 0;
}
struct Endpoint {
    int rate = 0, capacity = 0, buffer = 0, burst = 0, performance = 0,
            state = 0, device = 0, mmap = 0, xruns = -1, timestampError = 0, hardwareRate = 0;
    int64_t written = 0, read = 0, position = 0, timestampNs = 0;
    bool timestampSuccess = false;
};
Endpoint inspect(const std::shared_ptr<oboe::AudioStream>& stream, const MMapSymbols& symbols) {
    Endpoint e;
    auto* s = raw(stream);
    if (!s) return e;
    // Oboe caches the API 34 hardware rate when opening the stream, including
    // across FilterAudioStream. This accessor does not issue a platform query.
    e.hardwareRate = std::max(0, stream->getHardwareSampleRate());
    e.rate = AAudioStream_getSampleRate(s);
    e.capacity = AAudioStream_getBufferCapacityInFrames(s);
    e.buffer = AAudioStream_getBufferSizeInFrames(s);
    e.burst = AAudioStream_getFramesPerBurst(s);
    e.performance = AAudioStream_getPerformanceMode(s);
    e.state = AAudioStream_getState(s);
    e.device = std::max(0, AAudioStream_getDeviceId(s));
    e.mmap = symbols.used ? (symbols.used(s) ? 2 : 1) : 0;
    e.xruns = AAudioStream_getXRunCount(s);
    e.written = AAudioStream_getFramesWritten(s);
    e.read = AAudioStream_getFramesRead(s);
    const auto timestampResult = AAudioStream_getTimestamp(s, CLOCK_MONOTONIC,
            &e.position, &e.timestampNs);
    e.timestampSuccess = timestampResult == AAUDIO_OK;
    e.timestampError = magnitude(timestampResult);
    return e;
}

std::atomic<int> lastCode{0};
std::mutex evidenceMutex;
std::array<jlong, metricCount> emptyMetrics() {
    std::array<jlong, metricCount> result{};
    result[12] = result[13] = -1;
    result[36] = fridayDuplexQueueBudget;
    result[63] = 1;
    return result;
}
std::array<jlong, metricCount> lastEvidence = emptyMetrics();
void retain(const std::array<jlong, metricCount>& values, int code) {
    std::lock_guard<std::mutex> lock(evidenceMutex);
    lastEvidence = values;
    lastEvidence[14] = code;
    lastCode.store(code);
}

struct Duplex final : oboe::AudioStreamDataCallback, oboe::AudioStreamErrorCallback {
    std::shared_ptr<oboe::AudioStream> input, output;
    MMapSymbols symbols;
    std::mutex lifecycle, writer, metricsCache;
    PcmQueue queue; // One serialized JNI producer; one output callback consumer.
    std::atomic<bool> stopped{false}, ready{false};
    std::atomic<int> error{0};
    std::atomic<int64_t> requestedFrames{0}, submittedFrames{0}, firstMusic{-1}, outputMissing{0},
            lastAt{0}, maxGapUs{0}, callbackCount{0}, lastFrames{0}, minimumFrames{0}, maximumFrames{0},
            maximumBatchFrames{0}, maximumBatchCount{0}, inputReadFrames{0}, readWaitMaxUs{0};
    std::array<std::atomic<int64_t>, 101> readWaitHistogram{};
    // Aggregates only: no PCM or per-call log, allocation, platform query or new
    // lock is added to read completion or the output callback.
    std::atomic<int64_t> readCalls{0}, readLockWaitMaxUs{0}, readTotalMaxUs{0},
            readSuccesses{0}, lastDeliveryAt{0}, deliveryGapMaxUs{0},
            deliveryGapOver10ms{0}, deliveryGapOver20ms{0}, deliveryGapOver50ms{0},
            platformRefreshCount{0}, platformRefreshMaxUs{0}, platformRefreshLastUs{0},
            routeRefreshLockWaitMaxUs{0}, minimumReadFrames{0}, maximumReadFrames{0};
    // These two fields are accessed only by the one output callback thread.
    int64_t batchFrames = 0, batchCount = 0;
    int inputId = 0, expectedOutput = 0, channels = 2, inputRateMode = 0;
    bool policyAvailable = false, policyRestored = false, drainCompleted = false;
    int policyBefore = 0, policySetError = 0;
    int64_t drainedFrames = 0;
    bool finalSnapshot = false, closed = false;
    std::array<jlong, metricCount> finalMetrics{};
    // The cache lock covers only a fixed-size copy. Platform queries are made
    // before acquiring it, never from fast metrics or the render/capture workers.
    std::array<jlong, metricCount> cachedMetrics = emptyMetrics();
    bool cacheFinal = false;

    ~Duplex() {
        // Registry/JNI/callback shared leases have all ended before destruction.
        // Do not clear queue storage concurrently with an in-flight writer.
        volatile int16_t* p = queue.data;
        for (size_t i = 0; i < sizeof(queue.data) / sizeof(queue.data[0]); ++i) p[i] = 0;
    }

    int fail(int code) {
        int expected = 0;
        error.compare_exchange_strong(expected, code);
        stopped.store(true);
        ready.store(false);
        return -error.load();
    }

    oboe::DataCallbackResult onAudioReady(oboe::AudioStream*, void* destination, int32_t frames) override {
        auto* samples = static_cast<int16_t*>(destination);
        const auto time = clockNs(), previous = lastAt.exchange(time, std::memory_order_relaxed);
        const auto count = callbackCount.fetch_add(1, std::memory_order_relaxed);
        requestedFrames.fetch_add(frames, std::memory_order_relaxed);
        lastFrames.store(frames, std::memory_order_relaxed);
        if (count == 0 || frames < minimumFrames.load(std::memory_order_relaxed))
            minimumFrames.store(frames, std::memory_order_relaxed);
        maximumFrames.store(std::max<int64_t>(maximumFrames.load(std::memory_order_relaxed), frames),
                std::memory_order_relaxed);
        if (previous > 0) maxGapUs.store(std::max(maxGapUs.load(std::memory_order_relaxed),
                (time - previous) / 1000), std::memory_order_relaxed);
        if (previous > 0 && time - previous < 1000000) { batchFrames += frames; ++batchCount; }
        else { batchFrames = frames; batchCount = 1; }
        maximumBatchFrames.store(std::max(maximumBatchFrames.load(std::memory_order_relaxed), batchFrames),
                std::memory_order_relaxed);
        maximumBatchCount.store(std::max(maximumBatchCount.load(std::memory_order_relaxed), batchCount),
                std::memory_order_relaxed);
        if (frames <= 0 || frames > fridayDuplexQueueBudget || stopped.load() || error.load()) {
            if (frames > 0) std::memset(samples, 0, static_cast<size_t>(frames) * 2 * sizeof(int16_t));
            if (frames <= 0 || frames > fridayDuplexQueueBudget) fail(60006);
            return oboe::DataCallbackResult::Stop;
        }
        const auto absolute = submittedFrames.load(std::memory_order_relaxed);
        const int obtained = queue.pop(samples, frames);
        if (obtained > 0) { int64_t empty = -1; firstMusic.compare_exchange_strong(empty, absolute); }
        if (obtained < frames) {
            std::memset(samples + obtained * 2, 0,
                    static_cast<size_t>(frames - obtained) * 2 * sizeof(int16_t));
            if (firstMusic.load(std::memory_order_relaxed) >= 0)
                outputMissing.fetch_add(frames - obtained, std::memory_order_relaxed);
        }
        submittedFrames.fetch_add(frames, std::memory_order_relaxed);
        return oboe::DataCallbackResult::Continue;
    }

    std::array<jlong, metricCount> snapshotLocked(bool final) {
        if (finalSnapshot) return finalMetrics;
        const auto began = clockNs();
        const auto out = inspect(output, symbols), in = inspect(input, symbols);
        const auto now = clockNs();
        const auto queryUs = std::max<int64_t>(0, (now - began) / 1000);
        platformRefreshCount.fetch_add(1, std::memory_order_relaxed);
        atomicMaximum(platformRefreshMaxUs, queryUs);
        platformRefreshLastUs.store(queryUs, std::memory_order_relaxed);
        const auto outTime = estimateAudioTimestamp(out.timestampSuccess, false,
                out.position, out.timestampNs, now, out.written, out.rate);
        const auto inTime = estimateAudioTimestamp(in.timestampSuccess, true,
                in.position, in.timestampNs, now, in.read, in.rate);
        // lifecycle excludes input reads during this complete sampling window.
        // Pair these client counters with [45,46] and the raw rate [38]. Fast
        // metrics must not replace them with fresher values like legacy [27].
        const auto sampleReturned = inputReadFrames.load(std::memory_order_relaxed);
        const bool sampleStarted = ready.load() && !stopped.load() && !error.load()
                && drainCompleted && out.state == static_cast<int>(oboe::StreamState::Started)
                && in.state == static_cast<int>(oboe::StreamState::Started)
                && out.device == expectedOutput && in.device == inputId
                && in.rate > 0 && in.rate <= 384000
                && in.read >= 0 && in.written >= in.read && in.written <= metricLimit
                && sampleReturned >= 0 && sampleReturned <= metricLimit
                && drainedFrames >= 0 && drainedFrames <= metricLimit;
        int64_t total = 0, accumulated = 0, p95 = 0;
        for (auto& n : readWaitHistogram) total += n.load(std::memory_order_relaxed);
        if (total > 0) for (size_t i = 0; i < readWaitHistogram.size(); ++i) {
            accumulated += readWaitHistogram[i].load(std::memory_order_relaxed);
            if (accumulated * 100 >= total * 95) { p95 = (static_cast<int64_t>(i) + 1) * 1000; break; }
        }
        std::array<jlong, metricCount> values{{
            0, 0, out.device, in.device,
            fridayClientFrames(out.buffer, out.rate), fridayClientFrames(in.buffer, in.rate),
            fridayClientFrames(out.burst, out.rate), fridayClientFrames(in.burst, in.rate),
            out.performance, in.performance, 0, outputMissing.load(),
            out.xruns >= 0 ? out.xruns : -1, in.xruns >= 0 ? in.xruns : -1,
            error.load(), 0, fridayDuplexClientRate, fridayDuplexClientRate, 0, 0, 0,
            requestedFrames.load(), lastFrames.load(), maxGapUs.load(), 1, now / 1000000, 0,
            inputReadFrames.load(), drainedFrames, callbackCount.load(), minimumFrames.load(),
            maximumFrames.load(), maximumBatchFrames.load(), maximumBatchCount.load(),
            readWaitMaxUs.load(), p95, fridayDuplexQueueBudget,
            out.rate, in.rate, out.capacity, in.capacity, out.burst, in.burst,
            out.written, out.read, in.written, in.read, out.mmap, in.mmap,
            policyRestored ? 1 : 0, outTime.timestampAvailable ? 1 : 0, outTime.pendingFrames,
            inTime.timestampAvailable ? 1 : 0, inTime.pendingFrames,
            outTime.pendingAvailable ? 1 : 0, inTime.pendingAvailable ? 1 : 0, 0,
            policyAvailable ? 1 : 0, policyBefore, policySetError, drainCompleted ? 1 : 0,
            out.state, in.state, 1,
            sampleReturned, drainedFrames, began / 1000000, queryUs,
            in.timestampError, inTime.framePosition, inTime.ageUs, inTime.future ? 1 : 0,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, sampleStarted ? 1 : 0, 0, 0, 0,
            in.hardwareRate, in.hardwareRate > 0 && in.hardwareRate <= 384000 ? 1 : 0, inputRateMode,
        }};
        populateRealtime(values, now);
        if (final) { finalMetrics = values; finalSnapshot = true; }
        {
            std::lock_guard<std::mutex> lock(metricsCache);
            cachedMetrics = values;
            cacheFinal = final;
        }
        return values;
    }
    void populateRealtime(std::array<jlong, metricCount>& values, int64_t now) {
        // Preserve the existing production callback estimate for AV sync. It is
        // not a DAC timestamp. Raw platform telemetry below retains its original
        // observation time in [25], refreshed by low-frequency routeNative().
        const auto last = lastAt.load(), callbackSize = lastFrames.load();
        const auto submitted = submittedFrames.load(), first = firstMusic.load();
        const auto within = last > 0 ? std::min<int64_t>(callbackSize,
                std::max<int64_t>(0, now - last) * fridayDuplexClientRate / 1000000000LL) : 0;
        const auto presented = std::max<int64_t>(0, submitted - callbackSize + within - values[4]);
        values[0] = first < 0 ? 0 : std::max<int64_t>(0, std::min<int64_t>(queue.read.load(),
                presented - first - outputMissing.load()));
        values[1] = static_cast<int64_t>(queue.size()) + std::max<int64_t>(0, submitted - presented);
        values[11] = outputMissing.load(); values[14] = error.load();
        values[21] = requestedFrames.load(); values[22] = callbackSize; values[23] = maxGapUs.load();
        values[27] = inputReadFrames.load(); values[29] = callbackCount.load();
        values[30] = minimumFrames.load(); values[31] = maximumFrames.load();
        values[32] = maximumBatchFrames.load(); values[33] = maximumBatchCount.load();
        values[34] = readWaitMaxUs.load();
        int64_t total = 0, accumulated = 0;
        for (auto& n : readWaitHistogram) total += n.load(std::memory_order_relaxed);
        values[35] = 0;
        if (total > 0) for (size_t i = 0; i < readWaitHistogram.size(); ++i) {
            accumulated += readWaitHistogram[i].load(std::memory_order_relaxed);
            if (accumulated * 100 >= total * 95) { values[35] = (static_cast<int64_t>(i) + 1) * 1000; break; }
        }
        // Only timing aggregates are live. [64..71,84] belong to the cached
        // platform sampling window; a successful read must not alter that pair.
        values[72] = readCalls.load(); values[73] = readLockWaitMaxUs.load();
        values[74] = readTotalMaxUs.load(); values[75] = readSuccesses.load();
        values[76] = deliveryGapMaxUs.load(); values[77] = deliveryGapOver10ms.load();
        values[78] = deliveryGapOver20ms.load(); values[79] = deliveryGapOver50ms.load();
        values[80] = platformRefreshCount.load(); values[81] = platformRefreshMaxUs.load();
        values[82] = platformRefreshLastUs.load(); values[83] = routeRefreshLockWaitMaxUs.load();
        const auto delivered = lastDeliveryAt.load();
        values[85] = delivered > 0 ? std::max<int64_t>(0, now - delivered) / 1000 : 0;
        values[86] = minimumReadFrames.load(); values[87] = maximumReadFrames.load();
        for (size_t i = 0; i < values.size(); ++i) if (i != 12 && i != 13) values[i] = bounded(values[i]);
    }

    std::array<jlong, metricCount> fastSnapshot() {
        std::array<jlong, metricCount> values;
        {
            std::lock_guard<std::mutex> lock(metricsCache);
            values = cachedMetrics;
            if (cacheFinal) return values;
        }
        populateRealtime(values, clockNs());
        return values;
    }

    void stopLocked() {
        stopped.store(true);
        ready.store(false);
        snapshotLocked(true); // Before legacy stop catches read counters up.
        if (output) output->requestStop();
        if (input) input->requestStop();
    }
    void closeLocked() {
        if (closed) return;
        stopLocked();
        if (output) output->close();
        if (input) input->close();
        output.reset(); input.reset(); closed = true;
    }
    void close() {
        stopped.store(true);
        std::lock_guard<std::mutex> lock(lifecycle);
        closeLocked();
    }
    bool onError(oboe::AudioStream*, oboe::Result result) override {
        fail(magnitude(static_cast<int>(result)));
        std::lock_guard<std::mutex> lock(lifecycle);
        closeLocked();
        return true;
    }

    int start() {
        std::lock_guard<std::mutex> lock(lifecycle);
        if (stopped.load() || !input || !output) return fail(60010);
        if (ready.load()) return 0;
        const auto deadline = clockNs() + fridayDuplexStartupNs;
        auto result = input->requestStart();
        if (result == oboe::Result::OK) result = output->requestStart();
        if (result != oboe::Result::OK) return fail(magnitude(static_cast<int>(result)));
        bool verified = false;
        while (!stopped.load() && !error.load() && clockNs() < deadline) {
            const int outId = route(output), inId = route(input);
            if (outId > 0 && outId != expectedOutput) return fail(60002);
            if (inId > 0 && inId != inputId) return fail(60003);
            if (outId == expectedOutput && inId == inputId && callbackCount.load() > 0
                    && output->getState() == oboe::StreamState::Started
                    && input->getState() == oboe::StreamState::Started) { verified = true; break; }
            briefWait();
        }
        if (!verified) return fail(error.load() ? error.load() : 60001);
        auto* inputRaw = raw(input);
        const auto capacity = fridayClientFrames(AAudioStream_getBufferCapacityInFrames(inputRaw),
                AAudioStream_getSampleRate(inputRaw));
        FridayStartupDrain drain(clockNs(), deadline, fridayDrainFrameBudget(capacity));
        std::array<int16_t, 960> samples{};
        while (drain.mayRead(clockNs(), stopped.load())) {
            const int requested = static_cast<int>(std::min<int64_t>(240, drain.budget - drain.discarded));
            const auto read = input->read(samples.data(), requested, 0);
            if (!read) { erase(samples); return fail(magnitude(static_cast<int>(read.error()))); }
            if (read.value() < 0 || read.value() > requested) { erase(samples); return fail(60009); }
            drain.observe(read.value());
            drainedFrames = drain.discarded;
            erase(samples);
            if (!drain.complete && read.value() == 0) briefWait();
        }
        if (!drain.complete) return fail(error.load() ? error.load() : drain.observedInput ? 60005 : 60004);
        if (stopped.load() || error.load()) return fail(error.load() ? error.load() : 60010);
        if (route(output) != expectedOutput) return fail(60002);
        if (route(input) != inputId) return fail(60003);
        drainCompleted = true;
        ready.store(true);
        return 0;
    }
};

// Constructed before the lifecycle guard, so completion includes native lock
// wait, route checks, the bounded read, JNI copy and releasing that guard. The
// JNI shared owner lease outlives this observer even if close removes its token.
struct ReadObservation {
    Duplex& owner;
    const int64_t began = clockNs();
    int frames = 0;
    explicit ReadObservation(Duplex& value) : owner(value) {
        owner.readCalls.fetch_add(1, std::memory_order_relaxed);
    }
    void acquired() {
        atomicMaximum(owner.readLockWaitMaxUs, std::max<int64_t>(0, clockNs() - began) / 1000);
    }
    ~ReadObservation() {
        const auto now = clockNs();
        atomicMaximum(owner.readTotalMaxUs, std::max<int64_t>(0, now - began) / 1000);
        if (frames <= 0) return;
        owner.readSuccesses.fetch_add(1, std::memory_order_relaxed);
        atomicMinimumPositive(owner.minimumReadFrames, frames);
        atomicMaximum(owner.maximumReadFrames, frames);
        auto previous = owner.lastDeliveryAt.load(std::memory_order_relaxed);
        // Concurrent callers may finish out of order after dropping lifecycle.
        // Keep the delivery time monotonic instead of replacing it with an older
        // value. Production has one capture reader; this also keeps close races safe.
        while (now > previous && !owner.lastDeliveryAt.compare_exchange_weak(previous, now,
                std::memory_order_relaxed)) {}
        if (previous <= 0 || now <= previous) return;
        const auto gap = (now - previous) / 1000;
        atomicMaximum(owner.deliveryGapMaxUs, gap);
        if (gap > 10000) owner.deliveryGapOver10ms.fetch_add(1, std::memory_order_relaxed);
        if (gap > 20000) owner.deliveryGapOver20ms.fetch_add(1, std::memory_order_relaxed);
        if (gap > 50000) owner.deliveryGapOver50ms.fetch_add(1, std::memory_order_relaxed);
    }
};

AudioHandleRegistry<Duplex> handles;
}

extern "C" JNIEXPORT jlong JNICALL Java_local_friday_ktv_NativeDuplexIO_openNative(
        JNIEnv*, jclass, jint inputId, jint expectedOutput, jint channels, jint inputRateMode) {
    if (inputId <= 0 || expectedOutput <= 0 || (channels != 1 && channels != 2)
            || (inputRateMode != 0 && inputRateMode != 1)) {
        retain(emptyMetrics(), 50004); return 0;
    }
    auto e = std::make_shared<Duplex>();
    e->inputId = inputId; e->expectedOutput = expectedOutput; e->channels = channels;
    e->inputRateMode = inputRateMode;
    std::lock_guard<std::mutex> policyLock(fridayAudioOpenPolicyMutex());
    ScopedMMapPolicy policy(e->symbols.get, e->symbols.set, true);
    e->policyAvailable = policy.available; e->policyBefore = policy.before; e->policySetError = policy.setError;
    auto restore = [&]() { const bool ok = policy.restore(); e->policyRestored = policy.restored; return ok; };
    auto reject = [&](int code) -> jlong {
        if (!restore()) code = 50003;
        e->fail(code); e->close(); retain(e->finalMetrics, code); return 0;
    };
    if (!policy.ready) return reject(policy.available ? 50002 : 50001);
    oboe::AudioStreamBuilder out;
    out.setDirection(oboe::Direction::Output)->setAudioApi(oboe::AudioApi::AAudio)
        ->setPerformanceMode(oboe::PerformanceMode::LowLatency)->setSharingMode(oboe::SharingMode::Shared)
        ->setUsage(oboe::Usage::Game)->setContentType(oboe::ContentType::Music)
        ->setSampleRate(48000)->setChannelCount(2)->setFormat(oboe::AudioFormat::I16)
        ->setSampleRateConversionQuality(oboe::SampleRateConversionQuality::Medium)
        ->setDeviceId(0) // Follow system default; never force the builtin speaker route.
        ->setDataCallback(std::static_pointer_cast<oboe::AudioStreamDataCallback>(e))
        ->setErrorCallback(std::static_pointer_cast<oboe::AudioStreamErrorCallback>(e));
    auto result = out.openStream(e->output);
    if (result != oboe::Result::OK) return reject(10000 + magnitude(static_cast<int>(result)));
    const int outRoute = route(e->output);
    if (outRoute > 0 && outRoute != expectedOutput) return reject(20001);
    if (!raw(e->output)) return reject(20006);
    if (e->output->getSampleRate() != 48000 || AAudioStream_getSampleRate(raw(e->output)) != 48000)
        return reject(20002);
    if (e->output->getChannelCount() != 2) return reject(20003);
    if (e->output->getFormat() != oboe::AudioFormat::I16) return reject(20004);
    if (e->output->getPerformanceMode() != oboe::PerformanceMode::LowLatency
            && e->output->getPerformanceMode() != oboe::PerformanceMode::None) return reject(20005);
    if (!e->symbols.used || e->symbols.used(raw(e->output))) return reject(50005);
    oboe::AudioStreamBuilder in;
    in.setDirection(oboe::Direction::Input)->setAudioApi(oboe::AudioApi::AAudio)
        ->setPerformanceMode(oboe::PerformanceMode::LowLatency)->setSharingMode(oboe::SharingMode::Shared)
        ->setInputPreset(oboe::InputPreset::Unprocessed)->setSampleRate(48000)
        ->setChannelCount(channels)->setFormat(oboe::AudioFormat::I16)->setDeviceId(inputId)
        ->setSampleRateConversionQuality(inputRateMode == 1 ? oboe::SampleRateConversionQuality::None
                : oboe::SampleRateConversionQuality::Medium)
        ->setBufferCapacityInFrames(e->output->getBufferCapacityInFrames() * 2)
        ->setErrorCallback(std::static_pointer_cast<oboe::AudioStreamErrorCallback>(e));
    // No input data callback: retain the accepted pull/conversion path.
    result = in.openStream(e->input);
    if (result != oboe::Result::OK) return reject(30000 + magnitude(static_cast<int>(result)));
    const int inRoute = route(e->input);
    if (!raw(e->input)) return reject(40006);
    if (inRoute > 0 && inRoute != inputId) return reject(40001);
    // Strict diagnostic mode must not silently turn into a 96 kHz request or
    // a known framework 96->48 kHz conversion. Mode 0 preserves production.
    if (inputRateMode == 1 && AAudioStream_getSampleRate(raw(e->input)) != 48000)
        return reject(40007);
    if (inputRateMode == 1 && e->input->getHardwareSampleRate() > 0
            && e->input->getHardwareSampleRate() != 48000) return reject(40008);
    if (e->input->getSampleRate() != 48000) return reject(40002);
    if (e->input->getChannelCount() != channels) return reject(40003);
    if (e->input->getFormat() != oboe::AudioFormat::I16) return reject(40004);
    if (!e->symbols.used || e->symbols.used(raw(e->input))) return reject(50005);
    if (!restore()) return reject(50003);
    const auto id = handles.add(e);
    if (!id) return reject(50004);
    retain(e->snapshotLocked(false), 0);
    return id;
}

extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeDuplexIO_startNative(
        JNIEnv*, jclass, jlong handle) {
    auto e = handles.acquire(handle); if (!e) return -60010;
    const int result = e->start();
    if (result != 0) { e->close(); retain(e->finalMetrics, magnitude(result)); }
    else { std::lock_guard<std::mutex> lock(e->lifecycle); retain(e->snapshotLocked(false), 0); }
    return result;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeDuplexIO_readNative(
        JNIEnv* env, jclass, jlong handle, jshortArray array, jint frames) {
    auto e = handles.acquire(handle);
    if (!e || !array || frames < 1 || frames > 480 || env->GetArrayLength(array) < frames * e->channels)
        return -1;
    ReadObservation observation(*e);
    std::array<int16_t, 960> samples{};
    std::lock_guard<std::mutex> lock(e->lifecycle);
    observation.acquired();
    if (e->stopped.load() || e->error.load() || !e->input || !e->drainCompleted) return -1;
    if (route(e->input) != e->inputId) return e->fail(60003);
    if (route(e->output) != e->expectedOutput) return e->fail(60002);
    const auto started = clockNs();
    const auto read = fridayReadBounded(frames,
            [&](int offset, int remaining) -> FridayDuplexRead {
                const auto part = e->input->read(samples.data() + offset * e->channels, remaining, 0);
                return part ? FridayDuplexRead{part.value(), 0, false}
                            : FridayDuplexRead{0, magnitude(static_cast<int>(part.error())), false};
            }, clockNs, [&]() { return e->stopped.load() || e->error.load(); }, briefWait);
    const auto waited = std::max<int64_t>(0, (clockNs() - started) / 1000);
    e->readWaitMaxUs.store(std::max(e->readWaitMaxUs.load(), waited));
    e->readWaitHistogram[std::min<int64_t>(100, waited / 1000)].fetch_add(1);
    if (read.error) { erase(samples); return e->fail(read.error); }
    if (read.cancelled || e->stopped.load() || e->error.load()) { erase(samples); return -1; }
    const int count = read.frames * e->channels;
    if (count) env->SetShortArrayRegion(array, 0, count, samples.data());
    erase(samples);
    if (env->ExceptionCheck()) return e->fail(60011);
    e->inputReadFrames.fetch_add(read.frames);
    observation.frames = read.frames;
    return count;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeDuplexIO_writeNative(
        JNIEnv* env, jclass, jlong handle, jshortArray array, jint offset, jint samples) {
    auto e = handles.acquire(handle);
    if (!e || !array || samples < 2 || samples > 960 || samples % 2 || offset < 0
            || offset > env->GetArrayLength(array) - samples) return -1;
    std::lock_guard<std::mutex> writer(e->writer);
    if (!e->ready.load()) return -1; // Preflight output remains zero until routing and drain pass.
    const auto deadline = clockNs() + 500000000LL;
    while (!e->stopped.load() && !e->error.load() && e->queue.size() >= fridayDuplexQueueBudget
            && clockNs() < deadline) briefWait();
    if (e->stopped.load() || e->error.load()) return -1;
    if (clockNs() >= deadline) return e->fail(60008);
    const int frames = std::min(samples / 2,
            fridayDuplexQueueBudget - static_cast<int>(e->queue.size()));
    std::array<int16_t, 960> data{};
    env->GetShortArrayRegion(array, offset, frames * 2, data.data());
    if (env->ExceptionCheck()) { erase(data); return e->fail(60011); }
    const int written = e->stopped.load() ? 0 : e->queue.push(data.data(), frames);
    erase(data);
    return written > 0 ? written * 2 : -1;
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeDuplexIO_metricsNative(
        JNIEnv* env, jclass, jlong handle, jlongArray array) {
    if (!array || env->GetArrayLength(array) < static_cast<jsize>(metricCount)) return;
    auto e = handles.acquire(handle); if (!e) return;
    const auto values = e->fastSnapshot();
    env->SetLongArrayRegion(array, 0, metricCount, values.data());
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeDuplexIO_routeNative(
        JNIEnv*, jclass, jlong handle) {
    auto e = handles.acquire(handle); if (!e) return 0;
    const auto began = clockNs();
    std::lock_guard<std::mutex> lock(e->lifecycle);
    atomicMaximum(e->routeRefreshLockWaitMaxUs, std::max<int64_t>(0, clockNs() - began) / 1000);
    const int current = route(e->output);
    if (e->ready.load()) {
        if (current != e->expectedOutput) e->fail(60002);
        else if (route(e->input) != e->inputId) e->fail(60003);
    }
    e->snapshotLocked(false);
    return current;
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeDuplexIO_stopNative(
        JNIEnv*, jclass, jlong handle) {
    auto e = handles.acquire(handle); if (!e) return;
    e->stopped.store(true);
    std::lock_guard<std::mutex> lock(e->lifecycle); e->stopLocked();
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeDuplexIO_closeNative(
        JNIEnv*, jclass, jlong handle) {
    auto e = handles.remove(handle); if (e) e->close();
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeDuplexIO_openCodeNative(JNIEnv*, jclass) {
    return lastCode.load();
}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_ktv_NativeDuplexIO_openMetricsNative(
        JNIEnv* env, jclass) {
    std::array<jlong, metricCount> values;
    { std::lock_guard<std::mutex> lock(evidenceMutex); values = lastEvidence; }
    auto result = env->NewLongArray(metricCount);
    if (result) env->SetLongArrayRegion(result, 0, metricCount, values.data());
    return result;
}
