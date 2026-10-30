import SwiftUI

struct ConnectionView: View {
    @ObservedObject var service: ServiceManager
    @StateObject private var network = LocalNetworkAccess()
    private var presentation: HomeConnectionPresentation {
        HomeConnectionPresentation(status: service.status, readable: service.statusReadAvailable, phase: service.phase)
    }
    private var needsAuthorization: Bool { presentation.needsAuthorization }
    private func connect(_ report: DesktopStatus) {
        guard service.statusReadAvailable else { return }
        guard let host = report.networkHost else {
            service.message = "家庭配置缺少网关地址。请核对已有配置后重试。"
            return
        }
        let context = service.requestContext
        network.check(host: host, port: report.networkPort) { Task { await service.connectHome(ifCurrent: context) } }
    }
    var body: some View {
        GeometryReader { geometry in
        ScrollView {
        VStack(spacing: 18) {
            Image(systemName: needsAuthorization ? "key.fill" : service.status?.failure == nil ? "house" : "exclamationmark.triangle")
                .font(.system(size: 36))
                .foregroundStyle(service.status?.failure == nil ? Color.secondary : Color.orange)
                .accessibilityHidden(true)
            Text(presentation.title).font(.title2.weight(.semibold)).multilineTextAlignment(.center)
                .accessibilityAddTraits(.isHeader)
            if let description = presentation.titleDetail {
                Text(description).font(.caption).foregroundStyle(.secondary)
            }
            Text(presentation.detail).foregroundStyle(.primary).multilineTextAlignment(.center).frame(maxWidth: 560)
            if needsAuthorization {
                Text("在 Home Assistant 中重新登录并确认授权，返回 Friday 后重新读取家庭状态。")
                    .foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 520)
            }
            if let report = service.status {
                if needsAuthorization {
                    Button("重新授权") { service.openHomeAuthorization() }
                        .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                        .disabled(!service.statusReadAvailable || !report.configurationAvailable || service.connecting)
                    if service.awaitingAuthorization {
                        Text("授权页面已打开，在浏览器中完成登录后返回这里。")
                            .foregroundStyle(.secondary).multilineTextAlignment(.center)
                        Button("检查授权") { connect(report) }
                            .disabled(!service.statusReadAvailable || service.connecting || network.checking)
                        Text("完成授权后重新读取家庭状态").font(.caption).foregroundStyle(.secondary)
                    }
                } else if !report.credentialAvailable && report.configurationAvailable {
                    Button("登录网关") { service.openHomeAuthorization() }
                        .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                        .disabled(!service.statusReadAvailable || service.connecting)
                    Text("在 Home Assistant 中登录").font(.caption).foregroundStyle(.secondary)
                } else {
                    Button(network.checking ? "等待网络权限…" : service.status?.failure == nil ? "连接家庭" : "重新读取") {
                        connect(report)
                    }.buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                        .disabled(!service.statusReadAvailable || !report.configurationAvailable || !report.credentialAvailable || service.connecting || network.checking)
                }
            } else {
                Button("重新读取") { Task { await service.refresh() } }
                    .buttonStyle(.borderedProminent).controlSize(.large)
                    .keyboardShortcut(.defaultAction)
            }
            if service.connecting { ProgressView("正在读取家庭状态…") }
            if let message = network.message { Text(message).foregroundStyle(.secondary).frame(maxWidth: 560) }
            Text("连接恢复后，即可查看家庭状态和控制设备。")
                .font(.callout).foregroundStyle(.secondary)
            if service.status?.failure != nil {
                Button("查看记录") { service.openLogs() }
            }
            DisclosureGroup("连接详情") {
                VStack(alignment: .leading, spacing: 10) {
                    if let report = service.status {
                        Text(report.configurationAvailable ? "已有家庭配置" : "尚未配置家庭网关")
                        Text(report.credentialAvailable ? "本机有授权记录，当前有效性以重新读取的结果为准。" : "本机尚无可用授权记录。")
                        Text(report.arl.connectionDetail)
                        Text(report.arl.connectionRecovery)
                    }
                    Text("此页仅管理连接，不会开启摄像头、音频或设备控制。")
                }.font(.callout).foregroundStyle(.secondary).padding(.top, 8)
            }.frame(maxWidth: 560)
        }.padding(32).frame(maxWidth: .infinity, minHeight: geometry.size.height)
        }
        }
    }
}
