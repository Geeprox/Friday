// Input diagnostics and explicit, bounded listening. No recording or default-device writes.
import AppKit
import AVFoundation
import AudioToolbox
import CoreAudio
import CryptoKit
import Foundation

struct ProbeFailure: Error { let code: String }

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func scalar<T>(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector,
               _ initial: T, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> T? {
    var property = address(selector, scope), value = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(device, &property, 0, nil, &size, $0)
    }
    return status == noErr ? value : nil
}

func label(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var property = address(selector)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(device, &property, 0, nil, &size, &value) == noErr,
          let value else { return nil }
    return value.takeRetainedValue() as String
}

func channelCount(_ device: AudioObjectID, _ scope: AudioObjectPropertyScope) -> Int {
    var property = address(kAudioDevicePropertyStreamConfiguration, scope), size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(device, &property, 0, nil, &size) == noErr,
          size >= MemoryLayout<AudioBufferList>.size else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(device, &property, 0, nil, &size, raw) == noErr else { return 0 }
    return UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        .reduce(0) { $0 + Int($1.mNumberChannels) }
}

struct Device {
    let id: AudioDeviceID
    let uid: String
    let name: String
    let inputs: Int
    let outputs: Int
    var report: [String: Any] {
        var result: [String: Any] = ["uid": uid, "name": name, "input_channels": inputs, "output_channels": outputs]
        if let rate = scalar(id, kAudioDevicePropertyNominalSampleRate, Float64(0)), rate > 0 {
            result["sample_rate_hz"] = rate
            if let frames = scalar(id, kAudioDevicePropertyBufferFrameSize, UInt32(0)) {
                result["io_buffer_frames"] = frames
                result["io_buffer_ms"] = Double(frames) / rate * 1000
            }
            for (key, scope) in [("input", kAudioObjectPropertyScopeInput), ("output", kAudioObjectPropertyScopeOutput)] {
                if let frames = scalar(id, kAudioDevicePropertyLatency, UInt32(0), scope) {
                    result[key + "_reported_device_latency_ms"] = Double(frames) / rate * 1000
                }
                if let frames = scalar(id, kAudioDevicePropertySafetyOffset, UInt32(0), scope) {
                    result[key + "_safety_offset_ms"] = Double(frames) / rate * 1000
                }
            }
        }
        return result
    }
}

func devices() throws -> [Device] {
    var property = address(kAudioHardwarePropertyDevices), size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &property, 0, nil, &size) == noErr,
          size > 0, size <= 65536 else { throw ProbeFailure(code: "audio_inventory_unavailable") }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    let status = ids.withUnsafeMutableBytes {
        AudioObjectGetPropertyData(system, &property, 0, nil, &size, $0.baseAddress!)
    }
    guard status == noErr else { throw ProbeFailure(code: "audio_inventory_unavailable") }
    return ids.compactMap { id in
        guard let uid = label(id, kAudioDevicePropertyDeviceUID), let name = label(id, kAudioObjectPropertyName) else { return nil }
        return Device(id: id, uid: uid, name: name,
                      inputs: channelCount(id, kAudioObjectPropertyScopeInput),
                      outputs: channelCount(id, kAudioObjectPropertyScopeOutput))
    }
}

func permission() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .notDetermined: return "not_determined"
    case .denied: return "denied"
    case .restricted: return "restricted"
    @unknown default: return "unknown"
    }
}

func bindAudioDevice(_ node: AVAudioIONode, _ device: Device) throws {
    try node.auAudioUnit.setDeviceID(device.id)
    guard node.auAudioUnit.deviceID == device.id else { throw ProbeFailure(code: "device_selection_mismatch") }
}

// Only transient numeric aggregates survive the audio callback. No PCM buffer is retained.
final class Levels {
    private let lock = NSLock()
    private var samples: UInt64 = 0, buffers: UInt64 = 0, clipped: UInt64 = 0
    private var squares: Double = 0, peak: Double = 0
    func add(_ values: UnsafePointer<Float>, _ count: Int, stride: Int = 1) {
        var sum: Double = 0, maximum: Double = 0
        var clips: UInt64 = 0
        for index in 0..<count {
            let value = Double(values[index * stride])
            guard value.isFinite else { continue }
            sum += value * value
            maximum = max(maximum, abs(value))
            if abs(value) >= 0.999 { clips += 1 }
        }
        lock.lock(); defer { lock.unlock() }
        samples += UInt64(count); buffers += 1; squares += sum; peak = max(peak, maximum); clipped += clips
    }
    var report: [String: Any] {
        lock.lock(); defer { lock.unlock() }
        return ["samples_observed": samples, "buffers_observed": buffers, "clipped_samples": clipped,
                "rms_dbfs": samples > 0 && squares > 0 ? max(-160, 10 * log10(squares / Double(samples))) : -160,
                "peak_dbfs": peak > 0 ? max(-160, 20 * log10(peak)) : -160,
                "signal_observed": peak > 0]
    }
}

