import AppKit
import Foundation

// Compile alongside the exact ServiceManager.swift and DesktopStatus.swift.
// These local types shadow only the process/client/workspace boundaries in this
// probe module. No App, service, socket, authorization browser or child runs.
@MainActor
final class Process {
    static var latest: Process?
    static var nextPID: Int32 = 41000
    var executableURL: URL?
    var arguments: [String]?
    var environment: [String: String]?
    var currentDirectoryURL: URL?
    var standardInput: Any?
    var standardOutput: Any?
    var standardError: Any?
    var terminationHandler: ((Process) -> Void)?
    var isRunning = false
    var terminationStatus: Int32 = 0
    let processIdentifier: Int32
    init() { Self.nextPID += 1; processIdentifier = Self.nextPID; Self.latest = self }
    func run() throws { isRunning = true }
    func terminate() { isRunning = false }
}

@MainActor
final class NSWorkspace {
    static let shared = NSWorkspace()
    private(set) var opened = 0
    func open(_ url: URL) -> Bool { opened += 1; return true }
}

enum FridayPaths {
    static let base = FileManager.default.temporaryDirectory.appendingPathComponent("Friday-state-probe-" + UUID().uuidString)
    static let product = base
    static let resources = base
    static let environment: [String: String] = [:]
    static let authorization = URL(string: "http://synthetic.invalid/authorization")!
}

@MainActor
struct DesktopClient {
    struct Pending {
        let path: String
        let continuation: CheckedContinuation<DesktopStatus, Error>
    }
    static var pending: [Pending] = []
    func request(_ path: String, method: String = "GET") async throws -> DesktopStatus {
        try await withCheckedThrowingContinuation { continuation in
            Self.pending.append(Pending(path: path, continuation: continuation))
        }
    }
    static func finish(_ result: Result<DesktopStatus, Error>) { finish(0, result) }
    static func finish(_ index: Int, _ result: Result<DesktopStatus, Error>) {
        pending.remove(at: index).continuation.resume(with: result)
    }
}

