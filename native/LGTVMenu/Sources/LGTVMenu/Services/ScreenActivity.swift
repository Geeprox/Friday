import Foundation
import IOKit

/// Only elapsed system idle time is queried. No event tap, key, pointer position
/// or physical input stream is opened, retained or logged.
enum ScreenActivity {
    static let locked = Notification.Name("com.apple.screenIsLocked")
    static let unlocked = Notification.Name("com.apple.screenIsUnlocked")
    static let screensaverStarted = Notification.Name("com.apple.screensaver.didstart")
    static let screensaverStopped = Notification.Name("com.apple.screensaver.didstop")

    static func idleSeconds() -> TimeInterval? {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOHIDSystem"))
        guard service != IO_OBJECT_NULL else { return nil }
        defer { IOObjectRelease(service) }
        guard let value = IORegistryEntryCreateCFProperty(service, "HIDIdleTime" as CFString,
                                                         kCFAllocatorDefault, 0)?.takeRetainedValue() as? NSNumber else { return nil }
        return value.doubleValue / 1_000_000_000
    }
}