let args = Array(CommandLine.arguments.dropFirst())
func option(_ name: String) -> String? {
    guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
    return args[index + 1]
}

let reportPath = option("--report")
func emit(_ value: [String: Any]) {
    var result = value
    result["schema_version"] = 1
    result["captured_at"] = ISO8601DateFormatter().string(from: Date())
    if result["raw_audio_saved"] == nil { result["raw_audio_saved"] = false }
    if result["playback_started"] == nil { result["playback_started"] = false }
    result["system_audio_defaults_changed"] = false
    result["end_to_end_latency_measured"] = false
    guard let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) else { exit(2) }
    if let reportPath {
        do {
            try data.write(to: URL(fileURLWithPath: reportPath), options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: reportPath)
        } catch { exit(2) }
    } else {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

func fail(_ code: String) -> Never {
    emit(["status": "unavailable", "code": code, "microphone_permission": permission()])
    exit(1)
}

func meter(_ selected: Device, _ seconds: Double) throws {
    let engine = AVAudioEngine()
    let input = engine.inputNode
    guard let unit = input.audioUnit else { throw ProbeFailure(code: "input_audio_unit_unavailable") }
    var device = selected.id
    guard AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                               0, &device, UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr else {
        throw ProbeFailure(code: "input_selection_failed")
    }
    func stillSelected() -> Bool {
        var current: AudioDeviceID = 0, size = UInt32(MemoryLayout<AudioDeviceID>.size)
        return AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                                    0, &current, &size) == noErr && current == selected.id
    }
    guard stillSelected() else { throw ProbeFailure(code: "input_selection_mismatch") }
    let format = input.outputFormat(forBus: 0)
    guard format.channelCount > 0, format.channelCount <= 32, format.sampleRate > 0,
          format.commonFormat == .pcmFormatFloat32 else { throw ProbeFailure(code: "input_format_unavailable") }
    let levels = (0..<Int(format.channelCount)).map { _ in Levels() }
    // Input tap only: deliberately no connection from the microphone to any output node.
    input.installTap(onBus: 0, bufferSize: 256, format: format) { buffer, _ in
        guard let data = buffer.floatChannelData, Int(buffer.format.channelCount) == levels.count else { return }
        for channel in 0..<levels.count {
            if buffer.format.isInterleaved {
                levels[channel].add(data[0] + channel, Int(buffer.frameLength), stride: levels.count)
            } else {
                levels[channel].add(data[channel], Int(buffer.frameLength))
            }
        }
    }
    defer { engine.stop(); input.removeTap(onBus: 0) }
    try engine.start()
    emit(["status": "metering", "input": selected.report, "seconds": seconds])
    let deadline = ProcessInfo.processInfo.systemUptime + seconds
    while ProcessInfo.processInfo.systemUptime < deadline {
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
        guard stillSelected(), engine.isRunning else { throw ProbeFailure(code: "input_changed_or_stopped") }
    }
    let statistics = levels.map(\.report)
    let framesObserved = statistics.allSatisfy { ($0["buffers_observed"] as? UInt64 ?? 0) > 0 }
    emit(["status": framesObserved ? "completed" : "unavailable",
          "code": framesObserved ? "none" : "no_input_frames",
          "input": selected.report, "seconds": seconds, "levels_by_channel": statistics,
          "signal_observed": statistics.contains { $0["signal_observed"] as? Bool == true },
          "microphone_permission": permission()])
}

