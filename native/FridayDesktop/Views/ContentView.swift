import SwiftUI

struct ContentView: View {
    @ObservedObject var service: ServiceManager
    private var presentation: HomeConnectionPresentation {
        HomeConnectionPresentation(status: service.status, readable: service.statusReadAvailable, phase: service.phase)
    }
    var body: some View {
        Group {
            if service.phase == .running && presentation.retainsConsole {
                ConsoleView(url: FridayPaths.endpoint)
            } else if service.phase == .starting || service.connecting || (service.phase == .running && service.status?.connection == "connecting") {
                VStack(spacing: 16) {
                    ProgressView().controlSize(.large)
                    Text("正在回到家中").font(.title3.weight(.medium))
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if service.phase == .running {
                ConnectionView(service: service)
            } else {
                VStack(spacing: 18) {
                    Image(systemName: "house").font(.system(size: 42)).foregroundStyle(.secondary)
                    Text(service.phase == .stopping ? "正在收尾" : service.phase == .stopped ? "家庭服务已暂停" : "Friday 暂时无法启动").font(.title2)
                    if let message = service.message { Text(message).foregroundStyle(.secondary) }
                    if service.phase != .stopping {
                        Button(service.phase == .stopped ? "恢复运行" : "重新启动") { Task { await service.start(userInitiated: true) } }.buttonStyle(.borderedProminent)
                    }
                }.padding(40).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }.frame(minWidth: 1000, minHeight: 680)
    }
}
