import AppKit
import Combine
import CoreAudio
import Foundation
import LGTVCore
import LGTVDSP
import OSLog

@MainActor
final class TVAudioController: ObservableObject {
    static let shared = TVAudioController()
    @Published private(set) var outputs: [AudioOutput] = []
    @Published private(set) var selectedUID: String
    @Published private(set) var enabled: Bool
    @Published private(set) var driverInstalled = false
    @Published private(set) var state: TVAudioState?
    @Published private(set) var connected = false
    @Published private(set) var routing = false
    @Published private(set) var message = "安装音量组件后，可使用系统声音滑块和音量键。"
    private let log = Logger(subsystem: "local.friday.LGTVMenu", category: "Audio")
    private let forwarder = HDMIForwarder()
    private var configuration: TVConfiguration?
    private var subscriptions: Set<AnyCancellable> = []
    private var observers: [NSObjectProtocol] = []
    private var listeners: [(AudioObjectID, AudioObjectPropertyAddress, AudioObjectPropertyListenerBlock)] = []
    private var monitorTask: Task<Void, Never>?
    private var routeTask: Task<Void, Never>?
    private var routeID: UUID?
    private var writeTask: Task<Void, Never>?
    private var writeID: UUID?
    private var monitor: WebOSClient?
    private var writer: WebOSClient?
    private var pending: (volume: Int, muted: Bool)?
    private var lastRevision: UInt64 = 0
    private var virtual: AudioOutput?
    private var sleeping = false
    private var waitingForHDMIAfterWake = false
    private var started = false
    private var suspendedForPairing = false
    private var lastDiagnosticWrite: TimeInterval = 0
    private var lastFailureCode: Int = 0
    private var failedRouteKey: String?

    private init() {
        selectedUID = UserDefaults.standard.string(forKey: "audioPhysicalUID") ?? ""
        enabled = UserDefaults.standard.bool(forKey: "audioBridgeEnabled")
    }