final class ListeningWindow: NSObject, NSWindowDelegate {
    private let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 430),
                                  styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
    private let microphone = NSPopUpButton(), output = NSPopUpButton()
    private let status = NSTextField(wrappingLabelWithString: "选择设备后开始，30 秒自动停止")
    private let level = NSTextField(labelWithString: "输入电平：等待试听")
    private let song = NSTextField(labelWithString: "尚未选择伴奏")
    private let volume = NSSlider(value: 0.15, minValue: 0, maxValue: 0.5, target: nil, action: nil)
    private let start = NSButton(title: "开始试听", target: nil, action: nil)
    private let stop = NSButton(title: "停止试听", target: nil, action: nil)
    private let refresh = NSButton(title: "刷新设备", target: nil, action: nil)
    private let choose = NSButton(title: "选择歌曲", target: nil, action: nil)
    private let prepare = NSButton(title: "生成伴奏", target: nil, action: nil)
    private let netease = NSButton(checkboxWithTitle: "使用网易云播放声音", target: nil, action: nil)
    private let excerpt = NSButton(title: "截取歌曲", target: nil, action: nil)
    private let playNetease = NSButton(title: "播放歌曲", target: nil, action: nil)
    private var inputs: [Device] = [], outputs: [Device] = []
    private var networkOutputs: [[String: String]] = []
    private let root = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    private var discovery: Process?, discoveryGeneration = 0
    private var preparation: Process?, preparationTimer: Timer?, preparationGeneration = 0
    private var selectedFile: URL?
    private var engine: AVAudioEngine?, player: AVAudioPlayerNode?, timer: Timer?
    private var audioFailureCode: Int?
    private var musicCapture: NeteaseCapture?, networkAudio: NetworkAudio?
    private var microphoneCapture: MicrophoneCapture?
    private var excerptFile: MusicExcerptWriter?, excerptURL: URL?, capturingExcerpt = false
    private var networkTapInstalled = false
    private var audioFile: AVAudioFile?, levels: Levels?, generation = 0
    private var activeInput: Device?, activeOutput: Device?
    private var startedAt: TimeInterval = 0, tapInstalled = false, startedPlayback = false
    private var awaitingPermission = false
    private var waitingForNetwork = false, networkSetupAt: TimeInterval = 0

    func run() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        window.title = "Friday · 演唱试听"
        window.setContentSize(NSSize(width: 570, height: 520))
        window.isReleasedWhenClosed = false
        window.delegate = self
        let title = NSTextField(labelWithString: "演唱试听")
        title.font = .systemFont(ofSize: 23, weight: .semibold)
        let hint = NSTextField(wrappingLabelWithString: "网络音箱可直接连接，不用切换 Mac 输出。试听前请将音箱音量调低。")
        hint.textColor = .secondaryLabelColor
        for (button, action) in [(start, #selector(begin)), (stop, #selector(end)),
                                 (refresh, #selector(reload)), (choose, #selector(selectSong)),
                                 (prepare, #selector(prepareSong)), (excerpt, #selector(captureExcerpt)),
                                 (playNetease, #selector(resumeNetease))] {
            button.target = self; button.action = action; button.bezelStyle = .rounded
        }
        stop.isEnabled = false
        volume.target = self; volume.action = #selector(changeVolume)
        let stack = NSStackView(views: [title, hint, row("麦克风", microphone), row("输出设备", output),
                                       row("试听音量", volume), row("伴奏来源", netease),
                                       NSStackView(views: [choose, prepare, playNetease, excerpt]),
                                       NSTextField(labelWithString: "截取网易云 30 秒，自动生成伴奏"), song,
                                       NSStackView(views: [refresh, start, stop]), level, status])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
                                     stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
                                     stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24)])
        microphone.widthAnchor.constraint(equalToConstant: 370).isActive = true
        output.widthAnchor.constraint(equalToConstant: 370).isActive = true
        volume.widthAnchor.constraint(equalToConstant: 370).isActive = true
        reload()
        if let manifest = option("--prepared-manifest") { loadPreparedManifest(URL(fileURLWithPath: manifest)) }
        window.initialFirstResponder = output
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        NSApp.run()
    }

    private func row(_ name: String, _ control: NSView) -> NSStackView {
        let label = NSTextField(labelWithString: name)
        label.widthAnchor.constraint(equalToConstant: 65).isActive = true
        return NSStackView(views: [label, control])
    }

    private func loadPreparedManifest(_ manifest: URL) {
        let cache = root.appendingPathComponent(".artifacts/ktv-audio/separation/cache").resolvingSymlinksInPath().path + "/"
        guard manifest.resolvingSymlinksInPath().path.hasPrefix(root.appendingPathComponent(".artifacts/ktv-audio/").path),
              let data = try? Data(contentsOf: manifest), data.count <= 65536,
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              value["status"] as? String == "ready", value["model_verified"] as? Bool == true,
              let path = value["instrumental_path"] as? String,
              URL(fileURLWithPath: path).resolvingSymlinksInPath().path.hasPrefix(cache),
              let artifacts = value["artifacts"] as? [String: [String: Any]], let artifact = artifacts["instrumental"],
              let expected = artifact["sha256"] as? String, let expectedBytes = artifact["bytes"] as? Int,
              expectedBytes > 0, expectedBytes <= 134217728,
              let bytes = try? Data(contentsOf: URL(fileURLWithPath: path)), bytes.count == expectedBytes,
              SHA256.hash(data: bytes).map({ String(format: "%02x", $0) }).joined() == expected,
              let prepared = try? AVAudioFile(forReading: URL(fileURLWithPath: path)) else {
            status.stringValue = "伴奏文件已变化，请重新生成"; return
        }
        audioFile = prepared; netease.state = .off; song.stringValue = "网易云片段 · 伴奏已就绪"
        if let source = option("--selected-source"), let sourceBytes = try? Data(contentsOf: URL(fileURLWithPath: source)),
           sourceBytes.count <= 134217728,
           SHA256.hash(data: sourceBytes).map({ String(format: "%02x", $0) }).joined() == value["source_sha256"] as? String {
            selectedFile = URL(fileURLWithPath: source)
        }
    }

    @objc private func reload() {
        guard engine == nil, !awaitingPermission, preparation == nil else { return }
        do {
            let found = try devices()
            inputs = found.filter { $0.inputs > 0 }
            outputs = found.filter { $0.outputs > 0 && $0.inputs == 0 && $0.uid != "BlackHole2ch_UID" }
            microphone.removeAllItems(); output.removeAllItems()
            microphone.addItems(withTitles: ["请选择麦克风"] + inputs.map(\.name))
            rebuildOutputs()
            status.stringValue = "正在查找网络音箱"
            discoverNetwork()
        } catch { status.stringValue = "无法读取设备，请重新刷新" }
    }

    private func rebuildOutputs() {
        output.removeAllItems()
        output.addItems(withTitles: ["请选择输出设备"] + outputs.map(\.name) + networkOutputs.map { ($0["name"] ?? "音箱") + " · 网络" })
    }

    private func discoverNetwork() {
        discoveryGeneration += 1
        let request = discoveryGeneration
        if let old = discovery, old.isRunning { old.terminate() }
        let process = Process()
        let report = root.appendingPathComponent(".artifacts/ktv-audio/discovery-\(UUID().uuidString).json")
        process.executableURL = root.appendingPathComponent("data/embodied/runtime/bin/python")
        process.arguments = [root.appendingPathComponent("scripts/ktv_network_audio.py").path, "--discover", "--report", report.path]
        process.currentDirectoryURL = root; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        discovery = process
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.discoveryGeneration == request else { return }
                self.discovery = nil
                guard self.engine == nil, !self.awaitingPermission else { return }
                if let data = try? Data(contentsOf: report), data.count <= 65536,
                   let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let found = value["devices"] as? [[String: Any]] {
                    self.networkOutputs = found.compactMap { entry in
                        guard let name = entry["name"] as? String, let identifier = entry["identifier"] as? String,
                              let host = entry["host"] as? String else { return nil }
                        return ["name": name, "identifier": identifier, "host": host]
                    }
                    self.rebuildOutputs()
                    self.status.stringValue = self.networkOutputs.isEmpty ? "未找到网络音箱，可刷新重试" : "已找到网络音箱，选择输出后开始"
                } else { self.status.stringValue = "网络音箱暂不可用，可刷新重试" }
            }
        }
        do { try process.run() } catch { discovery = nil; status.stringValue = "网络音箱暂不可用，可刷新重试" }
    }

    @objc private func selectSong() {
        guard engine == nil, !awaitingPermission, preparation == nil else { return }
        let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { audioFile = try AVAudioFile(forReading: url); selectedFile = url; netease.state = .off; song.stringValue = "原曲 · " + url.lastPathComponent }
        catch { audioFile = nil; selectedFile = nil; song.stringValue = "无法播放此文件，请选择其他音频" }
    }

    @objc private func prepareSong() {
        guard engine == nil, !awaitingPermission, preparation == nil else { return }
        guard let file = selectedFile else { status.stringValue = "请先选择一首本地歌曲"; return }
        let executable = root.appendingPathComponent(".artifacts/ktv-audio/separation/runtime/bin/python")
        guard FileManager.default.isExecutableFile(atPath: executable.path),
              FileManager.default.fileExists(atPath: root.appendingPathComponent(".artifacts/ktv-audio/separation/READY.json").path) else {
            status.stringValue = "伴奏引擎仍在准备，请稍后重试"; return
        }
        preparationGeneration += 1
        let request = preparationGeneration
        let report = root.appendingPathComponent(".artifacts/ktv-audio/preparation-\(UUID().uuidString).json")
        let cache = root.appendingPathComponent(".artifacts/ktv-audio/separation/cache", isDirectory: true)
        let process = Process(); preparation = process
        process.executableURL = executable
        process.arguments = [root.appendingPathComponent("scripts/ktv_prepare.py").path, "--input", file.path,
                             "--cache-dir", cache.path, "--report", report.path]
        process.currentDirectoryURL = root; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        controls(active: true); status.stringValue = "正在生成伴奏，首次需要下载模型"
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, self.preparationGeneration == request else { return }
                self.preparation = nil; self.preparationTimer?.invalidate(); self.preparationTimer = nil
                self.controls(active: false)
                guard let data = try? Data(contentsOf: report), data.count <= 65536,
                      let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      value["status"] as? String == "ready", let path = value["instrumental_path"] as? String,
                      URL(fileURLWithPath: path).resolvingSymlinksInPath().path.hasPrefix(cache.resolvingSymlinksInPath().path + "/"),
                      let instrumental = try? AVAudioFile(forReading: URL(fileURLWithPath: path)) else {
                    self.status.stringValue = "伴奏生成未完成，可重试或更换歌曲"; return
                }
                self.audioFile = instrumental; self.netease.state = .off
                self.song.stringValue = "伴奏已就绪 · " + file.lastPathComponent
                self.status.stringValue = "伴奏已就绪，可以开始试听"
            }
        }
        do { try process.run() }
        catch { preparation = nil; controls(active: false); status.stringValue = "伴奏引擎暂不可用，请重试" }
    }

    @objc private func resumeNetease() {
        guard engine == nil, !capturingExcerpt, !awaitingPermission, preparation == nil else { return }
        let process = Process()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.arguments = [root.appendingPathComponent("scripts/ktv_netease.py").path, "resume", "--execute"]
        process.currentDirectoryURL = root; process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
        do { try process.run(); status.stringValue = "已请求网易云播放，请确认歌曲开始" }
        catch { status.stringValue = "无法打开网易云，请手动播放歌曲" }
    }

    @objc private func captureExcerpt() {
        guard engine == nil, !awaitingPermission, preparation == nil else { return }
        do {
            let capture = NeteaseCapture(); musicCapture = capture
            let directory = root.appendingPathComponent(".artifacts/ktv-audio/separation/sources", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                   attributes: [.posixPermissions: 0o700])
            let path = directory.appendingPathComponent("netease-\(UUID().uuidString).wav")
            excerptURL = path; capturingExcerpt = true
            generation += 1
            let request = generation
            try capture.start(capture: { [weak self] format in
                let file = try MusicExcerptWriter(url: path, format: format)
                self?.excerptFile = file
                return { [weak self] buffer in
                    do { try file.append(buffer) }
                    catch { DispatchQueue.main.async {
                        guard let self, self.generation == request, self.excerptURL == path, self.capturingExcerpt else { return }
                        self.finish("excerpt_write_failed", message: "歌曲片段保存失败，请重试")
                    } }
                }
            })
            startedAt = ProcessInfo.processInfo.systemUptime
            controls(active: true)
            status.stringValue = "正在截取网易云片段 · 30 秒"
            song.stringValue = "只保存网易云播放，不采集麦克风"
            emit(["status": "capturing_music_excerpt", "microphone_audio_saved": false,
                  "saved_audio_scope": "netease_playback_only", "raw_audio_saved": true, "max_seconds": 30])
            let watchdog = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in self?.tick() }
            timer = watchdog; RunLoop.main.add(watchdog, forMode: .common)
        } catch { finish("netease_excerpt_unavailable", message: "请先播放网易云歌曲，并允许音频访问") }
    }

    private func controls(active: Bool) {
        start.isEnabled = !active; stop.isEnabled = active
        microphone.isEnabled = !active; output.isEnabled = !active
        choose.isEnabled = !active; prepare.isEnabled = !active; excerpt.isEnabled = !active
        netease.isEnabled = !active; refresh.isEnabled = !active; playNetease.isEnabled = !active
    }

    @objc private func begin() {
        guard engine == nil, !awaitingPermission, preparation == nil else { return }
        let mic = microphone.indexOfSelectedItem - 1, speaker = output.indexOfSelectedItem - 1
        guard inputs.indices.contains(mic), speaker >= 0, speaker < outputs.count + networkOutputs.count else {
            status.stringValue = "请先选择麦克风和输出设备"; return
        }
        guard speaker >= outputs.count || !outputs[speaker].name.localizedCaseInsensitiveContains("BlackHole") else {
            status.stringValue = "请选择耳机或音箱，虚拟设备不能直接听到"; return
        }
        generation += 1
        let request = generation
        if permission() == "not_determined" {
            awaitingPermission = true; controls(active: true)
            status.stringValue = "请允许麦克风访问，或停止试听"
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                DispatchQueue.main.async {
                    guard let self, self.generation == request else { return }
                    self.awaitingPermission = false
                    if granted { self.startEngine(self.inputs[mic], speaker) }
                    else { self.controls(active: false); self.status.stringValue = "麦克风未获允许，请在系统设置中允许后重试" }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self] in
                guard let self, self.generation == request, self.awaitingPermission else { return }
                self.finish("permission_timeout", message: "等待权限超时，请重试")
            }
        } else if permission() == "authorized" { startEngine(inputs[mic], speaker) }
        else { status.stringValue = "麦克风未获允许，请在系统设置中允许后重试" }
    }

    private func selectDevice(_ node: AVAudioIONode, _ device: Device) throws {
        try bindAudioDevice(node, device)
    }

    private func matches(_ node: AVAudioIONode, _ device: Device) -> Bool {
        node.auAudioUnit.deviceID == device.id
    }

    private func startEngine(_ mic: Device, _ selection: Int) {
        do {
            let found = try devices()
            let endpoint = selection >= outputs.count ? networkOutputs[selection - outputs.count] : nil
            let audio = AVAudioEngine(); engine = audio
            let speaker: Device
            if endpoint != nil {
                guard let sink = found.first(where: { $0.uid == "BuiltInSpeakerDevice" && $0.outputs > 0 && $0.inputs == 0 }) else {
                    throw ProbeFailure(code: "network_sink_unavailable")
                }
                speaker = sink
            } else { speaker = outputs[selection] }
            guard found.contains(where: { $0.uid == mic.uid && $0.id == mic.id && $0.inputs > 0 }),
                  found.contains(where: { $0.uid == speaker.uid && $0.id == speaker.id && $0.outputs > 0 }) else {
                throw ProbeFailure(code: "selected_device_disappeared")
            }
            activeInput = mic; activeOutput = speaker
            if !matches(audio.outputNode, speaker) { try selectDevice(audio.outputNode, speaker) }
            guard matches(audio.outputNode, speaker) else {
                throw ProbeFailure(code: "device_selection_mismatch")
            }
            microphoneCapture = try MicrophoneCapture(input: mic, engine: audio)
            audio.mainMixerNode.outputVolume = Float(volume.doubleValue)
            if netease.state == .on {
                let capture = NeteaseCapture(); musicCapture = capture
                try capture.start(in: audio)
            } else if let file = audioFile {
                let backing = AVAudioPlayerNode(); player = backing; audio.attach(backing)
                audio.connect(backing, to: audio.mainMixerNode, fromBus: 0, toBus: 1, format: file.processingFormat)
                backing.volume = 0.5; file.framePosition = 0; backing.scheduleFile(file, at: nil)
            }
            if let endpoint {
                let format = audio.mainMixerNode.outputFormat(forBus: 0)
                let network = try NetworkAudio(root: root, endpoint: endpoint); networkAudio = network
                try network.prepare(format)
                audio.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in network.append(buffer) }
                networkTapInstalled = true
                let silent = AVAudioMixerNode(); audio.attach(silent); silent.outputVolume = 0
                audio.connect(audio.mainMixerNode, to: silent, format: format)
                audio.connect(silent, to: audio.outputNode, format: nil)
            }
            if endpoint != nil {
                waitingForNetwork = true; networkSetupAt = ProcessInfo.processInfo.systemUptime
            } else {
                try microphoneCapture?.start(); try audio.start(); player?.play(); startedPlayback = true
                startedAt = ProcessInfo.processInfo.systemUptime
            }
            controls(active: true)
            emit(["status": endpoint != nil ? "connecting" : "listening", "playback_started": endpoint == nil,
                  "input": mic.report, "output": speaker.report,
                  "backing_selected": audioFile != nil || netease.state == .on,
                  "netease_capture": netease.state == .on, "network_output": endpoint != nil, "max_seconds": 30])
            let watchdog = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in self?.tick() }
            timer = watchdog
            RunLoop.main.add(watchdog, forMode: .common)
            tick()
        } catch let error as ProbeFailure {
            finish(error.code, message: error.code.hasPrefix("netease") ? "网易云声音暂不可用，请先播放歌曲并允许音频访问" : "所选设备暂不可用，请刷新后重试")
        }
        catch { audioFailureCode = (error as NSError).code; finish("audio_start_failed", message: "无法开始试听，请重新选择设备") }
    }

    @objc private func changeVolume() { engine?.mainMixerNode.outputVolume = Float(volume.doubleValue) }

    private func tick() {
        if capturingExcerpt {
            guard musicCapture?.active == true else {
                finish("device_changed_or_stopped", message: "歌曲采集已停止，请重试"); return
            }
            let remaining = 30 - (ProcessInfo.processInfo.systemUptime - startedAt)
            if remaining <= 0 { finish("completed", message: "歌曲片段已就绪"); return }
            status.stringValue = "正在截取网易云片段 · \(Int(ceil(remaining))) 秒后准备伴奏"
            return
        }
        if waitingForNetwork {
            guard let network = networkAudio, let audio = engine else {
                finish("network_output_failed", message: "网络音箱连接已停止，请刷新后重试"); return
            }
            let state = network.report
            if state["status"] as? String == "unavailable" || state["pipe_failed"] as? Bool == true {
                finish("network_output_failed", message: "网络音箱连接未完成，请刷新后重试"); return
            }
            if state["status"] as? String == "input_ready" || state["status"] as? String == "streaming" {
                do {
                    try microphoneCapture?.start(); try audio.start(); player?.play(); startedPlayback = true
                    waitingForNetwork = false; startedAt = ProcessInfo.processInfo.systemUptime
                } catch { audioFailureCode = (error as NSError).code; finish("audio_start_failed", message: "无法开始试听，请重新选择设备") }
            } else if ProcessInfo.processInfo.systemUptime - networkSetupAt >= 12 {
                finish("network_setup_timeout", message: "音箱连接超时，请重试")
            } else { status.stringValue = "正在连接所选音箱，连接后开始试听" }
            return
        }
        guard let audio = engine, let mic = activeInput, let speaker = activeOutput,
              audio.isRunning, microphoneCapture?.active == true, microphoneCapture?.sourcesAvailable == true,
              microphoneCapture?.input.uid == mic.uid, matches(audio.outputNode, speaker) else {
            finish("device_changed_or_stopped", message: "设备已变化，试听已停止"); return
        }
        let remaining = 30 - (ProcessInfo.processInfo.systemUptime - startedAt)
        if remaining <= 0 { finish("completed", message: "试听结束，可以再次开始"); return }
        status.stringValue = "正在试听 · \(Int(ceil(remaining))) 秒后停止"
        if let network = networkAudio {
            let state = network.report
            if state["status"] as? String == "unavailable" || state["pipe_failed"] as? Bool == true {
                finish("network_output_failed", message: "网络音箱连接已停止，请刷新后重试"); return
            }
            if state["status"] as? String == "connecting" { status.stringValue = "正在连接音箱 · \(Int(ceil(remaining))) 秒后停止" }
        }
        if let value = microphoneCapture?.report["peak_dbfs"] as? Double { level.stringValue = String(format: "输入峰值：%.1f dBFS", value) }
    }

    private func finish(_ code: String, message: String) {
        let wasExcerpt = capturingExcerpt, capturedURL = excerptURL
        let microphoneReport = microphoneCapture?.report ?? levels?.report ?? [:]
        let missingInput = !wasExcerpt && code == "completed" && startedPlayback &&
            (microphoneReport["samples_observed"] as? UInt64 ?? 0) == 0
        let finalCode = missingInput ? "no_input_frames" : code
        generation += 1; awaitingPermission = false; waitingForNetwork = false; timer?.invalidate(); timer = nil
        preparationGeneration += 1
        if let process = preparation, process.isRunning {
            process.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
        }
        preparation = nil; preparationTimer?.invalidate(); preparationTimer = nil
        player?.stop(); engine?.stop()
        microphoneCapture?.stop(); microphoneCapture = nil
        if tapInstalled { engine?.inputNode.removeTap(onBus: 0) }; tapInstalled = false
        if networkTapInstalled { engine?.mainMixerNode.removeTap(onBus: 0) }; networkTapInstalled = false
        let musicReport = musicCapture?.ring?.report ?? [:], networkReport = networkAudio?.report ?? [:]
        musicCapture?.stop(); musicCapture = nil; networkAudio?.stop(); networkAudio = nil
        excerptFile?.close(); excerptFile = nil; excerptURL = nil; capturingExcerpt = false
        engine = nil; player = nil; controls(active: false)
        status.stringValue = missingInput ? "未收到麦克风输入，请检查设备" : message
        emit(["status": finalCode == "completed" || finalCode == "stopped" ? "completed" : "unavailable", "code": finalCode,
              "playback_started": startedPlayback, "playback_active": false,
              "microphone_levels": microphoneReport, "music_capture": musicReport, "network_output": networkReport,
              "backing_selected": audioFile != nil || netease.state == .on,
              "audio_failure_code": audioFailureCode ?? 0,
              "raw_audio_saved": wasExcerpt, "saved_audio_scope": wasExcerpt ? "netease_playback_only" : "none",
              "microphone_audio_saved": false])
        audioFailureCode = nil
        startedPlayback = false; levels = nil; activeInput = nil; activeOutput = nil
        if wasExcerpt, code == "completed", musicReport["signal_observed"] as? Bool == true, let capturedURL {
            selectedFile = capturedURL
            song.stringValue = "网易云片段已就绪，正在准备伴奏"
            prepareSong()
        } else if wasExcerpt, code == "completed" {
            status.stringValue = "未收到网易云声音，请先播放歌曲或检查权限"
        }
    }

    @objc private func end() { finish("stopped", message: "已停止，可以再次开始") }
    func windowWillClose(_ notification: Notification) {
        discoveryGeneration += 1
        let children = [discovery, preparation, networkAudio?.childProcess].compactMap { $0 }
        if let process = discovery, process.isRunning { process.terminate() }
        end(); window.orderOut(nil)
        // Keep this cleanup owner alive until its own children have exited.
        DispatchQueue.global().async {
            let deadline = ProcessInfo.processInfo.systemUptime + 3
            while children.contains(where: \.isRunning), ProcessInfo.processInfo.systemUptime < deadline {
                Thread.sleep(forTimeInterval: 0.02)
            }
            for child in children where child.isRunning { kill(child.processIdentifier, SIGKILL) }
            for child in children { child.waitUntilExit() }
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }
    }
}

