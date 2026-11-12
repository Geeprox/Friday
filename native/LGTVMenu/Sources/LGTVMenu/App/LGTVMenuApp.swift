import AppKit
import Darwin
import SwiftUI

@MainActor
final class MenuAppDelegate: NSObject, NSApplicationDelegate {
    private var instanceLock: Int32 = -1
    func applicationDidFinishLaunching(_ notification: Notification) {
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("LGTVMenu", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            instanceLock = Darwin.open(directory.appendingPathComponent("instance.lock").path,
                                       O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            guard instanceLock >= 0, flock(instanceLock, LOCK_EX | LOCK_NB) == 0 else {
                for existing in NSRunningApplication.runningApplications(withBundleIdentifier: "local.friday.LGTVMenu")
                    where existing.processIdentifier != getpid() {
                    existing.activate()
                }
                NSApp.terminate(nil)
                return
            }
        } catch {
            // Without an instance lock, multiple wake observers could control the same TV.
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        TVAudioController.shared.start()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        NotificationCenter.default.post(name: .openTVSettings, object: nil)
        return true
    }
    func applicationWillTerminate(_ notification: Notification) {
        TVController.shared.shutdown()
        TVAudioController.shared.shutdown()
        if instanceLock >= 0 { Darwin.close(instanceLock) }
    }
}

private extension Notification.Name {
    static let openTVSettings = Notification.Name("LGTVMenu.openSettings")
}

@main
struct LGTVMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuAppDelegate.self) private var delegate
    @StateObject private var controller = TVController.shared

    var body: some Scene {
        MenuBarExtra {
            TVMenuView(controller: controller)
        } label: {
            MenuBarLabel(controller: controller)
        }
        .menuBarExtraStyle(.window)

        Window("LG TV 设置", id: "settings") {
            TVSettingsView(controller: controller)
        }
        .defaultSize(width: 570, height: 640)
        .windowResizability(.contentSize)
    }
}

private struct MenuBarLabel: View {
    @ObservedObject var controller: TVController
    @Environment(\.openWindow) private var openWindow
    @State private var shownSetup = false
    var body: some View {
        Label("LG TV", systemImage: "tv")
            .task {
                if !shownSetup && controller.configuration == nil {
                    shownSetup = true
                    openWindow(id: "settings")
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: .openTVSettings)) { _ in
                openWindow(id: "settings")
                NSApp.activate(ignoringOtherApps: true)
            }
    }
}
