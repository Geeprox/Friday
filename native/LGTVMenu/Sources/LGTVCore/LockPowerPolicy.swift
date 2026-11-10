import Foundation

/// Screen saver playback is distinct from a lock screen, display sleep and
/// system sleep. All times are monotonic seconds; no UI or hardware is queried.
public struct LockPowerPolicy: Sendable {
    public enum Reason: String, Sendable { case lock, displaySleep, systemSleep }
    public static let shutdownDelay: TimeInterval = 2
    public private(set) var screensaverRunning = false
    public private(set) var shutdownReason: Reason?
    public private(set) var lastShutdownReason: Reason?
    private var locked = false
    private var displaySleeping = false
    private var systemSleeping = false
    private var manualSuppression = false
    private var shutdownAt: TimeInterval?
    private var activityAfter: TimeInterval?

    public init() {}

    public var needsObservation: Bool { shutdownAt != nil || activityAfter != nil }

    @discardableResult
    public mutating func screenLocked(at now: TimeInterval) -> Bool {
        guard !locked else { return false }
        locked = true
        manualSuppression = false
        guard !screensaverRunning, !systemSleeping else { return false }
        return schedule(.lock, at: now + Self.shutdownDelay)
    }

    public mutating func screensaverStarted() {
        screensaverRunning = true
        // Notifications may arrive in either order. A saver that starts during
        // the lock delay cancels only that delay, never a real sleep request.
        if shutdownReason == .lock { cancelPending() }
    }

    @discardableResult
    public mutating func screensaverStopped(at now: TimeInterval) -> Bool {
        guard screensaverRunning else { return false }
        screensaverRunning = false
        guard locked, !displaySleeping, !systemSleeping, !manualSuppression else { return false }
        return schedule(.lock, at: now + Self.shutdownDelay)
    }

    @discardableResult
    public mutating func displayDidSleep(at now: TimeInterval) -> Bool {
        displaySleeping = true
        guard !manualSuppression else { return false }
        return schedule(.displaySleep, at: now)
    }

    public mutating func displayDidWake() { displaySleeping = false }

    @discardableResult
    public mutating func systemWillSleep(at now: TimeInterval) -> Bool {
        systemSleeping = true
        manualSuppression = false
        return schedule(.systemSleep, at: now)
    }

    public mutating func systemDidWake() { systemSleeping = false }

    public mutating func beginShutdown(at now: TimeInterval) -> Bool {
        guard let reason = shutdownReason, let deadline = shutdownAt, now >= deadline,
              reason != .lock || (locked && !screensaverRunning && !systemSleeping) else { return false }
        cancelPending()
        lastShutdownReason = reason
        // Ignore the lock shortcut's key-up and HDMI changes caused by the
        // shutdown itself. Later HID activity is enough; no key data is read.
        activityAfter = now + 0.75
        return true
    }

    public mutating func userActivity(at now: TimeInterval, idleSeconds: TimeInterval?) -> Bool {
        guard let cutoff = activityAfter, let idle = idleSeconds,
              now.isFinite, idle.isFinite, idle >= 0, now - idle > cutoff else { return false }
        return consumeRestoration()
    }

    public mutating func screenUnlocked() -> Bool {
        locked = false
        manualSuppression = false
        if shutdownReason == .lock { cancelPending() }
        return consumeRestoration()
    }

    @discardableResult
    public mutating func consumeRestoration() -> Bool {
        let needed = activityAfter != nil
        cancelPending()
        activityAfter = nil
        return needed
    }

    /// Explicit power commands and disabling the feature override automation.
    public mutating func suppress() {
        cancelPending(); activityAfter = nil; manualSuppression = true
    }

    private mutating func schedule(_ reason: Reason, at deadline: TimeInterval) -> Bool {
        // One off request per inactive cycle, even when lock/display/system
        // notifications all describe the same transition.
        guard activityAfter == nil else { return false }
        if let previous = shutdownAt, previous <= deadline { return false }
        shutdownReason = reason
        shutdownAt = deadline
        return true
    }

    private mutating func cancelPending() { shutdownAt = nil; shutdownReason = nil }
}
