import AppKit
import SwiftUI

struct TVMenuView: View {
    @ObservedObject var controller: TVController
    @ObservedObject private var audio = TVAudioController.shared
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Image(systemName: "tv").font(.title2)
                VStack(alignment: .leading, spacing: 3) {
                    Text(controller.configuration?.name ?? "LG TV").font(.headline).lineLimit(1)
                    Text(controller.status).font(.caption).foregroundStyle(.secondary)
                }
            }
            if let state = audio.state {
                HStack {
                    Button { audio.toggleMute() } label: {
                        Image(systemName: state.muted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                    }.buttonStyle(.plain).accessibilityLabel(state.muted ? "取消电视静音" : "电视静音")
                    Slider(value: Binding(get: { audio.state?.scalar ?? 0 }, set: audio.requestVolume), in: 0...1)
                        .accessibilityLabel("电视音量")
                    Text("\(state.volume)").monospacedDigit().frame(width: 28)
                }.disabled(!audio.connected || !state.adjustable)
                Text(audio.routing ? "已连接系统音量" : audio.message)
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack {
                Button("打开电视", systemImage: "tv") { controller.wake() }
                    .disabled(controller.isBusy || controller.configuration?.canWake != true)
                Spacer()
                Button("关闭电视", systemImage: "power") { controller.turnOff() }
                    .disabled(controller.configuration == nil)
            }
            Toggle("随 Mac 唤醒", isOn: Binding(get: { controller.automaticWake }, set: controller.setAutomaticWake))
                .disabled(controller.configuration?.canWake != true)
            Toggle("随 Mac 息屏、锁屏或睡眠关闭", isOn: Binding(get: { controller.automaticLockOff }, set: controller.setAutomaticLockOff))
                .disabled(controller.configuration?.canWake != true)
            if controller.isBusy { Button("取消当前操作") { controller.cancelWork() } }
            Divider()
            HStack {
                Button("设置…") { openWindow(id: "settings"); NSApp.activate(ignoringOtherApps: true) }
                Spacer()
                Button("退出") { NSApp.terminate(nil) }.keyboardShortcut("q")
            }
        }
        .padding(17).frame(width: 310)
    }
}
