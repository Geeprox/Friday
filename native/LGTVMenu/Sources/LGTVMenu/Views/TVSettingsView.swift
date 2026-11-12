import LGTVCore
import ServiceManagement
import SwiftUI

struct TVSettingsView: View {
    @ObservedObject var controller: TVController
    @AppStorage("setupName") private var name = "LG webOS TV"
    @AppStorage("setupHost") private var host = ""
    @State private var mac = ""
    @AppStorage("setupUseTLS") private var useTLS = true
    @State private var discoveryID: String?
    @State private var initialized = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                Image(systemName: "tv").font(.system(size: 32)).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    Text("LG TV").font(.title2.bold())
                    Text("电视电源与系统音量，放在一起。")
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if controller.isBusy { ProgressView().controlSize(.small) }
            }

            Form {
                Section("选择电视") {
                    HStack {
                        Text("保持电视开机，并连接同一局域网。")
                            .font(.callout).foregroundStyle(.secondary)
                        Spacer()
                        Button(controller.isSearching ? "查找中…" : "查找电视") { controller.discover() }
                            .disabled(controller.isSearching || controller.isBusy)
                    }
                    ForEach(controller.discovered, id: \.self) { tv in
                        Button {
                            name = tv.name; host = tv.host; discoveryID = tv.id
                            // MAC belongs to the selected NIC, never carry it to another TV.
                            mac = controller.configuration?.id == tv.id ? controller.configuration?.macAddress ?? "" : ""
                        } label: {
                            HStack {
                                Image(systemName: discoveryID == tv.id && host == tv.host ? "checkmark.circle.fill" : "circle")
                                Text(tv.name).lineLimit(1)
                                Spacer()
                                Text(tv.host).foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(controller.isBusy)
                        .accessibilityLabel("选择 \(tv.name)，地址 \(tv.host)")
                    }
                    TextField("名称", text: $name)
                    TextField("电视 IP 地址", text: $host)
                        .onChange(of: host) { _, _ in
                            if !controller.discovered.contains(where: { $0.id == discoveryID && $0.host == host }) {
                                discoveryID = nil
                            }
                            if host != controller.configuration?.host { mac = "" }
                        }
                    TextField("联网接口 MAC", text: $mac, prompt: Text("仅唤醒需要，可先留空"))
                    Text("配对和关机无需填写 MAC；配对成功后会尝试读取。")
                        .font(.caption).foregroundStyle(.secondary)
                    Toggle("使用加密连接", isOn: $useTLS)
                    if !useTLS {
                        Text("仅供不支持加密接口的旧款电视。配对信息会经本地网络明文传输。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    HStack {
                        Button(controller.configuration == nil ? "配对电视" : "重新配对") {
                            controller.pair(name: name, host: host, macAddress: mac, useTLS: useTLS, discoveryID: discoveryID)
                        }
                        .disabled(controller.isBusy || !TVConfiguration.isLANAddress(host))
                        if controller.configuration != nil {
                            Button("保存 MAC") { controller.saveMAC(mac) }
                                .disabled(controller.isBusy || host != controller.configuration?.host || (try? MagicPacket.bytes(for: mac)) == nil)
                        }
                        Spacer()
                        if controller.isBusy {
                            Button("取消") { controller.cancelWork() }
                        }
                    }
                    Text("点击配对后，在目标电视上选择允许。无需在电脑上输入电视账号或密码。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                TVAudioSettingsView(controller: controller)
                Section("随 Mac 开关电视") {
                    Toggle("息屏、睡眠或锁屏时关闭电视", isOn: Binding(
                        get: { controller.automaticLockOff }, set: controller.setAutomaticLockOff))
                        .disabled(controller.configuration?.canWake != true)
                    Text("屏保视频继续播放。息屏、睡眠或锁屏时关闭电视；再次动键鼠或解锁时自动打开。")
                        .font(.caption).foregroundStyle(.secondary)
                    Toggle("Mac 睡眠后唤醒时打开电视", isOn: Binding(
                        get: { controller.automaticWake }, set: controller.setAutomaticWake))
                        .disabled(controller.configuration?.canWake != true)
                    Toggle("登录时启动 LG TV", isOn: Binding(
                        get: { controller.loginEnabled || controller.loginNeedsApproval }, set: controller.setLoginEnabled))
                    if controller.loginNeedsApproval {
                        Button("在系统设置中允许登录启动") { SMAppService.openSystemSettingsLoginItems() }
                    }
                    Text("自动开机需要电视已开启“通过 Wi-Fi 开机”或“Wake on LAN”。LG TV 应用需保持运行。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)

            VStack(alignment: .leading, spacing: 5) {
                Text(controller.status).font(.headline)
                Text(controller.detail).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            .accessibilityElement(children: .combine)
            HStack {
                Button("读取状态") { controller.readStatus() }
                Button("打开电视") { controller.wake() }
                    .disabled(controller.configuration?.canWake != true)
                Spacer()
                Button("移除本机配对", role: .destructive) { controller.forget() }
            }
            .disabled(controller.configuration == nil || controller.isBusy)
        }
        .padding(22)
        .frame(width: 570, height: 790)
        .onAppear {
            controller.refreshLoginStatus()
            guard !initialized else { return }
            initialized = true
            if let saved = controller.configuration {
                name = saved.name; host = saved.host; mac = saved.macAddress
                useTLS = saved.useTLS; discoveryID = saved.id
            } else { controller.discover() }
        }
        .onChange(of: controller.configuration) { _, saved in
            if let saved { mac = saved.macAddress }
        }
    }
}
