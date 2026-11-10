import Foundation

public struct TVConfiguration: Codable, Equatable, Sendable {
    public var id: String
    public var name: String
    public var host: String
    public var macAddress: String
    public var useTLS: Bool
    public var certificateFingerprint: String?
    public var deviceUUID: String?
    public var pairingKeyID: String?
    public var audioControlGranted: Bool?

    public init(id: String = UUID().uuidString, name: String, host: String,
                macAddress: String = "", useTLS: Bool = true,
                certificateFingerprint: String? = nil, deviceUUID: String? = nil,
                pairingKeyID: String? = nil, audioControlGranted: Bool? = nil) {
        self.id = id
        self.name = name
        self.host = host
        self.macAddress = macAddress
        self.useTLS = useTLS
        self.certificateFingerprint = certificateFingerprint
        self.deviceUUID = deviceUUID
        self.pairingKeyID = pairingKeyID
        self.audioControlGranted = audioControlGranted
    }

    public var canWake: Bool { (try? MagicPacket.bytes(for: macAddress)) != nil }
    /// Existing configurations keep their credential account until explicit re-pairing.
    public var keychainAccount: String { pairingKeyID ?? id }

    /// Explicit IPv4 LAN addresses keep discovery, credentials and power control local.
    public static func isLANAddress(_ value: String) -> Bool {
        let parts = value.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        let numbers = parts.compactMap { part -> UInt8? in
            guard !part.isEmpty, part.allSatisfy({ $0.isASCII && $0.isNumber }),
                  part.count == 1 || part.first != "0" else { return nil }
            return UInt8(part)
        }
        guard numbers.count == 4, numbers[3] != 255 else { return false }
        return numbers[0] == 10
            || (numbers[0] == 172 && (16...31).contains(numbers[1]))
            || (numbers[0] == 192 && numbers[1] == 168)
            || (numbers[0] == 169 && numbers[1] == 254)
    }
}

public enum TVError: LocalizedError, Equatable {
    case invalidAddress, invalidMAC, networkUnavailable, timeout, invalidResponse
    case pairingRequired, pairingRejected, identityChanged, certificateChanged
    case keychain, commandRejected, discoveryFailed
    case audioPermissionRequired, audioUnavailable, invalidVolume

    public var errorDescription: String? {
        switch self {
        case .invalidAddress: return "请输入电视当前的局域网 IPv4 地址。"
        case .invalidMAC: return "请填写电视当前联网接口的有效 MAC 地址。"
        case .networkUnavailable: return "同一局域网尚未就绪，请检查 Mac 和电视的网络。"
        case .timeout: return "电视没有及时回应。请确认电视已开机，并允许本地网络访问。"
        case .invalidResponse: return "无法识别电视的回复，尚未确认执行结果。"
        case .pairingRequired: return "请打开电视，在设置中重新配对。"
        case .pairingRejected: return "配对未获允许，请在电视上选择允许后重试。"
        case .identityChanged: return "此地址的电视身份已改变，请重新查找并配对。"
        case .certificateChanged: return "电视证书已改变，请确认目标后重新配对。"
        case .keychain: return "无法访问配对钥匙串。请解锁后重试；更新应用后可能需要重新配对。"
        case .commandRejected: return "电视拒绝了请求，请检查配对权限。"
        case .discoveryFailed: return "未能查找电视，请检查本地网络权限，或手动填写地址。"
        case .audioPermissionRequired: return "请在电视上授权音量控制。已有开关机配对会保留至新授权验证完成。"
        case .audioUnavailable: return "电视当前音频状态不可用，请确认电视已开机且音频输出支持调节。"
        case .invalidVolume: return "电视音量必须在可用范围内。"
        }
    }
}
