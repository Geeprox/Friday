#include <oboe/Oboe.h>
#include <oboe/FullDuplexStream.h>
#include <aaudio/AAudio.h>
#include <jni.h>
#include <dlfcn.h>
#include <atomic>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <memory>
#include <mutex>
#include <time.h>
#include "AudioDiagnosticPolicy.h"
#include "SharedAudioOpenPolicy.h"
#include "AudioMonitorTelemetry.h"
#include "DirectVoiceMonitor.h"

namespace {
constexpr size_t metricCount = 97;
constexpr int64_t metricLimit = 1000000000000LL;
static_assert(std::atomic<int64_t>::is_always_lock_free,
        "Diagnostic callback counters must not use lock-based atomics");
std::atomic<int> openCode{0};
std::mutex openEvidenceMutex;
std::array<jlong, metricCount> openEvidence{};

void retainOpenEvidence(const std::array<jlong, metricCount>& values, int code) {
    std::lock_guard<std::mutex> guard(openEvidenceMutex);
    openEvidence = values;
    openEvidence[7] = code;
    openCode.store(code);
}

int64_t clockNs() {
    timespec t{};
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000000000LL + t.tv_nsec;
}
int64_t bounded(int64_t value) {
    return std::max<int64_t>(0, std::min(metricLimit, value));
}
int errorMagnitude(int value) {
    return static_cast<int>(std::min<int64_t>(100000,
            value < 0 ? -static_cast<int64_t>(value) : value));
}

// These are the same diagnostic symbols used by OboeExtensions. Resolve them
// explicitly so "symbol unavailable" never becomes a false MMAP=false result,
// and so the exact process policy can be restored rather than a bool approximation.
struct MMapSymbols {
    using Used = bool (*)(AAudioStream*);
    void* library = nullptr;
    ScopedMMapPolicy::Getter get = nullptr;
    ScopedMMapPolicy::Setter set = nullptr;
    Used used = nullptr;
    MMapSymbols() {
        library = dlopen("libaaudio.so", RTLD_NOW | RTLD_LOCAL);
        if (library) {
            get = reinterpret_cast<ScopedMMapPolicy::Getter>(dlsym(library, "AAudio_getMMapPolicy"));
            set = reinterpret_cast<ScopedMMapPolicy::Setter>(dlsym(library, "AAudio_setMMapPolicy"));
            used = reinterpret_cast<Used>(dlsym(library, "AAudioStream_isMMapUsed"));
        }
    }
    ~MMapSymbols() { if (library) dlclose(library); }
};

AAudioStream* rawStream(const std::shared_ptr<oboe::AudioStream>& stream) {
    return stream && stream->getAudioApi() == oboe::AudioApi::AAudio
            ? static_cast<AAudioStream*>(stream->getUnderlyingStream()) : nullptr;
}
int routedDevice(const std::shared_ptr<oboe::AudioStream>& stream) {
    auto* raw = rawStream(stream);
    // Oboe 1.11 caches device IDs on old Android versions without its newer
    // routing callback API. Read AAudio itself while our lifecycle lock owns it.
    return raw ? std::max(0, AAudioStream_getDeviceId(raw)) : 0;
}

struct StreamSnapshot {
    int api = 0, mmap = 0, sharing = 0, state = 0, route = 0;
    int capacity = 0, burst = 0, timestampError = 0, rate = 0, xruns = -1;
    int64_t written = 0, read = 0, position = 0, time = 0;
    bool timestampSuccess = false;
};

StreamSnapshot inspectStream(const std::shared_ptr<oboe::AudioStream>& stream,
                             const MMapSymbols& symbols) {
    StreamSnapshot s;
    auto* raw = rawStream(stream);
    if (!raw) return s;
    s.api = static_cast<int>(stream->getAudioApi());
    s.mmap = symbols.used ? (symbols.used(raw) ? 2 : 1) : 0;
    s.sharing = static_cast<int>(AAudioStream_getSharingMode(raw));
    s.state = static_cast<int>(AAudioStream_getState(raw));
    s.route = std::max(0, AAudioStream_getDeviceId(raw));
    s.capacity = std::max(0, AAudioStream_getBufferCapacityInFrames(raw));
    s.burst = std::max(0, AAudioStream_getFramesPerBurst(raw));
    s.rate = AAudioStream_getSampleRate(raw);
    s.written = AAudioStream_getFramesWritten(raw);
    s.read = AAudioStream_getFramesRead(raw);
    s.xruns = AAudioStream_getXRunCount(raw);
    const auto result = AAudioStream_getTimestamp(raw, CLOCK_MONOTONIC, &s.position, &s.time);
    s.timestampError = errorMagnitude(result);
    s.timestampSuccess = result == AAUDIO_OK;
    return s;
}

struct Monitor final : oboe::FullDuplexStream, oboe::AudioStreamErrorCallback {
    std::shared_ptr<oboe::AudioStream> input, output;
    std::mutex lifecycle;
    MMapSymbols symbols;
    std::atomic<bool> cancelled{false}, armed{false}, expired{false};
    std::atomic<int64_t> deadline{0}, readFrames{0}, outputFrames{0}, voiceFrames{0},
            readyFrames{0}, missing{0}, peak{0}, inputPeak{0}, gapMax{0}, lastAt{0}, callbacks{0};
    std::atomic<int64_t> steadyAt{0}, startupDiscarded{0}, steadyInput{0}, steadyOutput{0},
            steadyMissing{0}, steadyGapMax{0}, steadyShortReads{0}, inputSamples{0},
            inputFullScale{0}, inputFullScaleRunMax{0}, audibleFrames{0}, limitedFrames{0},
            limitedRunMax{0}, audibleInputMonoAbs{0}, outputAbs{0}, legacyWouldLimit{0};
    // Callback-owned state; only the published aggregate atomics are read elsewhere.
    int64_t steadyLastAt = 0, inputFullScaleRun = 0, limitedRun = 0;
    // Accessed only under lifecycle, never from an audio callback.
    AudioMonitorPlatformStats platformStats;
    std::atomic<int> error{0}, errorStream{0};
    int inputId = 0, outputId = 0, channels = 2, mode = 0;
    bool quiet = false, outputOnly = false, policyAvailable = false, policyRestored = false;
    int policyBefore = 0, policySetError = 0;
    int64_t startedAt = 0, config[4]{};
    std::array<jlong, metricCount> snapshot{};
    bool finalSnapshot = false;

