import AppKit
import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

final class MusicExcerptWriter {
    private let lock = NSLock()
    private var file: AVAudioFile?
    init(url: URL, format: AVAudioFormat) throws {
        file = try AVAudioFile(forWriting: url, settings: format.settings)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
    func append(_ buffer: AVAudioPCMBuffer) throws {
        lock.lock(); defer { lock.unlock() }
        try file?.write(from: buffer)
    }
    func close() { lock.lock(); file = nil; lock.unlock() }
    deinit { close() }
}

// Bounded in-memory stereo queue. Neither app audio nor microphone audio is written to disk.
final class MusicRing {
    private let lock = NSLock()
    private var left: [Float], right: [Float]
    private var readIndex = 0, count = 0
    private(set) var droppedFrames: UInt64 = 0
    private var maximum: Float = 0
    private var totalFrames: UInt64 = 0, totalBuffers: UInt64 = 0
    let format: AudioStreamBasicDescription

    init(_ format: AudioStreamBasicDescription) {
        self.format = format
        let capacity = max(4096, Int(format.mSampleRate * 2))
        left = [Float](repeating: 0, count: capacity)
        right = [Float](repeating: 0, count: capacity)
    }

    func push(_ input: UnsafePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard let first = buffers.first, let firstData = first.mData else { return }
        let interleaved = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved == 0
        let frames = Int(first.mDataByteSize) / (interleaved ? 8 : 4)
        let l = firstData.assumingMemoryBound(to: Float.self)
        let r = interleaved ? l + 1 : (buffers.count > 1 ? buffers[1].mData?.assumingMemoryBound(to: Float.self) : l)
        guard let r else { return }
        lock.lock(); defer { lock.unlock() }
        totalFrames += UInt64(frames); totalBuffers += 1
        for frame in 0..<frames {
            if count == left.count { readIndex = (readIndex + 1) % left.count; count -= 1; droppedFrames += 1 }
            let destination = (readIndex + count) % left.count
            let a = l[frame * (interleaved ? 2 : 1)], b = r[frame * (interleaved ? 2 : 1)]
            left[destination] = a.isFinite ? a : 0; right[destination] = b.isFinite ? b : 0
            maximum = max(maximum, abs(left[destination]), abs(right[destination])); count += 1
        }
    }

    func render(_ frames: Int, _ output: UnsafeMutablePointer<AudioBufferList>) {
        let buffers = UnsafeMutableAudioBufferListPointer(output)
        lock.lock(); defer { lock.unlock() }
        for frame in 0..<frames {
            let available = count > 0
            let a = available ? left[readIndex] : 0, b = available ? right[readIndex] : 0
            for channel in 0..<buffers.count {
                buffers[channel].mData?.assumingMemoryBound(to: Float.self)[frame] = channel == 0 ? a : b
            }
            if available { readIndex = (readIndex + 1) % left.count; count -= 1 }
        }
    }

    var report: [String: Any] {
        lock.lock(); defer { lock.unlock() }
        return ["signal_observed": maximum > 0, "peak_dbfs": maximum > 0 ? max(-160, 20 * log10(Double(maximum))) : -160,
                "dropped_frames": droppedFrames, "queued_frames": count,
                "samples_observed": totalFrames, "buffers_observed": totalBuffers]
    }
}

final class NeteaseCapture {
    private var tap: AudioObjectID = 0, aggregate: AudioObjectID = 0
    private var procedure: AudioDeviceIOProcID?
    private(set) var ring: MusicRing?
    private(set) var source: AVAudioSourceNode?
    private(set) var active = false

