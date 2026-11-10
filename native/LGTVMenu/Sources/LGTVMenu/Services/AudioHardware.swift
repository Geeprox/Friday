import CoreAudio
import AVFoundation
import Darwin
import Foundation
import LGTVDSP

struct AudioOutput: Identifiable, Equatable {
    let id: String
    let device: AudioDeviceID
    let name: String
    let transport: UInt32
    var isHDMI: Bool { transport == kAudioDeviceTransportTypeHDMI || transport == kAudioDeviceTransportTypeDisplayPort }
}

enum AudioBridgeError: LocalizedError {
    case hal(OSStatus), missingDriver, missingOutput, unsupportedFormat, recoveryUnavailable, inputPermission
    var errorDescription: String? {
        switch self {
        case .hal(let code): return "音频路由未完成（Core Audio \(code)）。已尝试恢复原输出。"
        case .missingDriver: return "请先安装 LG TV 音量组件。"
        case .missingOutput: return "所选 HDMI 音频输出暂不可用。"
        case .unsupportedFormat: return "当前输出格式不受支持。请在“音频 MIDI 设置”中选择双声道、44.1 或 48 kHz。"
        case .recoveryUnavailable: return "音频恢复助手不可用，未切换系统输出。"
        case .inputPermission: return "请在系统设置 → 隐私与安全性 → 麦克风中允许 LG TV。仅用于读取自己的虚拟音频输入。"
        }
    }
}

enum AudioHardware {
    static let virtualUID = "local.friday.LGTVAudio.device"
    static let reported: AudioObjectPropertySelector = 0x6c676173 // lgas
    static let intent: AudioObjectPropertySelector = 0x6c676576 // lgev
    static func address(_ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    }
    static func check(_ status: OSStatus) throws { if status != noErr { throw AudioBridgeError.hal(status) } }
    static func value<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, initial: T,
                         scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> T {
        var a = address(selector, scope), v = initial, size = UInt32(MemoryLayout<T>.size)
        try withUnsafeMutablePointer(to: &v) { try check(AudioObjectGetPropertyData(object, &a, 0, nil, &size, $0)) }
        return v
    }
    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) throws -> String {
        let v: Unmanaged<CFString>? = try value(object, selector, initial: nil as Unmanaged<CFString>?)
        guard let v else { throw AudioBridgeError.missingOutput }
        return v.takeRetainedValue() as String
    }
    static func objects(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> [AudioObjectID] {
        var a = address(selector, scope), size: UInt32 = 0
        try check(AudioObjectGetPropertyDataSize(object, &a, 0, nil, &size))
        var result = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard size > 0 else { return [] }
        try result.withUnsafeMutableBytes { try check(AudioObjectGetPropertyData(object, &a, 0, nil, &size, $0.baseAddress!)) }
        return result
    }
    static func outputs() -> [AudioOutput] {
        ((try? objects(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices)) ?? []).compactMap { id in
            guard channels(id, scope: kAudioObjectPropertyScopeOutput) >= 2,
                  let uid = try? string(id, kAudioDevicePropertyDeviceUID),
                  let name = try? string(id, kAudioObjectPropertyName),
                  let transport = try? value(id, kAudioDevicePropertyTransportType, initial: UInt32(0)) else { return nil }
            return AudioOutput(id: uid, device: id, name: name, transport: transport)
        }
    }
    static func channels(_ device: AudioDeviceID, scope: AudioObjectPropertyScope) -> Int {
        var a = address(kAudioDevicePropertyStreamConfiguration, scope), size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &a, 0, nil, &size) == 0, size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(device, &a, 0, nil, &size, raw) == 0 else { return 0 }
        return UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self)).reduce(0) { $0 + Int($1.mNumberChannels) }
    }
    static func defaultOutput() -> AudioDeviceID {
        (try? value(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, initial: AudioDeviceID(0))) ?? 0
    }
    static func select(_ device: AudioDeviceID) throws {
        var a = address(kAudioHardwarePropertyDefaultOutputDevice), v = device
        try check(AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, UInt32(MemoryLayout.size(ofValue: v)), &v))
        guard defaultOutput() == device else { throw AudioBridgeError.missingOutput }
    }
    static func state(_ device: AudioDeviceID) throws -> [String: Any] {
        let v: Unmanaged<CFDictionary>? = try value(device, intent, initial: nil as Unmanaged<CFDictionary>?)
        guard let v else { throw AudioBridgeError.missingDriver }
        return v.takeRetainedValue() as NSDictionary as? [String: Any] ?? [:]
    }
    static func report(_ device: AudioDeviceID, volume: Int, muted: Bool, ready: Bool) throws {
        var a = address(reported)
        let dictionary = ["volume": volume, "muted": muted, "ready": ready] as CFDictionary
        var reference = Unmanaged.passUnretained(dictionary)
        try check(AudioObjectSetPropertyData(device, &a, 0, nil, UInt32(MemoryLayout.size(ofValue: reference)), &reference))
    }
    static func validatePCM(_ device: AudioDeviceID, scope: AudioObjectPropertyScope) throws {
        let streams = try objects(device, kAudioDevicePropertyStreams, scope: scope)
        guard !streams.isEmpty else { throw AudioBridgeError.unsupportedFormat }
        for stream in streams {
            let f = try value(stream, kAudioStreamPropertyVirtualFormat, initial: AudioStreamBasicDescription())
            guard f.mFormatID == kAudioFormatLinearPCM, f.mBitsPerChannel == 32,
                  f.mFormatFlags & kAudioFormatFlagIsFloat != 0,
                  f.mFormatFlags & kAudioFormatFlagIsBigEndian == 0,
                  f.mChannelsPerFrame > 0,
                  f.mBytesPerFrame == 4 * (f.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0 ? 1 : f.mChannelsPerFrame),
                  [44100.0, 48000.0].contains(f.mSampleRate) else { throw AudioBridgeError.unsupportedFormat }
        }
    }
}