    func start() {
        guard !started else { return }; started = true
        observe(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices) { [weak self] in self?.refreshHardware() }
        observe(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice) { [weak self] in self?.reconcileRoute() }
        TVController.shared.$configuration.sink { [weak self] value in
            Task { @MainActor in self?.configurationChanged(value) }
        }.store(in: &subscriptions)
        TVController.shared.$isPairing.sink { [weak self] busy in
            Task { @MainActor in
                guard let self else { return }
                // Pairing stores a fresh credential; avoid concurrent keychain work.
                self.suspendedForPairing = busy
                if busy { self.disconnectMonitor(); self.cancelRoute(restore: true) } else { self.beginMonitor() }
            }
        }.store(in: &subscriptions)
        let center = NSWorkspace.shared.notificationCenter
        observers.append(center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.waitingForHDMIAfterWake = self.virtual?.device == AudioHardware.defaultOutput()
                self.sleeping = true; self.disconnectMonitor(); self.cancelRoute(restore: false)
            }
        })
        observers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.sleeping = false; self.refreshHardware(); self.beginMonitor()
            }
        })
        refreshHardware()
    }

    func setSelectedOutput(_ uid: String) {
        guard uid != selectedUID, outputs.contains(where: { $0.id == uid }) else { return }
        cancelRoute(restore: true)
        selectedUID = uid; UserDefaults.standard.set(uid, forKey: "audioPhysicalUID")
        reconcileRoute()
    }

    func setEnabled(_ value: Bool) {
        failedRouteKey = nil
        if !value { waitingForHDMIAfterWake = false }
        enabled = value; UserDefaults.standard.set(value, forKey: "audioBridgeEnabled")
        if value { refreshHardware(); beginMonitor(); reconcileRoute() }
        else { cancelRoute(restore: true); message = "系统音量桥接已关闭。" }
    }

    func refreshHardware(retry: Bool = false) {
        let all = AudioHardware.outputs()
        let previous = outputs
        outputs = all.filter(\.isHDMI)
        let found = all.first { $0.id == AudioHardware.virtualUID }
        if retry || previous != outputs || virtual?.device != found?.device { failedRouteKey = nil }
        if virtual?.device != found?.device {
            removeVirtualListener()
            virtual = found
            if let found {
                lastRevision = (try? AudioHardware.state(found.device)["revision"] as? NSNumber)?.uint64Value ?? 0
                observe(found.device, AudioHardware.intent) { [weak self] in self?.nativeIntent() }
            }
        }
        driverInstalled = found != nil
        // A preselected suggestion is not persisted until the user enables this feature.
        if selectedUID.isEmpty, let current = outputs.first(where: { $0.device == AudioHardware.defaultOutput() }) { selectedUID = current.id }
        reconcileRoute()
    }

    func requestVolume(_ scalar: Double) {
        guard connected, let state, let volume = try? state.requestedVolume(scalar: scalar) else { return }
        enqueue(volume: volume, muted: state.muted)
    }
    func toggleMute() {
        guard connected, let state else { return }
        enqueue(volume: state.volume, muted: !state.muted)
    }

    func shutdown() {
        disconnectMonitor(); cancelRoute(restore: true)
        for (object, var address, block) in listeners { AudioObjectRemovePropertyListenerBlock(object, &address, .main, block) }
        listeners.removeAll()
        persistDiagnostics(force: true)
    }

    func exportDiagnostics() {
        persistDiagnostics(force: true)
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "LG-TV-audio-diagnostics.json"
        panel.allowedContentTypes = [.json]
        guard panel.runModal() == .OK, let url = panel.url,
              let data = try? JSONSerialization.data(withJSONObject: diagnostics(), options: [.prettyPrinted, .sortedKeys]) else { return }
        try? data.write(to: url, options: .atomic)
    }

    private func diagnostics() -> [String: Any] {
        let stats = LGTVGetRouteStats(), current = AudioHardware.defaultOutput()
        let route = current == virtual?.device ? "virtual" : (outputs.first(where: { $0.id == selectedUID })?.device == current ? "selected_hdmi" : "other")
        return ["schema": 1, "timestamp": ISO8601DateFormatter().string(from: Date()),
                "app_build": Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "unknown",
                "bridge_enabled": enabled, "driver_loaded": driverInstalled,
                "tv_audio_authorized": configuration?.audioControlGranted == true,
                "tv_audio_connected": connected, "forwarding": routing, "default_route": route,
                "tv_volume": state?.volume ?? -1, "tv_muted": state?.muted ?? true,
                "io_callbacks": stats.callbacks, "forwarded_frames": stats.routedFrames,
                "route_stage": forwarder.stage, "route_input_channels": forwarder.routeInputChannels,
                "route_output_channels": forwarder.routeOutputChannels, "route_subdevices": forwarder.routeSubdevices,
                "virtual_input_permission": forwarder.inputPermission,
                "nonzero_samples": stats.nonzeroSamples, "last_error_code": lastFailureCode,
                "physical_audio_confirmed": false]
    }

    private func persistDiagnostics(force: Bool = false) {
        let now = ProcessInfo.processInfo.systemUptime
        guard force || now - lastDiagnosticWrite >= 2 else { return }
        lastDiagnosticWrite = now
        guard let data = try? JSONSerialization.data(withJSONObject: diagnostics(), options: [.prettyPrinted, .sortedKeys]) else { return }
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("LGTVMenu")
        let url = directory.appendingPathComponent("audio-diagnostics.json")
        try? data.write(to: url, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private func configurationChanged(_ value: TVConfiguration?) {
        guard value != configuration else { return }
        disconnectMonitor(); cancelRoute(restore: true)
        configuration = value
        beginMonitor()
    }

    private func beginMonitor() {
        guard monitorTask == nil, !sleeping, !suspendedForPairing,
              let configuration, configuration.audioControlGranted == true else { return }
        monitorTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                do {
                    guard let key = try await TVKeychain.read(configuration.keychainAccount) else { throw TVError.pairingRequired }
                    let client = try WebOSClient(configuration: configuration, clientKey: key, pairing: false, audioControl: true, persistent: true)
                    monitor = client
                    try await client.connect(pairing: false)
                    let initial = try await client.audioState()
                    try Task.checkCancellation()
                    accept(initial)
                    connected = true
                    failedRouteKey = nil
                    persistDiagnostics(force: true)
                    log.notice("audio_status_connected")
                    reconcileRoute()
                    try await client.monitorAudio { [weak self] payload in
                        guard let self, let update = TVAudioState.parse(payload, previous: self.state) else { return }
                        self.accept(update)
                    }
                } catch is CancellationError { break }
                catch {
                    guard !Task.isCancelled else { break }
                    log.notice("audio_status_disconnected code=\((error as NSError).code, privacy: .public)")
                    connected = false; message = error.localizedDescription
                    lastFailureCode = (error as NSError).code
                    cancelRoute(restore: true)
                }
                monitor?.close(); monitor = nil
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { break }
            }
        }
    }

    private func disconnectMonitor() {
        monitorTask?.cancel(); monitorTask = nil; monitor?.close(); monitor = nil
        writeID = nil; writeTask?.cancel(); writeTask = nil; writer?.close(); writer = nil; pending = nil
        connected = false; report(ready: false)
        persistDiagnostics(force: true)
    }

    private func accept(_ update: TVAudioState) {
        state = update
        persistDiagnostics()
        // A stream of older ACKs must not jump a slider back while a newer drag is queued.
        if pending == nil && writeTask == nil { report(ready: routing && connected && update.adjustable) }
    }

    private func report(ready: Bool) {
        guard let virtual else { return }
        let volume = state.map { Int(($0.scalar * 100).rounded()) } ?? 0
        try? AudioHardware.report(virtual.device, volume: volume, muted: state?.muted ?? true, ready: ready)
    }

    private func nativeIntent() {
        guard enabled, routing, connected, let state, let virtual,
              AudioHardware.defaultOutput() == virtual.device,
              let value = try? AudioHardware.state(virtual.device),
              let revision = (value["revision"] as? NSNumber)?.uint64Value,
              revision != lastRevision else { return }
        lastRevision = revision
        guard let volume = value["volume"] as? Int, (0...100).contains(volume),
              let muted = value["muted"] as? Bool,
              let requested = try? state.requestedVolume(scalar: Double(volume)/100) else { return }
        enqueue(volume: requested, muted: muted)
    }

    private func enqueue(volume: Int, muted: Bool) {
        pending = (volume, muted)
        guard writeTask == nil, let configuration, connected else { return }
        let id = UUID(); writeID = id
        writeTask = Task {
            defer {
                if writeID == id {
                    // Do not reuse an idle write socket. TV firmware can close it silently.
                    writer?.close(); writer = nil; writeTask = nil; writeID = nil
                    report(ready: routing && connected && state?.adjustable == true)
                }
            }
            do {
                // Coalesce drags and held media keys without creating an unbounded TV queue.
                try await Task.sleep(nanoseconds: 80_000_000)
                while var target = pending {
                    pending = nil
                    for attempt in 0..<2 {
                        do {
                            if writer == nil {
                                guard let key = try await TVKeychain.read(configuration.keychainAccount) else { throw TVError.pairingRequired }
                                let client = try WebOSClient(configuration: configuration, clientKey: key, pairing: false, audioControl: true, persistent: true)
                                writer = client; try await client.connect(pairing: false)
                            }
                            guard let writer else { return }
                            if target.volume != state?.volume { try await writer.setVolume(target.volume) }
                            if target.muted != state?.muted { try await writer.setMuted(target.muted) }
                            let update = try await writer.audioState()
                            try Task.checkCancellation()
                            guard writeID == id else { return }
                            accept(update)
                            message = routing ? "系统声音滑块和音量键已连接电视。" : "已读取电视音量。"
                            break
                        } catch {
                            try Task.checkCancellation()
                            writer?.close(); writer = nil
                            if let failure = error as? TVError, failure != .timeout && failure != .networkUnavailable { throw error }
                            guard attempt == 0 else { throw error }
                            // Absolute volume/mute are idempotent. Reconnect once, keeping the
                            // newest queued intent even if the earlier ACK was ambiguous.
                            target = pending ?? target; pending = nil
                            message = "正在重新连接电视音量…"
                        }
                    }
                    log.notice("audio_command_readback volume=\(self.state?.volume ?? -1, privacy: .public) muted=\(self.state?.muted ?? false, privacy: .public)")
                    if pending != nil { try await Task.sleep(nanoseconds: 80_000_000) }
                }
            } catch {
                guard writeID == id else { return }
                pending = nil; writer?.close(); writer = nil
                message = "暂时无法调整电视音量，请检查电视与本机的局域网连接。"
                // Restore the reported TV state after a rejected command.
            }
        }
    }

    private func reconcileRoute() {
        guard !sleeping else { return }
        guard enabled else { return }
        guard let virtual else { message = AudioBridgeError.missingDriver.localizedDescription; return }
        guard let physical = outputs.first(where: { $0.id == selectedUID }) else {
            // The TV is also the display: its HDMI endpoint can reappear after Mac wake.
            // Retain the user's virtual selection during that interval. An explicit
            // switch to any other output takes precedence immediately.
            if waitingForHDMIAfterWake, AudioHardware.defaultOutput() == virtual.device {
                message = "等待电视 HDMI 音频恢复…"; return
            }
            waitingForHDMIAfterWake = false
            cancelRoute(restore: true); message = AudioBridgeError.missingOutput.localizedDescription; return
        }
        let current = AudioHardware.defaultOutput()
        guard current == virtual.device || current == physical.device else {
            waitingForHDMIAfterWake = false
            cancelRoute(restore: false); message = "当前使用其他声音输出；选回 \(physical.name) 后自动接管。"; return
        }
        guard connected, state?.adjustable == true else {
            if routing { cancelRoute(restore: true) }
            message = configuration?.audioControlGranted == true ? "等待电视音量状态…" : "请先授权电视音量控制。"
            return
        }
        guard !routing, routeTask == nil else { return }
        let topology = "\(virtual.device):\(physical.device)"
        // Creating/destroying our private aggregate itself emits hardware-list changes.
        // A failed configuration must not turn those callbacks into an unlimited retry loop.
        guard failedRouteKey != topology else { return }
        message = "正在建立本机音频路由…"
        persistDiagnostics(force: true)
        let id = UUID(); routeID = id
        routeTask = Task {
            defer { if routeID == id { routeTask = nil; routeID = nil } }
            do {
                // The driver remains read-only until the TV's current value has been published.
                report(ready: false)
                try await forwarder.start(physical: physical, virtual: virtual)
                try Task.checkCancellation()
                guard enabled, connected, !sleeping,
                      [physical.device, virtual.device].contains(AudioHardware.defaultOutput()) else {
                    forwarder.stop(); return
                }
                UserDefaults.standard.set(selectedUID, forKey: "audioPhysicalUID")
                try AudioHardware.select(virtual.device)
                lastRevision = (try? AudioHardware.state(virtual.device)["revision"] as? NSNumber)?.uint64Value ?? 0
                routing = true; report(ready: true)
                waitingForHDMIAfterWake = false
                persistDiagnostics(force: true)
                message = "系统声音滑块和音量键已连接电视。"
                log.notice("audio_route_started")
            } catch {
                guard routeID == id else { return }
                failedRouteKey = topology
                cancelRoute(restore: true); message = error.localizedDescription
                lastFailureCode = (error as NSError).code
                persistDiagnostics(force: true)
                log.notice("audio_route_failed stage=\(self.forwarder.stage, privacy: .public) code=\((error as NSError).code, privacy: .public)")
            }
        }
    }

    private func cancelRoute(restore: Bool) {
        routeID = nil
        routeTask?.cancel(); routeTask = nil
        report(ready: false)
        if restore, let virtual, AudioHardware.defaultOutput() == virtual.device {
            let all = AudioHardware.outputs()
            if let target = all.first(where: { $0.id == selectedUID }) ?? all.first(where: { $0.transport == kAudioDeviceTransportTypeBuiltIn }) {
                try? AudioHardware.select(target.device)
            }
        }
        forwarder.stop(); routing = false
        persistDiagnostics(force: true)
    }

    private func observe(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, action: @escaping @MainActor () -> Void) {
        var address = AudioHardware.address(selector)
        let block: AudioObjectPropertyListenerBlock = { _, _ in Task { @MainActor in action() } }
        if AudioObjectAddPropertyListenerBlock(object, &address, .main, block) == 0 { listeners.append((object, address, block)) }
    }
    private func removeVirtualListener() {
        for (object, var address, block) in listeners where address.mSelector == AudioHardware.intent {
            AudioObjectRemovePropertyListenerBlock(object, &address, .main, block)
        }
        listeners.removeAll { $0.1.mSelector == AudioHardware.intent }
    }
}