    bool inSteadyWindow(int64_t time) const {
        return audioMonitorSteadyWindow(startedAt, time, deadline.load(), armed.load(),
                cancelled.load(), expired.load(), error.load());
    }

    void observeSteady(int64_t time, int inputCount, int outputCount) {
        if (steadyAt.load(std::memory_order_relaxed) == 0) {
            startupDiscarded.store(std::max<int64_t>(0,
                    readFrames.load(std::memory_order_relaxed)
                            - readyFrames.load(std::memory_order_relaxed)), std::memory_order_relaxed);
            steadyAt.store(time, std::memory_order_release);
        }
        if (steadyLastAt > 0) steadyGapMax.store(std::max(
                steadyGapMax.load(std::memory_order_relaxed), (time - steadyLastAt) / 1000),
                std::memory_order_relaxed);
        steadyLastAt = time;
        steadyInput.fetch_add(inputCount, std::memory_order_relaxed);
        steadyOutput.fetch_add(outputCount, std::memory_order_relaxed);
        if (!outputOnly && inputCount < outputCount) {
            steadyMissing.fetch_add(outputCount - inputCount, std::memory_order_relaxed);
            steadyShortReads.fetch_add(1, std::memory_order_relaxed);
        }
    }

    oboe::ResultWithValue<int32_t> readInput(int32_t frames) override {
        auto result = oboe::FullDuplexStream::readInput(frames);
        if (result && result.value() > 0) readFrames.fetch_add(result.value());
        else if (!result && readyFrames.load() > 0) {
            errorStream.store(1);
            error.store(errorMagnitude(static_cast<int>(result.error())));
        }
        return result;
    }

