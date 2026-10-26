import AppKit
import Darwin
import Foundation

@MainActor
final class FridayDelegate: NSObject, NSApplicationDelegate {
    private var lock: Int32 = -1
    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: FridayPaths.base, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
        lock = Darwin.open(FridayPaths.base.appendingPathComponent("native-app.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard lock >= 0, flock(lock, LOCK_EX | LOCK_NB) == 0 else {
            for app in NSRunningApplication.runningApplications(withBundleIdentifier: "com.friday.home.desktop")
                where app.processIdentifier != getpid() {
                app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
            }
            exit(0)
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        Task {
            await ServiceManager.shared.start()
            if CommandLine.arguments.contains("--self-check") { await self.selfCheck() }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { NSApp.windows.first?.makeKeyAndOrderFront(nil) }
        NSApp.activate(ignoringOtherApps: true)
        return true
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        ServiceManager.shared.stopForApplicationQuit() ? .terminateNow : .terminateCancel
    }
    func applicationWillTerminate(_ notification: Notification) {
        if lock >= 0 { Darwin.close(lock) }
    }
    private func selfCheck() async {
        let manager = ServiceManager.shared
        let pid = manager.servicePID
        let report = manager.status
        let stopped = await manager.stop()
        let disappeared = (try? await DesktopClient().request("desktop/status")) == nil
        let result: [String: Any] = [
            "status": pid != nil && report?.simulated == false && report?.arl.sdkVersion == "0.1.1" && stopped && disappeared ? "PASS_NATIVE_APP_SERVICE_LIFECYCLE" : "FAIL_NATIVE_APP_SERVICE_LIFECYCLE",
            "bundle": Bundle.main.bundleURL.path, "bundle_identifier": Bundle.main.bundleIdentifier ?? "",
            "owned_service_pid": pid ?? 0, "real_source": report?.source ?? "",
            "simulated": report?.simulated ?? true, "sdk_version": report?.arl.sdkVersion ?? "",
            "arl_ready": report?.arl.ready ?? false, "native_stop_completed": stopped,
            "listener_disappeared_after_stop": disappeared, "local_network_prompt_triggered": false,
            "device_control_or_model_inference": false]
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: FridayPaths.base.appendingPathComponent("NATIVE-SELF-CHECK.json"), options: .atomic)
        }
        NSApp.terminate(nil)
    }
}
