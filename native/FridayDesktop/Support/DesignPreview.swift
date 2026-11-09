import SwiftUI

/// Debug-only visual fixture. It never registers or contacts the household service.
enum FridayVisualPreview {
    static var scene: String? {
        #if DEBUG
        if Bundle.main.bundleIdentifier == "com.friday.home.designpreview",
           let scene = Bundle.main.object(forInfoDictionaryKey: "FridayDesignPreviewScene") as? String {
            return ["home", "setup", "starting", "stopped", "failed"].contains(scene) ? scene : "stopped"
        }
        guard let index = CommandLine.arguments.firstIndex(of: "--design-preview") else { return nil }
        let next = CommandLine.arguments.dropFirst(index + 1).first ?? "stopped"
        return ["home", "setup", "starting", "stopped", "failed"].contains(next) ? next : "stopped"
        #else
        return nil
        #endif
    }
    static var active: Bool { scene != nil }
}

#if DEBUG
struct FridayVisualPreviewScreen: View {
    @State private var scene: String
    init(scene: String) { _scene = State(initialValue: scene) }
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("设计预览 · simulated · 不连接家庭设备").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Picker("预览画面", selection: $scene) {
                    Text("我的家").tag("home")
                    Text("连接设置").tag("setup")
                    Text("正在打开").tag("starting")
                    Text("已暂停").tag("stopped")
                    Text("启动失败").tag("failed")
                }.frame(width: 230)
            }.padding(.horizontal, 16).padding(.vertical, 6)
            if scene == "home" || scene == "setup" {
                ConsoleView(url: URL(string: scene == "home"
                    ? "http://127.0.0.1:18743/?qa=populated#home"
                    : "http://127.0.0.1:18743/desktop/setup")!)
            } else {
                FridayServiceState(
                    title: scene == "starting" ? "正在打开你的家" : scene == "stopped" ? "家庭服务已暂停" : "Friday 暂时无法启动",
                    detail: scene == "starting" ? "正在启动本机家庭服务。" : scene == "stopped" ? "恢复运行后，继续查看房间与设备。" : "重新启动本机服务后再试一次。",
                    busy: scene == "starting",
                    diagnostic: scene == "failed" ? "合成错误：本机家庭服务未响应。" : nil,
                    actionTitle: scene == "stopped" ? "恢复运行" : "重新启动"
                )
            }
        }
    }
}
#endif
