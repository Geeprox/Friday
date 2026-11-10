import Foundation

public enum WebOSProtocol {
    /// Some firmware sends its hello without an ID. All other replies must
    /// retain request correlation, including registration and power commands.
    public static func matchesResponse(_ response: [String: Any], requestID: String) -> Bool {
        if let id = response["id"] as? String { return id == requestID }
        let hasNoID = response["id"] == nil || response["id"] is NSNull
        return hasNoID && requestID == "hello" && response["type"] as? String == "hello"
    }

    public static func registration(clientKey: String?, audioControl: Bool = false) -> [String: Any] {
        var permissions = ["CONTROL_POWER", "READ_POWER_STATE", "READ_NETWORK_STATE"]
        if audioControl { permissions.append("CONTROL_AUDIO") }
        var payload: [String: Any] = [
            "forcePairing": false, "pairingType": "PROMPT",
            "manifest": [
                "manifestVersion": 1, "appVersion": "1.0",
                "permissions": permissions
            ]
        ]
        if let clientKey { payload["client-key"] = clientKey }
        return ["id": "register", "type": "register", "payload": payload]
    }

    public static func normalizedUUID(_ value: String) -> String {
        let lower = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return lower.hasPrefix("uuid:") ? String(lower.dropFirst(5)) : lower
    }

    public static func validateIdentity(expected: String?, actual: String?) throws -> String {
        guard let actual, !normalizedUUID(actual).isEmpty else { throw TVError.invalidResponse }
        let identity = normalizedUUID(actual)
        if let expected, normalizedUUID(expected) != identity { throw TVError.identityChanged }
        return identity
    }

    /// Do not pick an inactive interface: its MAC will not wake the active NIC.
    public static func activeMAC(connection: [String: Any], host: String) -> String? {
        let interfaces = ["wired", "wifi"].compactMap { connection[$0] as? [String: Any] }
        let exact = interfaces.filter { $0["ipAddress"] as? String == host }
        let connected = interfaces.filter {
            $0["state"] as? String == "connected"
                && (($0["ipAddress"] as? String).map { $0.isEmpty } ?? true)
        }
        let candidates = exact.isEmpty ? connected : exact
        guard candidates.count == 1,
              let address = candidates[0]["macAddress"] as? String else { return nil }
        return try? MagicPacket.normalized(address)
    }

    public static func reportsActive(_ payload: [String: Any]) -> Bool {
        payload["state"] as? String == "Active"
    }
}
