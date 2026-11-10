#pragma once
#include <aaudio/AAudio.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>

namespace oboe {
enum class AudioApi { AAudio = 2 };
enum class Direction { Output, Input };
enum class PerformanceMode { None = 10, LowLatency = 12 };
enum class SharingMode { Shared = 1 };
enum class Usage { Game };
enum class ContentType { Music };
enum class InputPreset { Unprocessed };
enum class AudioFormat { I16 = 1 };
enum class SampleRateConversionQuality { None, Medium };
enum class StreamState { Started = 4 };
enum class Result { OK = 0, ErrorDisconnected = -899, ErrorClosed = -869 };
enum class DataCallbackResult { Continue, Stop };
template<class T> struct ResultWithValue {
    T number = 0;
    Result code = Result::OK;
    ResultWithValue(T n) : number(n) {}
    ResultWithValue(Result e) : code(e) {}
    explicit operator bool() const { return code == Result::OK; }
    T value() const { return number; }
    Result error() const { return code; }
};
class AudioStream;
struct AudioStreamDataCallback {
    virtual ~AudioStreamDataCallback() = default;
    virtual DataCallbackResult onAudioReady(AudioStream*, void*, int32_t) = 0;
};
struct AudioStreamErrorCallback {
    virtual ~AudioStreamErrorCallback() = default;
    virtual bool onError(AudioStream*, Result) = 0;
};
inline std::atomic<int> fakeStreamsAlive{0}, fakeOutputDevice{2}, fakeInputDevice{9};
inline std::atomic<int> fakeOutputCallbackFrames{960}, fakeInputStartupFrames{480};
inline std::atomic<int> fakeStrictInputRawRate{48000}, fakeStrictInputHardwareRate{48000},
        fakeStrictInputPerformance{12}, fakeInputOpenCount{0};
class AudioStream {
public:
    AAudioStream native;
    Direction direction = Direction::Output;
    int channels = 2, startupFrames = 0, clientRate = 48000, hardwareRate = 48000;
    std::shared_ptr<AudioStreamDataCallback> dataCallback;
    std::shared_ptr<AudioStreamErrorCallback> errorCallback;
    std::function<ResultWithValue<int32_t>(void*, int32_t, int64_t)> reader;
    AudioStream() { ++fakeStreamsAlive; }
    ~AudioStream() { --fakeStreamsAlive; }
    AudioApi getAudioApi() const { return AudioApi::AAudio; }
    void* getUnderlyingStream() { return native.closed ? nullptr : &native; }
    int getSampleRate() const { return clientRate; }
    int getHardwareSampleRate() const { return hardwareRate; }
    int getChannelCount() const { return channels; }
    int getBufferCapacityInFrames() const { return native.capacity; }
    AudioFormat getFormat() const { return AudioFormat::I16; }
    PerformanceMode getPerformanceMode() const { return static_cast<PerformanceMode>(native.performance); }
    StreamState getState() const { return static_cast<StreamState>(native.state.load()); }
    Result requestStart() {
        native.state = 4;
        if (dataCallback) {
            std::array<int16_t, 4096> samples{};
            if (dataCallback->onAudioReady(this, samples.data(), fakeOutputCallbackFrames)
                    == DataCallbackResult::Stop) native.state = 10;
            for (auto sample : samples) if (sample != 0) return Result::ErrorDisconnected;
        }
        return Result::OK;
    }
    Result requestStop() { native.state = 10; return Result::OK; }
    Result close() {
        if (native.activeReads) ++fakeCloseDuringRead;
        native.closed = true; native.state = 12; return Result::OK;
    }
    ResultWithValue<int32_t> read(void* buffer, int32_t frames, int64_t timeout) {
        ++native.activeReads;
        if (native.closed) ++fakeUseAfterClose;
        auto result = reader ? reader(buffer, frames, timeout) : startupRead(buffer, frames, timeout);
        if (result) { native.read += result.value() * (native.rate / 48000); native.written = native.read.load(); }
        --native.activeReads;
        return result;
    }
private:
    ResultWithValue<int32_t> startupRead(void* buffer, int32_t frames, int64_t timeout) {
        (void) timeout;
        const auto count = std::min(frames, startupFrames);
        std::fill_n(static_cast<int16_t*>(buffer), count * channels, int16_t(123));
        startupFrames -= count;
        return count;
    }
};

class AudioStreamBuilder {
    Direction direction_ = Direction::Output;
    SampleRateConversionQuality quality_ = SampleRateConversionQuality::Medium;
    int channels_ = 2, device_ = 0;
    std::shared_ptr<AudioStreamDataCallback> data_;
    std::shared_ptr<AudioStreamErrorCallback> error_;
public:
    AudioStreamBuilder* setDirection(Direction v) { direction_ = v; return this; }
    AudioStreamBuilder* setAudioApi(AudioApi) { return this; }
    AudioStreamBuilder* setPerformanceMode(PerformanceMode) { return this; }
    AudioStreamBuilder* setSharingMode(SharingMode) { return this; }
    AudioStreamBuilder* setUsage(Usage) { return this; }
    AudioStreamBuilder* setContentType(ContentType) { return this; }
    AudioStreamBuilder* setSampleRate(int) { return this; }
    AudioStreamBuilder* setChannelCount(int v) { channels_ = v; return this; }
    AudioStreamBuilder* setFormat(AudioFormat) { return this; }
    AudioStreamBuilder* setSampleRateConversionQuality(SampleRateConversionQuality v) { quality_ = v; return this; }
    AudioStreamBuilder* setDeviceId(int v) { device_ = v; return this; }
    AudioStreamBuilder* setInputPreset(InputPreset) { return this; }
    AudioStreamBuilder* setBufferCapacityInFrames(int) { return this; }
    AudioStreamBuilder* setDataCallback(std::shared_ptr<AudioStreamDataCallback> v) { data_ = v; return this; }
    AudioStreamBuilder* setErrorCallback(std::shared_ptr<AudioStreamErrorCallback> v) { error_ = v; return this; }
    Result openStream(std::shared_ptr<AudioStream>& result) {
        result = std::make_shared<AudioStream>();
        result->direction = direction_; result->channels = channels_;
        result->dataCallback = data_; result->errorCallback = error_;
        if (direction_ == Direction::Input) {
            ++fakeInputOpenCount;
            const bool strict = quality_ == SampleRateConversionQuality::None;
            result->native.rate = strict ? fakeStrictInputRawRate.load() : 96000;
            result->clientRate = strict ? result->native.rate : 48000;
            result->hardwareRate = strict ? fakeStrictInputHardwareRate.load() : 96000;
            result->native.burst = 480;
            result->native.buffer = result->native.capacity = 8192;
            result->native.performance = strict ? fakeStrictInputPerformance.load() : 12;
            result->native.device = fakeInputDevice.load();
            result->startupFrames = fakeInputStartupFrames;
        } else {
            // A nonzero requested output route would change this fake's route,
            // letting tests detect accidental forcing of the system default.
            result->native.device = device_ ? device_ : fakeOutputDevice.load();
        }
        return Result::OK;
    }
};
}
