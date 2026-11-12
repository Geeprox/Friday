import Foundation

public enum MagicPacket {
    public static func addressBytes(_ address: String) throws -> [UInt8] {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let compact: String
        if trimmed.contains(":") || trimmed.contains("-") {
            let separator: Character = trimmed.contains(":") ? ":" : "-"
            let parts = trimmed.split(separator: separator, omittingEmptySubsequences: false)
            guard parts.count == 6, parts.allSatisfy({ $0.count == 2 }) else {
                throw TVError.invalidMAC
            }
            compact = parts.joined()
        } else {
            compact = trimmed
        }
        guard compact.count == 12, compact.allSatisfy({ $0.isASCII && $0.isHexDigit }) else {
            throw TVError.invalidMAC
        }
        let chars = Array(compact)
        let result = stride(from: 0, to: 12, by: 2).compactMap {
            UInt8(String(chars[$0...($0 + 1)]), radix: 16)
        }
        guard result.count == 6, result[0] & 1 == 0,
              result.contains(where: { $0 != 0 }) else { throw TVError.invalidMAC }
        return result
    }

    public static func normalized(_ address: String) throws -> String {
        try addressBytes(address).map { String(format: "%02X", $0) }.joined(separator: ":")
    }

    public static func bytes(for address: String) throws -> Data {
        let mac = try addressBytes(address)
        return Data(Array(repeating: UInt8(255), count: 6) + Array(repeating: mac, count: 16).flatMap { $0 })
    }
}
