import AppKit
import Combine
import Foundation
import LGTVCore
import OSLog
import ServiceManagement

@MainActor
final class TVController: ObservableObject {
    private let log = Logger(subsystem: "local.friday.LGTVMenu", category: "Power")
    static let shared = TVController()
    @Published private(set) var configuration: TVConfiguration?
    @Published private(set) var status = "尚未配对"
    @Published private(set) var detail = "请在设置中选择自己的电视。"
    @Published private(set) var isBusy = false
    @Published private(set) var isPairing = false
    @Published private(set) var isSearching = false
    @Published private(set) var discovered: [DiscoveredTV] = []
    @Published private(set) var automaticWake: Bool
    @Published private(set) var automaticLockOff: Bool
    @Published private(set) var loginEnabled = false
    @Published private(set) var loginNeedsApproval = false
    private let defaults: UserDefaults
    private var policy = WakePolicy()
    private var lockPolicy = LockPowerPolicy()
    private var observers: [NSObjectProtocol] = []
    private var lockObservers: [NSObjectProtocol] = []
    private var lockWatch: Task<Void, Never>?
    private var lockWatchID: UUID?
    private var operation: UUID?
    private var work: Task<Void, Never>?
    private var search: Task<Void, Never>?
    private var searchID: UUID?
    private var activeClient: WebOSClient?
    private var automaticOperation = false
    private var automaticShutdown = false
    private var systemSleeping = false
    private let systemSleepObserver = SystemSleepObserver()
    private var shutdownCompletions: [() -> Void] = []
    private var systemSleepOperation: UUID?