    oboe::DataCallbackResult onAudioReady(oboe::AudioStream* stream, void* data,
                                         int32_t frames) override {
        const auto now = clockNs(), previous = lastAt.exchange(now);
        callbacks.fetch_add(1);
        outputFrames.fetch_add(frames);
        if (previous > 0) gapMax.store(std::max(gapMax.load(), (now - previous) / 1000));
        if (cancelled.load() || error.load() || now >= deadline.load()) {
            std::memset(data, 0, static_cast<size_t>(frames) * 2 * sizeof(int16_t));
            expired.store(now >= deadline.load());
            return oboe::DataCallbackResult::Stop;
        }
        if (outputOnly) {
            if (inSteadyWindow(now)) observeSteady(now, 0, frames);
            std::memset(data, 0, static_cast<size_t>(frames) * 2 * sizeof(int16_t));
            return oboe::DataCallbackResult::Continue;
        }
        auto result = oboe::FullDuplexStream::onAudioReady(stream, data, frames);
        if (result == oboe::DataCallbackResult::Stop && !cancelled.load() && !error.load()) {
            errorStream.store(1);
            error.store(1002);
        }
        return result;
    }

    oboe::DataCallbackResult onBothStreamsReady(const void* data, int inputCount,
                                               void* destination, int outputCount) override {
        auto* out = static_cast<int16_t*>(destination);
        auto* in = static_cast<const int16_t*>(data);
        std::memset(out, 0, static_cast<size_t>(outputCount) * 2 * sizeof(int16_t));
        const int n = std::min(inputCount, outputCount);
        readyFrames.fetch_add(inputCount);
        const auto time = clockNs();
        const bool allowed = directVoiceMayPlay(armed.load(), cancelled.load(), time, deadline.load());
        const bool steady = inSteadyWindow(time);
        if (steady) observeSteady(time, inputCount, outputCount);
        if (allowed) {
            voiceFrames.fetch_add(n);
            missing.fetch_add(outputCount - n);
        }
        int largestInput = 0, largestOutput = 0;
        int64_t fullScale = 0, maxFullScaleRun = 0, limited = 0, maxLimitedRun = 0,
                monoAbs = 0, renderedAbs = 0, oldLimited = 0;
        for (int i = 0; i < n; i++) {
            const auto left = in[i * channels], right = in[i * channels + channels - 1];
            largestInput = std::max(largestInput,
                    std::max(std::abs(static_cast<int>(left)), std::abs(static_cast<int>(right))));
            if (steady) {
                const bool leftFull = left == INT16_MIN || left == INT16_MAX;
                const bool rightFull = channels == 2 && (right == INT16_MIN || right == INT16_MAX);
                fullScale += (leftFull ? 1 : 0) + (rightFull ? 1 : 0);
                inputFullScaleRun = leftFull || rightFull ? inputFullScaleRun + 1 : 0;
                maxFullScaleRun = std::max(maxFullScaleRun, inputFullScaleRun);
            }
            if (allowed && !quiet) {
                const auto value = directVoiceSample(left, right);
                out[i * 2] = out[i * 2 + 1] = value;
                largestOutput = std::max(largestOutput, std::abs(static_cast<int>(value)));
                if (steady) {
                    const int mono = directVoiceMono(left, right), scaled = directVoiceUnclamped(mono);
                    const bool wasLimited = scaled > 2621 || scaled < -2621;
                    limited += wasLimited ? 1 : 0;
                    limitedRun = wasLimited ? limitedRun + 1 : 0;
                    maxLimitedRun = std::max(maxLimitedRun, limitedRun);
                    oldLimited += mono / 10 > 2621 || mono / 10 < -2621 ? 1 : 0;
                    monoAbs += std::abs(mono);
                    renderedAbs += std::abs(static_cast<int>(value));
                }
            }
        }
        if (steady) {
            inputSamples.fetch_add(static_cast<int64_t>(n) * channels, std::memory_order_relaxed);
            inputFullScale.fetch_add(fullScale, std::memory_order_relaxed);
            inputFullScaleRunMax.store(std::max(inputFullScaleRunMax.load(std::memory_order_relaxed),
                    maxFullScaleRun), std::memory_order_relaxed);
            if (allowed && !quiet) {
                audibleFrames.fetch_add(n, std::memory_order_relaxed);
                limitedFrames.fetch_add(limited, std::memory_order_relaxed);
                limitedRunMax.store(std::max(limitedRunMax.load(std::memory_order_relaxed),
                        maxLimitedRun), std::memory_order_relaxed);
                audibleInputMonoAbs.fetch_add(monoAbs, std::memory_order_relaxed);
                outputAbs.fetch_add(renderedAbs, std::memory_order_relaxed);
                legacyWouldLimit.fetch_add(oldLimited, std::memory_order_relaxed);
            }
        }
        if (!steady || n < outputCount) inputFullScaleRun = limitedRun = 0;
        inputPeak.store(std::max(inputPeak.load(), static_cast<int64_t>(largestInput)));
        peak.store(std::max(peak.load(), static_cast<int64_t>(largestOutput)));
        return oboe::DataCallbackResult::Continue;
    }