@main
struct NativeStateConsumerProbe {
    @MainActor static func expect(_ value: @autoclosure () -> Bool, _ reason: String) {
        precondition(value(), reason)
    }
    @MainActor static func waiting(_ count: Int) async {
        for _ in 0..<4000 {
            if DesktopClient.pending.count == count { return }
            await Task.yield()
        }
        preconditionFailure("Pending request count did not settle")
    }
    @MainActor static func report(_ connection: String = "connected", failure: String? = nil,
                                  revision: String = "r0", code: Int? = nil) -> DesktopStatus {
        DesktopStatus(service: "running", connection: connection, failure: failure,
            failureEvidence: code.map { HAFailureEvidence(httpReads: [HAHTTPRead(status: $0)]) },
            authorizationRevision: revision, source: "ha-read-only", entityCount: 3,
            deviceWrites: "disabled", simulated: false, pid: Process.latest!.processIdentifier,
            configurationAvailable: true, credentialAvailable: true, networkHost: "synthetic.invalid",
            networkPort: 8123, arl: ARLStatus(status: "not_checked", sdkVersion: "0.1.1", ready: false, inferenceAttempted: false))
    }
    @MainActor static func start(_ service: ServiceManager) async {
        let task = Task { await service.start() }
        await waiting(1)
        DesktopClient.finish(.failure(CocoaError(.fileReadUnknown)))
        await waiting(1)
        DesktopClient.finish(.success(report()))
        await task.value
        expect(service.phase == .running && service.statusReadAvailable, "Exact manager readiness")
    }
    @MainActor static func stop(_ service: ServiceManager) async {
        Process.latest!.isRunning = false
        let stopped = await service.stop()
        expect(stopped, "Memory child reaped")
    }
    @MainActor static func main() async {
        let service = ServiceManager()
        await start(service)

        // Older successful reads cannot replace a newer read failure.
        let first = Task { await service.refresh() }
        await waiting(1)
        let second = Task { await service.refresh() }
        await waiting(2)
        DesktopClient.finish(1, .failure(CocoaError(.fileReadUnknown)))
        await second.value
        expect(!service.statusReadAvailable, "Newer failure visible")
        let failureMessage = service.message
        DesktopClient.finish(.success(report()))
        await first.value
        expect(!service.statusReadAvailable && service.message == failureMessage, "Old success discarded")
        expect(service.status?.connection == "connected", "Cached Console/draft owner retained while read is unknown")

        // A newer authoritative HA failure also wins over an older connected DTO.
        let third = Task { await service.refresh() }
        await waiting(1)
        let fourth = Task { await service.refresh() }
        await waiting(2)
        DesktopClient.finish(1, .success(report("failed", failure: "ha_authorization_rejected", code: 403)))
        await fourth.value
        DesktopClient.finish(.success(report()))
        await third.value
        expect(service.status?.failure == "ha_authorization_rejected", "Reverse HA completion fenced")

        // Old catch and defer cannot reset a new child's in-flight operation.
        let oldConnect = Task { await service.connectHome() }
        await waiting(1)
        let oldContext = service.requestContext
        await stop(service)
        let oldRequest = DesktopClient.pending.removeFirst()
        await start(service)
        let newConnect = Task { await service.connectHome() }
        await waiting(1)
        oldRequest.continuation.resume(throwing: CocoaError(.fileReadUnknown))
        await oldConnect.value
        expect(service.connecting && service.statusReadAvailable && service.message == nil, "Old connect catch/defer fenced")
        DesktopClient.finish(.success(report()))
        await newConnect.value
        expect(!service.connecting && service.status?.connection == "connected", "New connect owns completion")
        await service.connectHome(ifCurrent: oldContext)
        expect(DesktopClient.pending.isEmpty, "Late permission completion fenced")

        let oldDisconnect = Task { await service.disconnectHome() }
        await waiting(1)
        await stop(service)
        let staleDisconnect = DesktopClient.pending.removeFirst()
        await start(service)
        let newDisconnect = Task { await service.disconnectHome() }
        await waiting(1)
        staleDisconnect.continuation.resume(throwing: CocoaError(.fileReadUnknown))
        await oldDisconnect.value
        expect(service.connecting && service.statusReadAvailable && service.message == nil, "Old disconnect catch/defer fenced")
        DesktopClient.finish(.success(report("not_connected")))
        await newDisconnect.value
        expect(!service.connecting && service.status?.connection == "not_connected", "New disconnect owns completion")

        // Actual authorization revision consumer reconnects only in its lifecycle.
        service.openHomeAuthorization()
        expect(service.awaitingAuthorization, "Memory authorization opened")
        let authorized = Task { await service.refresh() }
        await waiting(1)
        DesktopClient.finish(.success(report("not_connected", revision: "r1")))
        await waiting(1)
        expect(DesktopClient.pending[0].path == "desktop/connect", "Fresh revision consumed")
        DesktopClient.finish(.success(report(revision: "r1")))
        await authorized.value
        expect(!service.awaitingAuthorization && !service.connecting, "Authorization settled")
        service.openHomeAuthorization()
        let staleAuthorization = Task { await service.refresh() }
        await waiting(1)
        await stop(service)
        let staleRead = DesktopClient.pending.removeFirst()
        await start(service)
        staleRead.continuation.resume(returning: report("failed", failure: "ha_authorization_rejected", revision: "r2"))
        await staleAuthorization.value
        expect(!service.awaitingAuthorization && DesktopClient.pending.isEmpty && service.status?.connection == "connected", "Old authorization discarded")

        // The same presentation is used by ContentView and ConnectionView.
        let denied = HomeConnectionPresentation(status: report("failed", failure: "ha_authorization_rejected", code: 401), readable: true, phase: .running)
        expect(denied.header == "需要授权" && denied.headerDetail == "家庭连接需要重新授权", "Separate native short name/description")
        expect(denied.needsAuthorization && denied.detail.contains("HTTP 401"), "Captured rejection preserved")
        let unavailable = HomeConnectionPresentation(status: report("failed", failure: "ha_authorization_rejected"), readable: true, phase: .running)
        expect(unavailable.detail.contains("家庭授权暂不可用") && !unavailable.detail.contains("拒绝了现有授权"), "No invented HTTP rejection")
        let unknown = HomeConnectionPresentation(status: report("failed", failure: "ha_authorization_rejected"), readable: false, phase: .running)
        expect(unknown.unknown && !unknown.needsAuthorization && unknown.header == "连接状态待核对", "Cached authorization is not current")
        let cached = HomeConnectionPresentation(status: report(), readable: false, phase: .running)
        expect(cached.unknown && cached.retainsConsole && cached.header != "家庭已连接", "Console retained with current-state warning")
        let timeout = HomeConnectionPresentation(status: report("failed", failure: "ha_read_timeout"), readable: true, phase: .running)
        expect(timeout.title == "读取超时" && timeout.titleDetail == "家庭状态读取超时", "Separate timeout title/description")
        await stop(service)
        expect(DesktopClient.pending.isEmpty && !service.connecting && !service.awaitingAuthorization, "No pending memory work")
        try? FileManager.default.removeItem(at: FridayPaths.base)
        print("NativeStateConsumerProbe: finite synthetic consumer checks passed")
    }
}
