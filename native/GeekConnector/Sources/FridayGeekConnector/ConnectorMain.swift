import AppKit
import Darwin
import Foundation
import Network

// A named application responsible for the fixed read-only connector. It has
// no model, device-action, rule-deployment, or generic command IPC interface.
@MainActor
final class ConnectorApplication: NSObject, NSApplicationDelegate {
    private var child: Process?
    private var signals: [DispatchSourceSignal] = []
    private var stopping = false
    private var intentTimer: Timer?
    private var networkProbe: NWConnection?
    private var seenIntent: String?

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let root = Bundle.main.object(forInfoDictionaryKey: "FridayRuntimeRoot") as? String,
              let directory = Bundle.main.object(forInfoDictionaryKey: "FridayDataDirectory") as? String,
              let nodeDirectory = Bundle.main.object(forInfoDictionaryKey: "FridayNodeDirectory") as? String,
              root.hasPrefix("/"), directory.hasPrefix("/"), nodeDirectory.hasPrefix("/") else {
            finishAfterDelay()
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: root).appendingPathComponent(".venv/bin/python")
        process.arguments = ["-m", "homeplane.geek_daemon", "--directory", directory]
        process.currentDirectoryURL = URL(fileURLWithPath: root)
        process.environment = ["PATH": nodeDirectory + ":/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.stopping { exit(0) }
                self.finishAfterDelay()
            }
        }
        do {
            try process.run()
            child = process
        } catch {
            finishAfterDelay()
            return
        }
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { [weak self] in self?.stop() }
            source.resume()
            signals.append(source)
        }
        intentTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkNetworkPermission(directory: directory) }
        }
    }

    private func privateJSON(_ path: String, limit: Int) -> [String: Any]? {
        let descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_uid == getuid(),
              metadata.st_mode & 0o077 == 0, metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_size <= limit else { return nil }
        var buffer = [UInt8](repeating: 0, count: limit + 1)
        let count = read(descriptor, &buffer, buffer.count)
        guard count > 0, count <= limit else { return nil }
        return (try? JSONSerialization.jsonObject(with: Data(buffer.prefix(count)))) as? [String: Any]
    }

    private func checkNetworkPermission(directory: String) {
        guard !stopping else { return }
        guard let intent = privateJSON(directory + "/geek-connection-intent.json", limit: 2048),
              intent["keep_connected"] as? Bool == true,
              let saved = privateJSON(directory + "/settings.json", limit: 16384),
              let config = saved["config"] as? [String: Any],
              let address = intent["url"] as? String, address.count <= 512,
              config["geek_url"] as? String == address,
              let stamp = intent["credential_stamp"] as? String, stamp.count <= 128 else {
            seenIntent = nil
            networkProbe?.cancel()
            networkProbe = nil
            return
        }
        let key = address + "|" + stamp
        guard seenIntent != key, let url = URL(string: address),
              ["http", "https"].contains(url.scheme), url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, ["", "/"].contains(url.path),
              let host = url.host else { return }
        let octets = host.split(separator: ".").compactMap { Int($0) }
        guard octets.count == 4, octets.allSatisfy({ (0...255).contains($0) }),
              octets[0] == 10 || (octets[0] == 172 && (16...31).contains(octets[1])) || (octets[0] == 192 && octets[1] == 168),
              let port = NWEndpoint.Port(rawValue: UInt16(exactly: url.port ?? (url.scheme == "https" ? 443 : 80)) ?? 0),
              port.rawValue > 0 else { return }
        seenIntent = key
        // Native Network waits for the declared application's Local Network
        // privilege. Exact saved private endpoint only, no application bytes,
        // no PIN, no device command, and no network operation while stopped.
        let connection = NWConnection(host: NWEndpoint.Host(host), port: port, using: .tcp)
        networkProbe = connection
        connection.stateUpdateHandler = { state in
            if case .ready = state { connection.cancel() }
        }
        connection.start(queue: .global(qos: .utility))
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [weak self] in
            connection.cancel()
            if self?.networkProbe === connection { self?.networkProbe = nil }
        }
    }

    private func finishAfterDelay() {
        // Apple TN3179: very short-lived local-network callers may never show
        // the permission alert. The responsible application remains present.
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { exit(1) }
    }

    private func stop() {
        guard !stopping else { return }
        stopping = true
        intentTimer?.invalidate()
        networkProbe?.cancel()
        guard let child, child.isRunning else { exit(0) }
        child.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
            exit(0)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        stop()
        return .terminateLater
    }
}

@main
struct FridayConnectorMain {
    @MainActor
    static func main() {
        let application = NSApplication.shared
        let delegate = ConnectorApplication()
        application.delegate = delegate
        application.setActivationPolicy(.accessory)
        application.run()
    }
}