    // All platform getters, timing estimates, and final retention happen off the
    // real-time thread while the same lock excludes stream close.
    void captureSnapshotLocked(bool final) {
        if (finalSnapshot) return;
        const auto out = inspectStream(output, symbols);
        const auto in = inspectStream(input, symbols);
        const auto now = clockNs();
        const auto outTiming = estimateAudioTimestamp(out.timestampSuccess, false,
                out.position, out.time, now, out.written, out.rate);
        const auto inTiming = estimateAudioTimestamp(in.timestampSuccess, true,
                in.position, in.time, now, in.read, in.rate);
        // A stopped legacy output catches its read counter up to written. Only
        // retain >=1 s observations of the actually running, verified routes.
        // These are platform estimates, never microphone-to-speaker latency.
        if (inSteadyWindow(now) && steadyAt.load(std::memory_order_acquire) > 0
                && out.state == AAUDIO_STREAM_STATE_STARTED && out.route == outputId
                && (outputOnly || (in.state == AAUDIO_STREAM_STATE_STARTED && in.route == inputId))) {
            const AudioMonitorRawCounters outCounters{out.written, out.read, out.rate, out.xruns};
            const AudioMonitorRawCounters inCounters{in.written, in.read, in.rate, in.xruns};
            platformStats.add(now, outCounters, outputOnly ? nullptr : &inCounters,
                    outTiming, inTiming);
        }
        const auto steadyStart = steadyAt.load(std::memory_order_acquire);
        snapshot = {{
            readFrames.load(), outputFrames.load(), voiceFrames.load(), 0,
            std::max<int64_t>(0, readFrames.load() - readyFrames.load()), missing.load(),
            peak.load(), error.load(), config[0], config[1], config[2], config[3],
            errorStream.load(), quiet ? 1 : 0, gapMax.load(), callbacks.load(),
            expired.load() ? 1 : 0, inputPeak.load(), outputOnly ? 1 : 0,
            mode, out.api, in.api, out.mmap, in.mmap, out.sharing, in.sharing,
            out.state, in.state, out.route, in.route, out.capacity, in.capacity,
            out.burst, in.burst, out.written, out.read, in.written, in.read,
            outTiming.timestampAvailable ? 1 : 0, out.timestampError,
            outTiming.framePosition, outTiming.ageUs, outTiming.pendingFrames,
            inTiming.timestampAvailable ? 1 : 0, in.timestampError,
            inTiming.framePosition, inTiming.ageUs, inTiming.pendingFrames,
            startedAt > 0 ? std::min<int64_t>(20000, (now - startedAt) / 1000000) : 0,
            final ? 1 : 0, lastAt.load() > 0 ? (now - lastAt.load()) / 1000 : 0,
            policyAvailable ? 1 : 0, policyBefore, policySetError, policyRestored ? 1 : 0,
            outTiming.pendingAvailable ? 1 : 0, inTiming.pendingAvailable ? 1 : 0,
            outTiming.future ? 1 : 0, inTiming.future ? 1 : 0,
            out.rate, in.rate,
            steadyStart > 0 ? (steadyStart - startedAt) / 1000000 : 0,
            startupDiscarded.load(), steadyInput.load(), steadyOutput.load(),
            steadyMissing.load(), steadyGapMax.load(), steadyShortReads.load(),
            inputSamples.load(), inputFullScale.load(), inputFullScaleRunMax.load(),
            audibleFrames.load(), limitedFrames.load(), limitedRunMax.load(),
            audibleInputMonoAbs.load(), outputAbs.load(), platformStats.count,
            platformStats.outputPending.count, platformStats.outputPending.minimum,
            platformStats.outputPending.maximum, platformStats.outputPending.last,
            platformStats.inputPending.count, platformStats.inputPending.minimum,
            platformStats.inputPending.maximum, platformStats.inputPending.last,
            platformStats.outputCounter.minimum, platformStats.outputCounter.maximum,
            platformStats.inputCounter.minimum, platformStats.inputCounter.maximum,
            platformStats.outputXruns.available() ? 1 : 0, platformStats.outputXruns.delta(),
            platformStats.inputXruns.available() ? 1 : 0, platformStats.inputXruns.delta(),
            platformStats.clockSpanMs(), platformStats.inputProduced(), platformStats.outputPresented(),
            legacyWouldLimit.load(),
        }};
        for (auto& value : snapshot) value = bounded(value);
        finalSnapshot = final;
    }

