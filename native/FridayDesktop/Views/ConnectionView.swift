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
        VStack(spacing: 18) {
            Image(systemName: needsAuthorization ? "key.fill" : service.status?.failure == nil ? "house" : "exclamationmark.triangle")
                .font(.system(size: 36))
                .foregroundStyle(service.status?.failure == nil ? Color.secondary : Color.orange)
                .accessibilityHidden(true)
            Text(presentation.title).font(.title2.weight(.semibold)).multilineTextAlignment(.center)
            if let description = presentation.titleDetail {
                Text(description).font(.caption).foregroundStyle(.secondary)
            }
            Text(presentation.detail).foregroundStyle(.primary).multilineTextAlignment(.center).frame(maxWidth: 560)
            if needsAuthorization {
                Text("请核对权限与本次操作范围。请在 Home Assistant 中重新登录并确认授权，返回 Friday 后会重新读取家庭状态。")
                    .foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 520)
            }
            if let report = service.status {
                if needsAuthorization {
                    Button("重新授权") { service.openHomeAuthorization() }
                        .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                        .disabled(!service.statusReadAvailable || !report.configurationAvailable || service.connecting)
                    if service.awaitingAuthorization {
                        Text("请核对权限与本次操作范围。授权页面已打开。请在浏览器中完成登录和授权。")
                            .foregroundStyle(.secondary).multilineTextAlignment(.center)
                        Button("检查授权") { connect(report) }
                            .disabled(!service.statusReadAvailable || service.connecting || network.checking)
                        Text("完成授权后重新读取家庭状态").font(.caption).foregroundStyle(.secondary)
                    }
                } else if !report.credentialAvailable && report.configurationAvailable {
                    Button("登录网关") { service.openHomeAuthorization() }
                        .buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                        .disabled(!service.statusReadAvailable || service.connecting)
                    Text("在Home Assistant中登录").font(.caption).foregroundStyle(.secondary)
                } else {
                    Button(network.checking ? "等待网络权限…" : service.status?.failure == nil ? "连接家庭" : "重新读取") {
                        connect(report)
                    }.buttonStyle(.borderedProminent).controlSize(.large).keyboardShortcut(.defaultAction)
                        .disabled(!service.statusReadAvailable || !report.configurationAvailable || !report.credentialAvailable || service.connecting || network.checking)
                }
            }
            if service.connecting { ProgressView("正在读取家庭状态…") }
            if let message = network.message { Text(message).foregroundStyle(.secondary).frame(maxWidth: 560) }
            Text("连接恢复前，家庭状态与对话不可用。恢复连接前，家庭状态与对话暂不可用。")
                .font(.callout).foregroundStyle(.secondary)
            if service.status?.failure != nil {
                Button("查看记录") { service.openLogs() }
            }
            DisclosureGroup("连接详情") {
                VStack(alignment: .leading, spacing: 10) {
                    if let report = service.status {
                        Text(report.configurationAvailable ? "已有家庭配置" : "尚未配置家庭网关")
                        Text(report.credentialAvailable ? "本机有授权记录，不代表当前授权有效。本机有授权记录；记录存在不代表授权当前有效。" : "本机尚无可用授权记录。")
                        Text(report.arl.connectionDetail)
                    }
                    Text("设备相机音频NAS读取模型调用均未启用。设备执行、摄像头、音频、NAS 内容读取与模型调用未启用。")
                }.font(.callout).foregroundStyle(.secondary).padding(.top, 8)
            }.frame(maxWidth: 560)
        }.padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