    private init() {
        defaults = UserDefaults.standard
        configuration = defaults.data(forKey: "television").flatMap {
            try? JSONDecoder().decode(TVConfiguration.self, from: $0)
        }
        automaticWake = defaults.bool(forKey: "automaticWake")
        automaticLockOff = defaults.bool(forKey: "automaticLockOff")
        if configuration != nil {
            status = "已保存配对"
            detail = "尚未读取电视当前状态。"
        }
        refreshLoginStatus()
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.willSleepNotification, NSWorkspace.didWakeNotification,
                     NSWorkspace.screensDidWakeNotification, NSWorkspace.screensDidSleepNotification] {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                Task { @MainActor in self?.handle(note.name) }
            })
        }
        for name in [ScreenActivity.locked, ScreenActivity.unlocked,
                     ScreenActivity.screensaverStarted, ScreenActivity.screensaverStopped] {
            lockObservers.append(DistributedNotificationCenter.default().addObserver(
                forName: name, object: nil, queue: .main) { [weak self] note in
                    Task { @MainActor in self?.handleLock(note.name) }
                })
        }
        let sleepObserverReady = systemSleepObserver.start(onSleep: { [weak self] completion in
            guard let self else { completion(); return }
            self.prepareForSystemSleep(completion: completion)
        }, onTimeout: { [weak self] in
            guard let self, self.systemSleeping,
                  let id = self.systemSleepOperation, self.operation == id else { return }
            self.log.notice("system_sleep_shutdown_deadline")
            self.cancelWork()
        })
        log.notice("system_sleep_observer_ready available=\(sleepObserverReady, privacy: .public)")
        log.notice("power_observers_ready lock_off=\(self.automaticLockOff, privacy: .public)")
    }

    func discover() {
        guard !isSearching && !isBusy else { return }
        let id = UUID()
        searchID = id
        isSearching = true
        search = Task {
            defer {
                if searchID == id { isSearching = false; search = nil; searchID = nil }
            }
            do {
                let results = try await TVDiscovery.scan()
                try Task.checkCancellation()
                guard searchID == id else { return }
                discovered = results
                if discovered.isEmpty {
                    detail = "未发现电视。请保持电视开机、连接同一局域网，并允许本地网络访问；也可手动填写地址。"
                }
            } catch is CancellationError { }
            catch { if searchID == id && !isBusy { detail = error.localizedDescription } }
        }
    }

    func pair(name: String, host: String, macAddress: String, useTLS: Bool, discoveryID: String?, audioControl: Bool = false) {
        guard !isBusy else { return }
        let address = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard TVConfiguration.isLANAddress(address) else { detail = TVError.invalidAddress.localizedDescription; return }
        let mac: String
        do { mac = macAddress.isEmpty ? "" : try MagicPacket.normalized(macAddress) }
        catch { detail = error.localizedDescription; return }
        searchID = nil
        search?.cancel()
        search = nil
        isSearching = false
        // Re-pairing is explicit and may trust a new certificate, never a background action.
        var candidate = TVConfiguration(id: discoveryID ?? UUID().uuidString,
                                        name: name.isEmpty ? "LG webOS TV" : String(name.prefix(120)),
                                        host: address, macAddress: mac, useTLS: useTLS,
                                        pairingKeyID: UUID().uuidString, audioControlGranted: audioControl)
        if audioControl, let previous = configuration, previous.id == candidate.id, previous.host == address {
            // Extending permissions on the paired TV must retain its established identity.
            candidate.certificateFingerprint = previous.certificateFingerprint
            candidate.deviceUUID = previous.deviceUUID
        }
        isPairing = true
        perform("正在连接电视", detail: "正在连接本地 webOS 接口，此操作不会开关电视。") { [self] in
            let client = try WebOSClient(configuration: candidate, clientKey: nil, pairing: true, audioControl: audioControl)
            activeClient = client
            defer { client.close() }
            try await client.connect(pairing: true) { [weak self] in
                self?.status = "等待电视允许配对"
                self?.detail = "电视返回配对等待状态；如屏幕出现连接许可，请用遥控器允许。"
            }
            if audioControl { _ = try await client.audioState() }
            var saved = client.configuration
            if saved.macAddress.isEmpty, let found = await client.networkMAC() { saved.macAddress = found }
            try Task.checkCancellation()
            guard let key = client.clientKey else { throw TVError.pairingRequired }
            // Preserve the older account and its ACL as recovery data. Only explicit
            // pairing creates a fresh credential account, independent of discovery ID.
            try await TVKeychain.save(key, id: saved.keychainAccount)
            guard try await TVKeychain.read(saved.keychainAccount) == key else { throw TVError.keychain }
            log.notice("pairing_credential_readback_verified")
            try Task.checkCancellation()
            configuration = saved
            persist()
            // The requested automatic behavior is enabled after successful pairing and NIC identification.
            setAutomaticWake(saved.canWake)
            return saved.canWake
                ? ("配对已保存", "已启用随 Mac 唤醒。请先测试菜单中的打开电视，确认电视允许网络开机。")
                : ("配对已保存", "可以关闭电视。请补充电视当前联网接口的 MAC 地址，再启用随 Mac 唤醒。")
        }
    }

    func authorizeAudio() {
        guard let saved = configuration else { return }
        pair(name: saved.name, host: saved.host, macAddress: saved.macAddress,
             useTLS: saved.useTLS, discoveryID: saved.id, audioControl: true)
    }

    func saveMAC(_ value: String) {
        guard var saved = configuration else { return }
        do {
            saved.macAddress = try MagicPacket.normalized(value)
            configuration = saved
            persist()
            detail = "MAC 地址已保存，可以测试打开电视。"
        } catch { detail = error.localizedDescription }
    }

    func turnOff(automatic: Bool = false, completion: (() -> Void)? = nil) {
        // This also cancels WOL retries queued before the user's explicit power-off.
        if !automatic { lockPolicy.suppress(); stopLockWatch() }
        cancelWork()
        if !systemSleeping { policy.suppressUntilNextSleep() }
        guard let saved = configuration else { completion?(); return }
        if let completion { shutdownCompletions.append(completion) }
        automaticShutdown = automatic
        let reason = automatic ? lockPolicy.lastShutdownReason?.rawValue ?? "automatic" : "manual"
        log.notice("power_off_started automatic=\(automatic, privacy: .public) reason=\(reason, privacy: .public)")
        perform("正在请求关机", automatic: automatic) { [self] in
            let client = try await authorizedClient(saved)
            defer { client.close() }
            try await client.connect(pairing: false)
            let acknowledged = try await client.turnOff()
            log.notice("power_off_request_completed automatic=\(automatic, privacy: .public) acknowledged=\(acknowledged, privacy: .public)")
            return acknowledged
                ? ("电视已接收关机请求", automatic
                   ? "再次动键鼠或解锁时会尝试打开电视，请以实际画面为准。"
                   : "请以电视实际画面为准。下次 Mac 睡眠后唤醒仍可自动开机。")
                : ("关机请求已发出", "连接随后中断，尚未确认执行结果，请以电视实际画面为准。")
        }
    }

    func wake(automatic: Bool = false) {
        guard !isBusy, let saved = configuration, saved.canWake else { return }
        lockPolicy.consumeRestoration()
        stopLockWatch()
        log.notice("wake_started automatic=\(automatic, privacy: .public)")
        perform("等待局域网恢复", detail: "即将向已选择的电视发送唤醒包。", automatic: automatic) { [self] in
            var sent = false
            var lastError: Error = TVError.networkUnavailable
            // Bounded retries allow Wi-Fi/Ethernet to recover after Mac sleep.
            for (index, delay) in [1, 2, 4, 8].enumerated() {
                try await Task.sleep(nanoseconds: UInt64(delay) * 1_000_000_000)
                try Task.checkCancellation()
                do {
                    log.notice("wake_attempt number=\(index + 1, privacy: .public)")
                    try LANTransport.wake(saved)
                    sent = true
                    status = "唤醒包已发送"
                    detail = "正在等电视回应；发包成功尚不能证明电视已开机。"
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                    log.notice("wake_verification_started number=\(index + 1, privacy: .public)")
                    let client = try await authorizedClient(saved)
                    log.notice("wake_verification_connecting number=\(index + 1, privacy: .public)")
                    defer { client.close() }
                    try await client.connect(pairing: false)
                    if try await client.reportsActive() {
                        log.notice("wake_active_reported")
                        return ("电视报告已开机", "本地接口报告 Active，请以实际画面为准。")
                    }
                } catch {
                    try Task.checkCancellation()
                    let failure = error as NSError
                    log.notice("wake_attempt_failed number=\(index + 1, privacy: .public) domain=\(failure.domain, privacy: .public) code=\(failure.code, privacy: .public)")
                    // Identity or credentials must be corrected, not retried indefinitely.
                    if let failure = error as? TVError,
                       [.identityChanged, .certificateChanged, .pairingRequired].contains(failure) { throw failure }
                    lastError = error
                }
            }
            if sent {
                log.notice("wake_unconfirmed")
                if lastError as? TVError == .keychain {
                    return ("唤醒包已发送，结果待确认", "配对钥匙串暂不可访问，仍已完成唤醒重试。解锁后可重新配对并读取状态。")
                }
                return ("唤醒包已发送，结果待确认", "尚未收到开机状态。请检查电视的网络开机 / Wake on LAN 设置和当前联网接口的 MAC 地址。")
            }
            throw lastError
        }
    }

    func readStatus() {
        guard !isBusy, let saved = configuration else { return }
        perform("正在读取电视状态") { [self] in
            let client = try await authorizedClient(saved)
            defer { client.close() }
            try await client.connect(pairing: false)
            let active = try await client.reportsActive()
            return active
                ? ("电视报告已开机", "已读取本地接口，未发送电源指令。")
                : ("电视未报告 Active", "已读取本地接口；待机、屏幕关闭与离线并不等同。")
        }
    }

    func setAutomaticWake(_ enabled: Bool) {
        guard !enabled || configuration?.canWake == true else { return }
        automaticWake = enabled
        defaults.set(enabled, forKey: "automaticWake")
        if !enabled {
            policy.suppressUntilNextSleep()
            if automaticOperation { cancelWork() }
        }
    }

    func setAutomaticLockOff(_ enabled: Bool) {
        guard !enabled || configuration?.canWake == true else { return }
        automaticLockOff = enabled
        defaults.set(enabled, forKey: "automaticLockOff")
        if !enabled {
            lockPolicy.suppress()
            stopLockWatch()
            if automaticShutdown { cancelWork() }
        }
        log.notice("lock_off_setting enabled=\(enabled, privacy: .public)")
    }

    func forget() {
        cancelWork()
        guard let saved = configuration else { return }
        perform("正在移除本机配对") { [self] in
            try await TVKeychain.delete(saved.keychainAccount)
            setAutomaticWake(false)
            setAutomaticLockOff(false)
            configuration = nil
            defaults.removeObject(forKey: "television")
            return ("尚未配对", "本机配对已移除；电视中的授权可在电视设置中撤销。")
        }
    }

    func refreshLoginStatus() {
        loginEnabled = SMAppService.mainApp.status == .enabled
        loginNeedsApproval = SMAppService.mainApp.status == .requiresApproval
    }

    func setLoginEnabled(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            refreshLoginStatus()
        } catch {
            refreshLoginStatus()
            detail = "登录启动设置未完成。请将应用放在应用程序目录，并在系统设置 → 通用 → 登录项中检查。"
        }
    }

    func cancelWork() {
        if isBusy {
            status = "操作已取消"
            detail = "尚未确认电视执行结果。"
        }
        operation = nil
        work?.cancel()
        work = nil
        activeClient?.close()
        activeClient = nil
        isBusy = false
        isPairing = false
        automaticOperation = false
        automaticShutdown = false
        finishShutdown()
    }

    func shutdown() {
        cancelWork(); stopLockWatch(); systemSleepObserver.stop()
    }

    private func finishShutdown() {
        let completions = shutdownCompletions
        shutdownCompletions.removeAll()
        systemSleepOperation = nil
        completions.forEach { $0() }
    }

    private func handle(_ name: Notification.Name) {
        log.notice("workspace_event name=\(name.rawValue, privacy: .public)")
        switch name {
        case NSWorkspace.willSleepNotification:
            // Also supplies best-effort handling if I/O Kit registration failed.
            // Never cancel an off request already running inside the sleep lease.
            prepareForSystemSleep(completion: {})
        case NSWorkspace.didWakeNotification:
            systemSleeping = false
            lockPolicy.systemDidWake()
            if policy.didWake() && automaticWake { wakeAutomatically() }
            startLockWatch()
        case NSWorkspace.screensDidWakeNotification:
            lockPolicy.displayDidWake()
            if policy.screensDidWake() && automaticWake { wakeAutomatically() }
            else { checkLockActivity() }
        case NSWorkspace.screensDidSleepNotification:
            policy.screensDidSleep()
            if automaticLockOff, configuration?.canWake == true, !isPairing {
                lockPolicy.displayDidSleep(at: ProcessInfo.processInfo.systemUptime)
                requestAutomaticShutdown()
            }
        default: break
        }
    }

    private func handleLock(_ name: Notification.Name) {
        log.notice("lock_event name=\(name.rawValue, privacy: .public)")
        if name == ScreenActivity.screensaverStarted {
            lockPolicy.screensaverStarted()
            if !lockPolicy.needsObservation { stopLockWatch() }
            log.notice("screensaver_state_started")
        } else if name == ScreenActivity.screensaverStopped {
            lockPolicy.screensaverStopped(at: ProcessInfo.processInfo.systemUptime)
            if automaticLockOff { startLockWatch() }
        } else if name == ScreenActivity.unlocked {
            let restore = lockPolicy.screenUnlocked()
            stopLockWatch()
            if restore && automaticLockOff { wakeAutomatically() }
        } else if automaticLockOff, configuration?.canWake == true, !isPairing, !systemSleeping {
            guard lockPolicy.screenLocked(at: ProcessInfo.processInfo.systemUptime) else {
                if lockPolicy.screensaverRunning { log.notice("lock_off_deferred_for_screensaver") }
                return
            }
            // A lock supersedes wake retries, but does not send any power command
            // until the delay expires and the session is still locked.
            cancelWork()
            policy.suppressUntilNextSleep()
            log.notice("lock_off_scheduled")
            log.notice("lock_activity_source available=\(ScreenActivity.idleSeconds() != nil, privacy: .public)")
            startLockWatch()
        }
    }

    private func startLockWatch() {
        guard lockWatch == nil, automaticLockOff, !systemSleeping, lockPolicy.needsObservation else { return }
        let id = UUID()
        lockWatchID = id
        lockWatch = Task { [weak self] in
            defer {
                if self?.lockWatchID == id { self?.lockWatch = nil; self?.lockWatchID = nil }
            }
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 500_000_000) } catch { break }
                guard let self, self.lockWatchID == id, !self.systemSleeping else { break }
                self.requestAutomaticShutdown()
                self.checkLockActivity()
                if !self.lockPolicy.needsObservation { break }
            }
        }
    }

    private func requestAutomaticShutdown(completion: (() -> Void)? = nil) {
        guard automaticLockOff, configuration?.canWake == true, !isPairing else { completion?(); return }
        guard lockPolicy.beginShutdown(at: ProcessInfo.processInfo.systemUptime) else { completion?(); return }
        turnOff(automatic: true, completion: completion)
        if !systemSleeping { startLockWatch() }
    }

    private func prepareForSystemSleep(completion: @escaping () -> Void) {
        let wasPairing = isPairing
        if !systemSleeping {
            systemSleeping = true
            stopLockWatch()
            policy.willSleep()
            if !automaticShutdown { cancelWork() }
        }
        guard automaticLockOff, configuration?.canWake == true, !wasPairing else { completion(); return }
        lockPolicy.systemWillSleep(at: ProcessInfo.processInfo.systemUptime)
        if automaticShutdown && isBusy {
            shutdownCompletions.append(completion)
        } else {
            requestAutomaticShutdown(completion: completion)
        }
        if automaticShutdown { systemSleepOperation = operation }
    }

    private func checkLockActivity() {
        guard automaticLockOff, !systemSleeping,
              lockPolicy.userActivity(at: ProcessInfo.processInfo.systemUptime,
                                      idleSeconds: ScreenActivity.idleSeconds()) else { return }
        log.notice("lock_restore_requested reason=user_activity")
        wakeAutomatically()
    }

    private func stopLockWatch() {
        lockWatchID = nil
        lockWatch?.cancel()
        lockWatch = nil
    }

    private func wakeAutomatically() {
        guard !isPairing, configuration?.canWake == true else { return }
        if automaticOperation && !automaticShutdown { return }
        cancelWork()
        wake(automatic: true)
    }

    private func authorizedClient(_ saved: TVConfiguration) async throws -> WebOSClient {
        guard let key = try await TVKeychain.read(saved.keychainAccount) else { throw TVError.pairingRequired }
        let client = try WebOSClient(configuration: saved, clientKey: key, pairing: false)
        activeClient = client
        return client
    }

    private func persist() {
        if let configuration, let bytes = try? JSONEncoder().encode(configuration) {
            defaults.set(bytes, forKey: "television")
        }
    }

    private func perform(_ title: String, detail message: String = "", automatic: Bool = false,
                         action: @escaping @MainActor () async throws -> (String, String)) {
        guard !isBusy else { return }
        let id = UUID()
        operation = id
        isBusy = true
        automaticOperation = automatic
        status = title
        detail = message
        work = Task {
            defer {
                if operation == id {
                    isBusy = false; isPairing = false; automaticOperation = false
                    automaticShutdown = false; activeClient = nil; work = nil
                    finishShutdown()
                }
            }
            do {
                let (title, message) = try await action()
                try Task.checkCancellation()
                guard operation == id else { return }
                status = title; detail = message
            } catch {
                guard operation == id else { return }
                status = "操作未完成"
                if let networkError = error as? URLError,
                   [.timedOut, .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet].contains(networkError.code) {
                    detail = TVError.timeout.localizedDescription
                } else {
                    detail = error.localizedDescription
                }
            }
        }
    }
}
