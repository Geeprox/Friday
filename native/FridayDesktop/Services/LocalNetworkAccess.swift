import Foundation
import Network

@MainActor
final class LocalNetworkAccess: ObservableObject {
    @Published var checking = false
    @Published var message: String?
    private var connection: NWConnection?

    func check(host: String, port: UInt16, then operation: @escaping () -> Void) {
        guard !checking, let port = NWEndpoint.Port(rawValue: port) else { return }
        checking = true
        message = nil
        let connection = NWConnection(host: NWEndpoint.Host(host), port: port, using: .tcp)
        self.connection = connection
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            Task { @MainActor in
                guard let self, let connection, self.connection === connection else { return }
                switch state {
                case .ready:
                    connection.cancel()
                    self.connection = nil
                    self.checking = false
                    operation()
                case .waiting, .failed:
                    self.message = "请核对连接状态与使用范围。网关连接尚未通过。若系统显示 Friday 本地网络弹窗，请由你确认；也可在系统设置中检查 Friday。"
                    connection.cancel()
                    self.connection = nil
                    self.checking = false
                default: break
                }
            }
        }
        connection.start(queue: .global(qos: .userInitiated))
    }
}
