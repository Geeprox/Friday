import SwiftUI
import AppKit

struct ContentView: View {
    @ObservedObject var service: ServiceManager
    private var presentation: HomeConnectionPresentation {
        HomeConnectionPresentation(status: service.status, readable: service.statusReadAvailable, phase: service.phase)
    }
    var body: some View {
        Group {
            #if DEBUG
            if let scene = FridayVisualPreview.scene {
                FridayVisualPreviewScreen(scene: scene)
            } else {
                liveContent
            }
            #else
            liveContent
            #endif
        }
        .frame(minWidth: 760, minHeight: 560)
        .preferredColorScheme(.light)
    }
    private var liveContent: some View {
        Group {
            if service.phase == .running && presentation.retainsConsole {
                ConsoleView(url: FridayPaths.endpoint)
            } else if service.phase == .starting {
                FridayServiceState(title: "正在打开你的家", detail: "正在启动本机家庭服务。", busy: true)
            } else if service.phase == .running {
                ConsoleView(url: FridayPaths.endpoint.appendingPathComponent("desktop/setup"))
            } else {
                FridayServiceState(
                    title: service.phase == .stopping ? "正在停止服务" : service.phase == .stopped ? "家庭服务已暂停" : "Friday 暂时无法启动",
                    detail: service.phase == .stopped ? "恢复运行后，继续查看房间与设备。" : service.phase == .stopping ? "请稍候。" : "重新启动本机服务后再试一次。",
                    busy: service.phase == .stopping,
                    diagnostic: service.message,
                    actionTitle: service.phase == .stopped ? "恢复运行" : "重新启动"
                ) { Task { await service.start(userInitiated: true) } }
            }
        }
    }
}

/// Shared by service lifecycle screens; it never starts a service on appearance.
struct FridayServiceState: View {
    let title: String
    let detail: String
    var busy = false
    var diagnostic: String? = nil
    var actionTitle = "重新启动"
    var action: () -> Void = {}
    private let accent = Color(red: 66 / 255, green: 68 / 255, blue: 73 / 255)

    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            HStack(spacing: 10) {
                Image(nsImage: NSImage(named: NSImage.applicationIconName) ?? NSImage()).resizable().frame(width: 28, height: 28).accessibilityHidden(true)
                Text("Friday")
            }
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(accent)
            VStack(alignment: .leading, spacing: 10) {
                Text(title).font(.title2.weight(.semibold)).accessibilityAddTraits(.isHeader)
                Text(detail).font(.body).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if busy {
                ProgressView().controlSize(.small).accessibilityLabel(title)
            } else {
                Button(actionTitle, action: action)
                    .buttonStyle(.borderedProminent).controlSize(.large)
                    .keyboardShortcut(.defaultAction).tint(accent)
            }
            if let diagnostic, !diagnostic.isEmpty {
                DisclosureGroup("查看详情") {
                    Text(diagnostic).font(.callout).foregroundStyle(.secondary)
                        .textSelection(.enabled).padding(.top, 8)
                }
            }
        }
        .frame(maxWidth: 400, alignment: .leading)
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(red: 245 / 255, green: 245 / 255, blue: 243 / 255))
    }
}