/// The aggregate contains only our own loopback input and the explicitly selected HDMI output.
/// Its clock follows HDMI; Core Audio compensates drift on the virtual subdevice.
@MainActor
final class HDMIForwarder {
    private var aggregate: AudioDeviceID = 0
    private var proc: AudioDeviceIOProcID?
    private var guardProcess: Process?
    private var guardPipe: Pipe?
    private(set) var physicalUID: String?
    private(set) var stage = "idle"
    private(set) var routeInputChannels = 0
    private(set) var routeOutputChannels = 0
    private(set) var routeSubdevices = 0
    private(set) var inputPermission = "unknown"

    func start(physical: AudioOutput, virtual: AudioOutput) async throws {
        if aggregate != 0 && physicalUID == physical.id { return }
        stop()
        stage = "physical_output"
        LGTVResetRouteStats()
        guard physical.isHDMI, AudioHardware.channels(physical.device, scope: kAudioObjectPropertyScopeInput) == 0,
              AudioHardware.channels(physical.device, scope: kAudioObjectPropertyScopeOutput) == 2 else { throw AudioBridgeError.unsupportedFormat }
        let rate = try AudioHardware.value(physical.device, kAudioDevicePropertyNominalSampleRate, initial: Float64(0))
        guard [44100.0, 48000.0].contains(rate) else { throw AudioBridgeError.unsupportedFormat }
        var virtualRate = rate, rateAddress = AudioHardware.address(kAudioDevicePropertyNominalSampleRate)
        stage = "virtual_sample_rate"
        try AudioHardware.check(AudioObjectSetPropertyData(virtual.device, &rateAddress, 0, nil, 8, &virtualRate))
        // Sample-rate changes are asynchronous driver configuration requests.
        for _ in 0..<20 {
            if (try? AudioHardware.value(virtual.device, kAudioDevicePropertyNominalSampleRate, initial: Float64(0))) == rate { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        guard try AudioHardware.value(virtual.device, kAudioDevicePropertyNominalSampleRate, initial: Float64(0)) == rate else { throw AudioBridgeError.unsupportedFormat }
        stage = "physical_pcm_format"
        try AudioHardware.validatePCM(physical.device, scope: kAudioObjectPropertyScopeOutput)
        stage = "virtual_input_permission"
        let permission = AVCaptureDevice.authorizationStatus(for: .audio)
        inputPermission = String(permission.rawValue)
        if permission == .notDetermined {
            let granted = await AVCaptureDevice.requestAccess(for: .audio)
            try Task.checkCancellation()
            inputPermission = granted ? "authorized" : "denied"
            guard granted else { throw AudioBridgeError.inputPermission }
        } else {
            guard permission == .authorized else { throw AudioBridgeError.inputPermission }
            inputPermission = "authorized"
        }
        let description: [String: Any] = [
            kAudioAggregateDeviceUIDKey: "local.friday.LGTVAudio.route.\(UUID().uuidString)",
            kAudioAggregateDeviceNameKey: "LG TV Audio Route", kAudioAggregateDeviceIsPrivateKey: 1,
            // On the installed HAL, stacked=1 creates an output-only multi-output device.
            // Concatenated mode preserves the virtual input and distinct HDMI output channels.
            kAudioAggregateDeviceIsStackedKey: 0, kAudioAggregateDeviceMainSubDeviceKey: physical.id,
            kAudioAggregateDeviceSubDeviceListKey: [
                [kAudioSubDeviceUIDKey: virtual.id, kAudioSubDeviceDriftCompensationKey: 1],
                [kAudioSubDeviceUIDKey: physical.id, kAudioSubDeviceDriftCompensationKey: 0]
            ]
        ]
        stage = "aggregate_create"
        try AudioHardware.check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregate))
        do {
            stage = "aggregate_configuration"
            var ids: [String] = []
            // HAL publishes aggregate streams asynchronously; an empty initial read is not a format verdict.
            for _ in 0..<40 {
                try Task.checkCancellation()
                let ref: Unmanaged<CFArray>? = (try? AudioHardware.value(aggregate, kAudioAggregateDevicePropertyFullSubDeviceList, initial: nil as Unmanaged<CFArray>?)) ?? nil
                if let ref { ids = ref.takeRetainedValue() as NSArray as? [String] ?? [] }
                routeSubdevices = ids.count
                routeInputChannels = AudioHardware.channels(aggregate, scope: kAudioObjectPropertyScopeInput)
                routeOutputChannels = AudioHardware.channels(aggregate, scope: kAudioObjectPropertyScopeOutput)
                if Set(ids) == Set([virtual.id, physical.id]), ids.count == 2,
                   routeInputChannels == 2, routeOutputChannels == 4 { break }
                try await Task.sleep(nanoseconds: 50_000_000)
            }
            guard Set(ids) == Set([virtual.id, physical.id]), ids.count == 2,
                  routeInputChannels == 2, routeOutputChannels == 4 else { throw AudioBridgeError.unsupportedFormat }
            let offset: UInt32 = ids.first == physical.id ? 0 : 2
            stage = "aggregate_input_format"
            try AudioHardware.validatePCM(aggregate, scope: kAudioObjectPropertyScopeInput)
            stage = "aggregate_output_format"
            try AudioHardware.validatePCM(aggregate, scope: kAudioObjectPropertyScopeOutput)
            // No Swift actor, closure captures or ARC work runs on the real-time thread.
            stage = "io_create"
            try AudioHardware.check(LGTVCreateRouteIOProc(aggregate, offset, &proc))
            stage = "recovery_start"
            try startRecovery(physical.id)
            stage = "io_start"
            try AudioHardware.check(AudioDeviceStart(aggregate, proc))
            physicalUID = physical.id
            stage = "running"
        } catch { stop(); throw error }
    }

    func stop() {
        if aggregate != 0 {
            if let proc { AudioDeviceStop(aggregate, proc); AudioDeviceDestroyIOProcID(aggregate, proc) }
            AudioHardwareDestroyAggregateDevice(aggregate)
        }
        aggregate = 0; proc = nil; physicalUID = nil
        // A deliberate stop is handled by the owner; disarm the crash-only helper.
        try? guardPipe?.fileHandleForWriting.write(contentsOf: Data([75]))
        guardPipe?.fileHandleForWriting.closeFile(); guardPipe = nil
        guardProcess = nil
    }

    private func startRecovery(_ uid: String) throws {
        let url = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/LGTVAudioRecovery")
        guard FileManager.default.isExecutableFile(atPath: url.path) else { throw AudioBridgeError.recoveryUnavailable }
        let process = Process(), pipe = Pipe()
        _ = fcntl(pipe.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)
        process.executableURL = url; process.arguments = [uid]
        process.standardInput = pipe; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        try process.run()
        pipe.fileHandleForReading.closeFile()
        guard process.isRunning else { throw AudioBridgeError.recoveryUnavailable }
        guardProcess = process; guardPipe = pipe
    }
}
