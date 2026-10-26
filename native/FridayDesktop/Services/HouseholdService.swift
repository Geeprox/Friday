import Darwin
import Foundation
import ServiceManagement

/// launchd owns the same signed executable in service mode. The Python backend
/// replaces it with exec, retaining launchd's PID and signal/shutdown semantics.
enum HouseholdService {
    static let label = "com.friday.household.service"
    static var agent: SMAppService { .agent(plistName: label + ".plist") }

    static func runIfRequested() {
        if CommandLine.arguments.contains("--service-status") {
            let value: [String: Any] = ["registration": agent.status.rawValue, "pid": pid() as Any? ?? NSNull()]
            if let data = try? JSONSerialization.data(withJSONObject: value), let text = String(data: data, encoding: .utf8) { print(text) }
            exit(0)
        }
        guard CommandLine.arguments.contains("--household-service") else { return }
        guard let executable = Bundle.main.executableURL?.resolvingSymlinksInPath() else { exit(70) }
        let resources = executable.deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources")
        let python = resources.appendingPathComponent("product/runtime/bin/python3").path
        let backend = resources.appendingPathComponent("backend/desktop_backend.py").path
        let arguments = [python, "-I", "-B", backend, "--persistent"]
        setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin", 1)
        setenv("PYTHONDONTWRITEBYTECODE", "1", 1)
        let pointers = arguments.map { strdup($0) } + [nil]
        pointers.withUnsafeBufferPointer { buffer in _ = execv(python, buffer.baseAddress!) }
        exit(70)
    }

    static func processExists(_ pid: Int32) -> Bool {
        pid > 0 && (Darwin.kill(pid, 0) == 0 || errno == EPERM)
    }

    /// Verify OS ownership before attaching to loopback; a listening port alone
    /// never identifies Friday and is never grounds to terminate a process.
    static func pid() -> Int32? {
        let command = Process(), output = Pipe()
        command.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        command.arguments = ["list", label]
        command.standardOutput = output
        command.standardError = FileHandle.nullDevice
        do {
            try command.run()
            let bytes = output.fileHandleForReading.readDataToEndOfFile()
            command.waitUntilExit()
            guard command.terminationStatus == 0,
                  let text = String(data: bytes, encoding: .utf8),
                  let range = text.range(of: #"\"PID\"\s*=\s*[0-9]+"#, options: .regularExpression),
                  let value = text[range].split(separator: "=").last else { return nil }
            return Int32(value.trimmingCharacters(in: .whitespaces))
        } catch { return nil }
    }
}
