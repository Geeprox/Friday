import AppKit
import Foundation
import OSLog
import ServiceManagement

/// The application is a client of the household service, never its lifetime owner.
@MainActor
final class ServiceManager: ObservableObject {
    static let shared = ServiceManager()
    @Published var phase: ServicePhase = .stopped
    @Published var status: DesktopStatus?
    @Published var statusReadAvailable = false
    @Published var message: String?
    @Published var connecting = false
    @Published var awaitingAuthorization = false
    private var authorizationRevisionBeforeOpening: String?
    private var lifecycleRevision: UInt64 = 0
    private var statusRevision: UInt64 = 0
    private var refreshTask: Task<Void, Never>?
    private let client = DesktopClient()
    private let logger = Logger(subsystem: "com.friday.home.desktop", category: "lifecycle")
    private(set) var servicePID: Int32?

    var requestContext: ServiceRequestContext {
        ServiceRequestContext(lifecycle: lifecycleRevision, request: statusRevision)
    }

    private func invalidateRequests() {
        refreshTask?.cancel()
        refreshTask = nil
        lifecycleRevision &+= 1
        statusRevision &+= 1
        connecting = false
        awaitingAuthorization = false
    }

    private func owned(_ report: DesktopStatus) -> Bool {
        report.pid == HouseholdService.pid() && report.lifetime == "persistent"
            && !report.simulated && ["disabled", "scoped"].contains(report.deviceWrites)
    }

    func start(userInitiated: Bool = false) async {
        guard phase != .starting, phase != .stopping else { return }
        if !userInitiated && UserDefaults.standard.bool(forKey: "householdServicePaused") {
            phase = .stopped
            return
        }
        invalidateRequests()
        let lifecycle = lifecycleRevision
        phase = .starting
        message = nil
        logger.notice("stage=start result=start")
        do {
            let agent = HouseholdService.agent
            if agent.status != .enabled {
                do { try agent.register() }
                catch { if agent.status != .requiresApproval { throw error } }
            }
            guard agent.status == .enabled else {
                phase = .failed
                message = agent.status == .requiresApproval
                    ? "请在系统设置的登录项中允许 Friday 后台运行。"
                    : "后台服务注册未完成，请重新启动。"
                logger.notice("stage=start result=refused registration=\(agent.status.rawValue)")
                return
            }
            UserDefaults.standard.set(false, forKey: "householdServicePaused")
            for _ in 0..<80 {
                guard lifecycleRevision == lifecycle else { return }
                if let report = try? await client.request("desktop/status"), owned(report) {
                    servicePID = report.pid
                    status = report
                    statusReadAvailable = true
                    phase = .running
                    logger.notice("stage=start result=ok")
                    refreshTask = Task { [weak self] in
                        while !Task.isCancelled {
                            try? await Task.sleep(nanoseconds: 3_000_000_000)
                            guard !Task.isCancelled, let self else { return }
                            await self.refresh()
                        }
                    }
                    return
                }
                try await Task.sleep(nanoseconds: 150_000_000)
            }
            throw CocoaError(.executableRuntimeMismatch)
        } catch {
            guard lifecycleRevision == lifecycle else { return }
            phase = .failed
            statusReadAvailable = false
            message = "家庭服务暂不可用，请查看运行记录。"
            logger.notice("stage=start result=error code=startupFailed")
        }
    }

    func refresh() async {
        guard phase == .running, !connecting else { return }
        let lifecycle = lifecycleRevision
        statusRevision &+= 1
        let request = statusRevision
        let report = try? await client.request("desktop/status")
        guard lifecycle == lifecycleRevision, request == statusRevision else { return }
        if let report, owned(report) {
            servicePID = report.pid
            status = report
            statusReadAvailable = true
            message = nil
            if awaitingAuthorization, let revision = report.authorizationRevision,
               revision != authorizationRevisionBeforeOpening {
                awaitingAuthorization = false
                await connectHome()
            }
        } else {
            statusReadAvailable = false
            message = "家庭服务正在恢复连接。"
        }
    }

    func openHomeAuthorization() {
        guard phase == .running, !connecting else { return }
        authorizationRevisionBeforeOpening = status?.authorizationRevision
        awaitingAuthorization = NSWorkspace.shared.open(FridayPaths.authorization)
        if !awaitingAuthorization { message = "授权页面暂时无法打开。" }
    }

    func connectHome(ifCurrent context: ServiceRequestContext? = nil) async {
        if let context, context != requestContext { return }
        await changeConnection("connect")
    }

    func disconnectHome() async { await changeConnection("disconnect") }

    private func changeConnection(_ operation: String) async {
        guard phase == .running, !connecting else { return }
        let lifecycle = lifecycleRevision
        statusRevision &+= 1
        let request = statusRevision
        connecting = true
        message = nil
        defer { if lifecycleRevision == lifecycle { connecting = false } }
        do {
            let report = try await client.request("desktop/" + operation, method: "POST")
            guard lifecycleRevision == lifecycle, statusRevision == request else { return }
            guard owned(report) else { throw CocoaError(.executableRuntimeMismatch) }
            status = report
            statusReadAvailable = true
            logger.notice("stage=connection result=ok")
        } catch {
            guard lifecycleRevision == lifecycle, statusRevision == request else { return }
            statusReadAvailable = false
            message = "连接操作未完成，请重新查看状态。"
            logger.notice("stage=connection result=error")
        }
    }

    /// Explicit stop unregisters the login agent. Quitting the GUI never calls it.
    func stop() async -> Bool {
        guard phase != .stopping else { return false }
        invalidateRequests()
        phase = .stopping
        UserDefaults.standard.set(true, forKey: "householdServicePaused")
        let previousPID = HouseholdService.pid() ?? servicePID
        do {
            try await HouseholdService.agent.unregister()
            for _ in 0..<1200 {
                if HouseholdService.pid() == nil && !(previousPID.map(HouseholdService.processExists) ?? false) {
                    phase = .stopped
                    message = nil
                    status = nil
                    servicePID = nil
                    statusReadAvailable = false
                    logger.notice("stage=stop result=ok")
                    return true
                }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
        } catch { logger.notice("stage=stop result=error") }
        message = "家庭服务仍在收尾，请查看运行记录。"
        phase = .failed
        return false
    }

    func stopForApplicationQuit() -> Bool {
        invalidateRequests()
        logger.notice("stage=quit result=ok householdService=retained")
        return true
    }

    func openLogs() {
        NSWorkspace.shared.open(FridayPaths.base.appendingPathComponent("logs", isDirectory: true))
    }
}
