import AppKit
import SwiftUI

@main
struct FridayApp: App {
    init() { if !FridayVisualPreview.active { HouseholdService.runIfRequested() } }
    @NSApplicationDelegateAdaptor(FridayDelegate.self) private var delegate
    @StateObject private var service = ServiceManager.shared
    var body: some Scene {
        Window("Friday", id: "main") {
            ContentView(service: service)
        }.defaultSize(width: 1360, height: 900)
            .windowToolbarStyle(.unifiedCompact)
            .commands {
                CommandGroup(replacing: .newItem) {}
                CommandMenu("服务") {
                    Button("启动服务") { Task { await service.start(userInitiated: true) } }.disabled(FridayVisualPreview.active)
                    Button("停止服务") { Task { await service.stop() } }.disabled(FridayVisualPreview.active)
                    Divider()
                    Button("查看记录") { service.openLogs() }.disabled(FridayVisualPreview.active)
                }
            }
    }
}
