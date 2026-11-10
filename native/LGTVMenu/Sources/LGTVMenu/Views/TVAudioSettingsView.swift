import AppKit
import SwiftUI

struct TVAudioSettingsView: View {
    @ObservedObject var controller: TVController
    @ObservedObject private var audio = TVAudioController.shared
    var body: some View {
        Section("系统音量") {
            if !audio.driverInstalled {
                HStack {
                    Text("LG TV 音量组件尚未安装").foregroundStyle(.secondary)
                    Spacer()
                    Button("安装组件…") {
                        if let url = Bundle.main.url(forResource: "LGTVVolume", withExtension: "pkg") { NSWorkspace.shared.open(url) }
                    }
                }
                Text("由 macOS 安装器安装。安装时会重启音频服务，声音将短暂停顿。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Picker("HDMI 音频输出", selection: Binding(get: { audio.selectedUID }, set: audio.setSelectedOutput)) {
                if audio.selectedUID.isEmpty { Text("请选择").tag("") }
                ForEach(audio.outputs) { output in Text(output.name).tag(output.id) }
            }
            if controller.configuration?.audioControlGranted != true {
                Button("授权电视音量控制…") { controller.authorizeAudio() }
                    .disabled(controller.configuration == nil || controller.isBusy)
                Text("电视将显示新的连接许可，请用遥控器允许。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Toggle("使用系统声音滑块控制电视", isOn: Binding(get: { audio.enabled }, set: audio.setEnabled))
                .disabled(!audio.driverInstalled || controller.configuration?.audioControlGranted != true || audio.selectedUID.isEmpty)
            if let state = audio.state {
                HStack {
                    Text("电视音量")
                    Slider(value: Binding(get: { audio.state?.scalar ?? 0 }, set: audio.requestVolume),
                           in: 0...1, step: 1/Double(state.maximum))
                        .accessibilityLabel("电视音量")
                        .disabled(!audio.connected || !state.adjustable)
                    Text(state.muted ? "静音" : "\(state.volume)").monospacedDigit().frame(width: 34)
                    Button { audio.toggleMute() } label: { Image(systemName: state.muted ? "speaker.slash.fill" : "speaker.wave.2.fill") }
                        .accessibilityLabel(state.muted ? "取消电视静音" : "电视静音")
                        .disabled(!audio.connected)
                }
            }
            Text(audio.message).font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text("选回 HDMI 时自动接管；切换扬声器或耳机时让系统照常控制。退出 LG TV 后恢复原 HDMI 输出。")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("刷新音频设备") { audio.refreshHardware(retry: true) }
                Spacer()
                Button("导出音量诊断…") { audio.exportDiagnostics() }
            }
        }
    }
}