    void closeStreamsLocked() {
        cancelled.store(true);
        captureSnapshotLocked(true); // Retain before requestStop()/close() erase evidence.
        if (output) { output->requestStop(); output->close(); }
        if (input) { input->requestStop(); input->close(); }
        std::shared_ptr<oboe::AudioStream> empty;
        setSharedOutputStream(empty);
        setSharedInputStream(empty);
        output.reset();
        input.reset();
    }

    void closeStreams() {
        std::lock_guard<std::mutex> guard(lifecycle);
        closeStreamsLocked();
    }

    bool onError(oboe::AudioStream* stream, oboe::Result result) override {
        // Oboe invokes this on its own error worker, not the real-time callback.
        // Its shared callback references keep this owner alive through cleanup.
        std::lock_guard<std::mutex> guard(lifecycle);
        errorStream.store(stream == input.get() ? 1 : 2);
        error.store(errorMagnitude(static_cast<int>(result)));
        closeStreamsLocked();
        return true;
    }
};

struct Handle { std::shared_ptr<Monitor> engine; };
Handle* owner(jlong handle) { return reinterpret_cast<Handle*>(handle); }
}

extern "C" JNIEXPORT jlong JNICALL Java_local_friday_ktv_NativeVoiceMonitor_openNative(
        JNIEnv*, jclass, jint inputId, jint outputId, jint channels, jboolean quiet,
        jboolean outputOnly, jint mode) {
    if ((!outputOnly && inputId <= 0) || outputId <= 0 || (channels != 1 && channels != 2)
            || (outputOnly && !quiet) || !validAudioDiagnosticMode(mode)) {
        std::array<jlong, metricCount> invalid{};
        invalid[13] = quiet ? 1 : 0;
        invalid[18] = outputOnly ? 1 : 0;
        invalid[19] = validAudioDiagnosticMode(mode) ? mode : 0;
        invalid[49] = 1;
        retainOpenEvidence(invalid, 50004);
        return 0;
    }
    auto e = std::make_shared<Monitor>();
    e->inputId = inputId;
    e->outputId = outputId;
    e->channels = channels;
    e->quiet = quiet;
    e->outputOnly = outputOnly;
    e->mode = mode;

    // AAudio's policy applies at open time only. Restore before any stream starts.
    std::lock_guard<std::mutex> policyLock(fridayAudioOpenPolicyMutex());
    ScopedMMapPolicy policy(e->symbols.get, e->symbols.set,
            mode == static_cast<int>(AudioDiagnosticMode::SharedWithoutMMap));
    e->policyAvailable = policy.available;
    e->policyBefore = policy.before;
    e->policySetError = policy.setError;
    auto finishPolicy = [&]() {
        const bool restored = policy.restore();
        e->policyRestored = policy.restored;
        return restored;
    };
    auto reject = [&](int code) -> jlong {
        if (!finishPolicy()) code = 50003;
        e->error.store(code);
        e->errorStream.store(code >= 50000 ? 0 : code >= 30000 ? 1 : code >= 10000 ? 2 : 0);
        e->closeStreams();
        retainOpenEvidence(e->snapshot, code);
        return 0;
    };
    if (!policy.ready) return reject(policy.available ? 50002 : 50001);

    oboe::AudioStreamBuilder out;
    out.setDirection(oboe::Direction::Output)->setAudioApi(oboe::AudioApi::AAudio)
        ->setPerformanceMode(oboe::PerformanceMode::LowLatency)
        ->setSharingMode(mode == static_cast<int>(AudioDiagnosticMode::Exclusive)
                ? oboe::SharingMode::Exclusive : oboe::SharingMode::Shared)
        ->setUsage(oboe::Usage::Game)->setContentType(oboe::ContentType::Music)
        ->setSampleRate(48000)->setChannelCount(2)->setFormat(oboe::AudioFormat::I16)
        ->setDeviceId(outputId)
        ->setDataCallback(std::static_pointer_cast<oboe::AudioStreamDataCallback>(e))
        ->setErrorCallback(std::static_pointer_cast<oboe::AudioStreamErrorCallback>(e));
    auto result = out.openStream(e->output);
    if (result != oboe::Result::OK) return reject(10000 + errorMagnitude(static_cast<int>(result)));
    e->config[0] = static_cast<int>(e->output->getPerformanceMode());
    e->config[2] = e->output->getBufferSizeInFrames();
    const int route = routedDevice(e->output);
    // Legacy AAudio may not expose its actual route until after requestStart.
    // A missing route is deferred to the bounded pre-arm check, never accepted
    // as evidence of the selected device. A different positive route is rejected.
    if (route > 0 && route != outputId) return reject(20001);
    if (e->output->getSampleRate() != 48000) return reject(20002);
    if (e->output->getChannelCount() != 2) return reject(20003);
    if (e->output->getFormat() != oboe::AudioFormat::I16) return reject(20004);
    const auto performance = e->output->getPerformanceMode();
    if (performance != oboe::PerformanceMode::LowLatency
            && !(mode == static_cast<int>(AudioDiagnosticMode::SharedWithoutMMap)
                 && performance == oboe::PerformanceMode::None)) return reject(20005);
    if (mode == static_cast<int>(AudioDiagnosticMode::SharedWithoutMMap)
            && (!e->symbols.used || e->symbols.used(rawStream(e->output)))) return reject(50005);

    if (!outputOnly) {
        oboe::AudioStreamBuilder in;
        in.setDirection(oboe::Direction::Input)->setAudioApi(oboe::AudioApi::AAudio)
            ->setPerformanceMode(oboe::PerformanceMode::LowLatency)->setSharingMode(oboe::SharingMode::Shared)
            ->setInputPreset(oboe::InputPreset::Unprocessed)->setSampleRate(48000)
            ->setChannelCount(channels)->setFormat(oboe::AudioFormat::I16)->setDeviceId(inputId)
            ->setBufferCapacityInFrames(e->output->getBufferCapacityInFrames() * 2);
        result = in.openStream(e->input);
        if (result != oboe::Result::OK) return reject(30000 + errorMagnitude(static_cast<int>(result)));
        e->config[1] = static_cast<int>(e->input->getPerformanceMode());
        e->config[3] = e->input->getBufferSizeInFrames();
        const int inputRoute = routedDevice(e->input);
        if (inputRoute > 0 && inputRoute != inputId) return reject(40001);
        if (e->input->getSampleRate() != 48000) return reject(40002);
        if (e->input->getChannelCount() != channels) return reject(40003);
        if (e->input->getFormat() != oboe::AudioFormat::I16) return reject(40004);
        if (mode == static_cast<int>(AudioDiagnosticMode::SharedWithoutMMap)
                && (!e->symbols.used || e->symbols.used(rawStream(e->input)))) return reject(50005);
        e->setSharedInputStream(e->input);
        e->setSharedOutputStream(e->output);
        e->setNumInputBurstsCushion(1);
        // Keep Oboe's default zero threshold: child availability omits converted
        // frames already prefetched by FilterAudioStream. read() still has timeout 0.
    }
    if (!finishPolicy()) return reject(50003);
    e->captureSnapshotLocked(false);
    retainOpenEvidence(e->snapshot, 0);
    auto* h = new Handle;
    h->engine = std::move(e);
    return reinterpret_cast<jlong>(h);
}

extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeVoiceMonitor_openCodeNative(JNIEnv*, jclass) {
    return openCode.load();
}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_ktv_NativeVoiceMonitor_openMetricsNative(
        JNIEnv* env, jclass) {
    std::array<jlong, metricCount> values;
    {
        std::lock_guard<std::mutex> guard(openEvidenceMutex);
        values = openEvidence;
    }
    auto result = env->NewLongArray(metricCount);
    if (result) env->SetLongArrayRegion(result, 0, metricCount, values.data());
    return result;
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeVoiceMonitor_startNative(
        JNIEnv*, jclass, jlong handle) {
    auto e = owner(handle)->engine;
    std::lock_guard<std::mutex> guard(e->lifecycle);
    if (e->cancelled.load() || !e->output) return -895;
    e->startedAt = clockNs();
    e->deadline.store(e->startedAt + 20000000000LL);
    oboe::Result result = oboe::Result::OK;
    if (!e->outputOnly) {
        // FullDuplexStream initializes its preallocated input buffer and starts
        // input first. Its aggregate start result does not identify a failed end.
        result = e->start();
    } else result = e->output->requestStart();
    if (result != oboe::Result::OK) {
        e->errorStream.store(e->outputOnly ? 2 : 0);
        e->error.store(errorMagnitude(static_cast<int>(result)));
        e->closeStreamsLocked();
    }
    return static_cast<int>(result);
}
extern "C" JNIEXPORT jint JNICALL Java_local_friday_ktv_NativeVoiceMonitor_routeNative(
        JNIEnv*, jclass, jlong handle) {
    auto e = owner(handle)->engine;
    std::lock_guard<std::mutex> guard(e->lifecycle);
    return routedDevice(e->output);
}
extern "C" JNIEXPORT jboolean JNICALL Java_local_friday_ktv_NativeVoiceMonitor_armNative(
        JNIEnv*, jclass, jlong handle, jint output) {
    auto e = owner(handle)->engine;
    std::lock_guard<std::mutex> guard(e->lifecycle);
    const auto now = clockNs();
    if (e->cancelled.load() || e->error.load() || !e->output || e->outputId != output
            || routedDevice(e->output) != output || now >= e->deadline.load()
            || (!e->outputOnly && routedDevice(e->input) != e->inputId)) return false;
    e->deadline.store(std::min<int64_t>(e->deadline.load(), now + 15000000000LL));
    e->armed.store(true);
    return true;
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeVoiceMonitor_cancelNative(
        JNIEnv*, jclass, jlong handle) {
    owner(handle)->engine->cancelled.store(true);
}
extern "C" JNIEXPORT jlongArray JNICALL Java_local_friday_ktv_NativeVoiceMonitor_metricsNative(
        JNIEnv* env, jclass, jlong handle) {
    auto e = owner(handle)->engine;
    std::array<jlong, metricCount> values;
    {
        std::lock_guard<std::mutex> guard(e->lifecycle);
        e->captureSnapshotLocked(false);
        values = e->snapshot;
    }
    auto result = env->NewLongArray(metricCount);
    if (result) env->SetLongArrayRegion(result, 0, metricCount, values.data());
    return result;
}
extern "C" JNIEXPORT void JNICALL Java_local_friday_ktv_NativeVoiceMonitor_closeNative(
        JNIEnv*, jclass, jlong handle) {
    auto* h = owner(handle);
    h->engine->closeStreams();
    delete h;
}