do {
    if args.contains("--listen") {
        let listening = ListeningWindow()
        withExtendedLifetime(listening) { listening.run() }
    } else if args.contains("--check-output"), let uid = option("--output-uid") {
        let found = try devices().filter { $0.uid == uid && $0.outputs > 0 }
        guard found.count == 1 else { throw ProbeFailure(code: "exact_output_not_available") }
        emit(["status": "configuring_output", "output": found[0].report])
        let audio = AVAudioEngine()
        try bindAudioDevice(audio.outputNode, found[0])
        audio.stop()
        emit(["status": "completed", "output": found[0].report])
    } else if args.contains("--check-mix"), let inputUID = option("--input-uid"), let outputUID = option("--output-uid") {
        let found = try devices()
        guard let input = found.first(where: { $0.uid == inputUID && $0.inputs > 0 }),
              let output = found.first(where: { $0.uid == outputUID && $0.outputs > 0 }) else {
            throw ProbeFailure(code: "exact_device_unavailable")
        }
        let audio = AVAudioEngine()
        try bindAudioDevice(audio.outputNode, output)
        let microphone = try MicrophoneCapture(input: input, engine: audio)
        defer { audio.stop(); microphone.stop() }
        var backing: AVAudioPlayerNode?
        if let path = option("--backing") {
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
            let node = AVAudioPlayerNode(); backing = node; audio.attach(node)
            audio.connect(node, to: audio.mainMixerNode, fromBus: 0, toBus: 1, format: file.processingFormat)
            node.scheduleFile(file, at: nil)
        }
        let rendered = Levels(), format = audio.mainMixerNode.outputFormat(forBus: 0)
        audio.mainMixerNode.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            if let samples = buffer.floatChannelData?[0] { rendered.add(samples, Int(buffer.frameLength)) }
        }
        let silent = AVAudioMixerNode(); audio.attach(silent); silent.outputVolume = 0
        audio.connect(audio.mainMixerNode, to: silent, format: format)
        audio.connect(silent, to: audio.outputNode, format: nil)
        try microphone.start(); try audio.start(); backing?.play()
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.5))
        audio.stop(); microphone.stop(); audio.mainMixerNode.removeTap(onBus: 0)
        guard (microphone.report["samples_observed"] as? UInt64 ?? 0) > 0,
              (rendered.report["samples_observed"] as? UInt64 ?? 0) > 0 else { throw ProbeFailure(code: "no_input_frames") }
        emit(["status": "completed", "silent_render_verified": true,
              "input_levels": microphone.report, "mixed_output_levels": rendered.report])
    } else if args.contains("--self-test") {
        let levels = Levels()
        let fixture: [Float] = [0.5, -0.5, 1, -1]
        fixture.withUnsafeBufferPointer { levels.add($0.baseAddress!, $0.count) }
        let result = levels.report
        guard result["samples_observed"] as? UInt64 == 4,
              result["clipped_samples"] as? UInt64 == 2,
              abs((result["rms_dbfs"] as? Double ?? 0) - 10 * log10(0.625)) < 0.000001 else {
            throw ProbeFailure(code: "level_aggregation_failed")
        }
        let left = Levels(), right = Levels()
        let stereo: [Float] = [0, 0.5, 0, -0.5]
        stereo.withUnsafeBufferPointer {
            left.add($0.baseAddress!, 2, stride: 2)
            right.add($0.baseAddress! + 1, 2, stride: 2)
        }
        guard left.report["signal_observed"] as? Bool == false,
              right.report["signal_observed"] as? Bool == true,
              abs((right.report["peak_dbfs"] as? Double ?? 0) - 20 * log10(0.5)) < 0.000001 else {
            throw ProbeFailure(code: "channel_aggregation_failed")
        }
        emit(["status": "completed", "simulated": true, "check": "levels_clipping_silence_and_stereo"])
    } else if args.isEmpty || args.contains("--list") {
        emit(["status": "completed", "devices": try devices().map(\.report), "microphone_permission": permission(),
              "latency_basis": "device_reports_only_excludes_network_speaker_and_room"])
    } else if args.contains("--meter"), let uid = option("--input-uid"),
              let seconds = Double(option("--seconds") ?? "8"), seconds.isFinite, seconds >= 1, seconds <= 15 {
        let found = try devices().filter { $0.uid == uid && $0.inputs > 0 }
        guard found.count == 1 else { throw ProbeFailure(code: "exact_input_not_available") }
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        if permission() == "not_determined" {
            emit(["status": "awaiting_permission", "microphone_permission": permission()])
            var resolved: Bool? = nil
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                DispatchQueue.main.async { resolved = granted }
            }
            let deadline = ProcessInfo.processInfo.systemUptime + 60
            while resolved == nil && ProcessInfo.processInfo.systemUptime < deadline {
                RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.05))
            }
            guard let granted = resolved else { throw ProbeFailure(code: "permission_timeout") }
            guard granted else { throw ProbeFailure(code: "microphone_permission_denied") }
        }
        guard permission() == "authorized" else { throw ProbeFailure(code: "microphone_permission_denied") }
        try meter(found[0], seconds)
    } else {
        throw ProbeFailure(code: "invalid_arguments")
    }
} catch let error as ProbeFailure { fail(error.code) }
catch {
    emit(["status": "unavailable", "code": "audio_probe_failed", "native_error_code": (error as NSError).code])
    exit(1)
}