    private func matchingProcesses() throws -> [AudioObjectID] {
        var property = address(kAudioHardwarePropertyProcessObjectList), size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &property, 0, nil, &size) == noErr,
              size > 0, size <= 65536 else { throw ProbeFailure(code: "netease_audio_process_unavailable") }
        var processes = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        let result = processes.withUnsafeMutableBytes {
            AudioObjectGetPropertyData(system, &property, 0, nil, &size, $0.baseAddress!)
        }
        guard result == noErr else { throw ProbeFailure(code: "netease_audio_process_unavailable") }
        return processes.filter {
            guard let bundle = label($0, kAudioProcessPropertyBundleID) else { return false }
            return bundle == "com.netease.163music" || bundle.hasPrefix("com.netease.163music.")
        }
    }

    func start(in engine: AVAudioEngine? = nil,
               capture: ((AVAudioFormat) throws -> ((AVAudioPCMBuffer) -> Void))? = nil) throws {
        do {
            let processes = try matchingProcesses()
            guard !processes.isEmpty else { throw ProbeFailure(code: "netease_audio_process_unavailable") }
            let description = CATapDescription(stereoMixdownOfProcesses: processes)
            description.name = "Friday NetEase Audio"
            description.isPrivate = true; description.muteBehavior = .mutedWhenTapped
            guard AudioHardwareCreateProcessTap(description, &tap) == noErr else {
                throw ProbeFailure(code: "netease_audio_permission_or_tap_unavailable")
            }
            let specification: [String: Any] = [kAudioAggregateDeviceNameKey: "Friday Music Input",
                kAudioAggregateDeviceUIDKey: UUID().uuidString, kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString,
                                                 kAudioSubTapDriftCompensationKey: true]]]
            guard AudioHardwareCreateAggregateDevice(specification as CFDictionary, &aggregate) == noErr else {
                throw ProbeFailure(code: "netease_capture_device_unavailable")
            }
            guard let format = scalar(tap, kAudioTapPropertyFormat, AudioStreamBasicDescription()),
                  format.mFormatID == kAudioFormatLinearPCM, format.mBitsPerChannel == 32,
                  format.mFormatFlags & kAudioFormatFlagIsFloat != 0, format.mChannelsPerFrame == 2,
                  format.mSampleRate > 0 else { throw ProbeFailure(code: "netease_audio_format_unavailable") }
            let queue = MusicRing(format); ring = queue
            guard let renderingFormat = AVAudioFormat(standardFormatWithSampleRate: format.mSampleRate, channels: 2) else {
                throw ProbeFailure(code: "netease_audio_format_unavailable")
            }
            let consumer = try capture?(renderingFormat)
            if let engine {
                let node = AVAudioSourceNode(format: renderingFormat) { _, _, frames, data in
                    queue.render(Int(frames), data); return noErr
                }
                source = node; engine.attach(node)
                engine.connect(node, to: engine.mainMixerNode, fromBus: 0, toBus: 1, format: renderingFormat)
            }
            guard AudioDeviceCreateIOProcIDWithBlock(&procedure, aggregate, nil, { _, input, _, _, _ in
                queue.push(input)
                if let consumer {
                    let frames = Int(input.pointee.mBuffers.mDataByteSize) / Int(format.mBytesPerFrame)
                    if frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: renderingFormat, frameCapacity: AVAudioFrameCount(frames)) {
                        buffer.frameLength = AVAudioFrameCount(frames)
                        queue.render(frames, buffer.mutableAudioBufferList)
                        consumer(buffer)
                    }
                }
            }) == noErr,
                  let procedure, AudioDeviceStart(aggregate, procedure) == noErr else {
                throw ProbeFailure(code: "netease_audio_capture_failed")
            }
            active = true
        } catch { stop(); throw error }
    }

    func stop() {
        active = false
        if aggregate != 0, let procedure {
            AudioDeviceStop(aggregate, procedure); AudioDeviceDestroyIOProcID(aggregate, procedure)
        }
        procedure = nil
        if aggregate != 0 { AudioHardwareDestroyAggregateDevice(aggregate); aggregate = 0 }
        if tap != 0 { AudioHardwareDestroyProcessTap(tap); tap = 0 }
        source = nil
    }
    deinit { stop() }
}
