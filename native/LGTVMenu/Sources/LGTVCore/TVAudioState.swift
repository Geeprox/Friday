import CoreFoundation
import Foundation

public struct TVAudioState: Equatable, Sendable {
    public var volume: Int
    public var muted: Bool
    public var maximum: Int
    public var adjustable: Bool

    public init(volume: Int, muted: Bool, maximum: Int = 100, adjustable: Bool = true) {
        self.volume = volume; self.muted = muted; self.maximum = maximum; self.adjustable = adjustable
    }

    public var scalar: Double { Double(volume) / Double(maximum) }
    public func requestedVolume(scalar: Double) throws -> Int {
        guard scalar.isFinite, (0...1).contains(scalar), adjustable else { throw TVError.invalidVolume }
        return Int((scalar * Double(maximum)).rounded())
    }

    /// Supports current nested volumeStatus and legacy flat getStatus/getVolume replies.
    public static func parse(_ payload: [String: Any], previous: TVAudioState? = nil) -> TVAudioState? {
        guard payload["returnValue"] as? Bool != false else { return nil }
        let nested = payload["volumeStatus"] as? [String: Any] ?? payload
        let maximum = integer(nested["maxVolume"] ?? payload["maxVolume"]) ?? previous?.maximum ?? 100
        guard (1...100).contains(maximum),
              let volume = integer(nested["volume"] ?? payload["volume"]) ?? previous?.volume,
              (0...maximum).contains(volume),
              let muted = boolean(nested["muteStatus"] ?? nested["muted"] ?? nested["mute"] ?? payload["mute"] ?? payload["muted"]) ?? previous?.muted else { return nil }
        let adjustable = boolean(nested["adjustVolume"] ?? payload["adjustVolume"]) ?? previous?.adjustable ?? true
        return TVAudioState(volume: volume, muted: muted, maximum: maximum, adjustable: adjustable)
    }

    private static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
              (0...100).contains(number.doubleValue) else { return nil }
        return number.intValue
    }
    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
}
