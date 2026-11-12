/// Power is requested only after both system sleep/wake and a screen wake.
/// Screen-only events, launch, duplicate notifications and dark wakes do nothing.
public struct WakePolicy: Sendable {
    private var armed = false
    private var systemAwake = false
    private var screenAwake = false

    public init() {}

    public mutating func willSleep() {
        armed = true
        systemAwake = false
        screenAwake = false
    }

    public mutating func didWake() -> Bool {
        systemAwake = true
        return consumeIfReady()
    }

    public mutating func screensDidWake() -> Bool {
        screenAwake = true
        return consumeIfReady()
    }

    public mutating func screensDidSleep() { screenAwake = false }

    public mutating func suppressUntilNextSleep() {
        armed = false
        systemAwake = false
        screenAwake = false
    }

    private mutating func consumeIfReady() -> Bool {
        guard armed && systemAwake && screenAwake else { return false }
        armed = false
        return true
    }
}
