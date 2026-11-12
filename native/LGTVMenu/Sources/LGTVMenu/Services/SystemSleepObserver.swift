import Foundation
import IOKit
import IOKit.pwr_mgt

// IOMessage.h defines these as function-like macro expansions that Swift does
// not import: iokit_common_msg(code) = (0x38 << 26) | code.
private let canSystemSleepMessage: UInt32 = (0x38 << 26) | 0x270
private let systemWillSleepMessage: UInt32 = (0x38 << 26) | 0x280

/// Acknowledges a system sleep request exactly once, even if the application
/// or network operation stops responding. This never denies sleep.
final class SleepAcknowledgement: @unchecked Sendable {
    private let lock = NSLock()
    private let acknowledge: @Sendable () -> Void
    private var finished = false
    private var timeout: DispatchWorkItem?

    init(acknowledge: @escaping @Sendable () -> Void) { self.acknowledge = acknowledge }

    func arm(seconds: TimeInterval, onTimeout: @escaping @Sendable () -> Void) {
        let item = DispatchWorkItem { [self] in
            if complete() { onTimeout() }
        }
        lock.lock()
        guard !finished else { lock.unlock(); return }
        timeout = item
        lock.unlock()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + seconds, execute: item)
    }

    @discardableResult
    func complete() -> Bool {
        lock.lock()
        guard !finished else { lock.unlock(); return false }
        finished = true
        let item = timeout; timeout = nil
        lock.unlock()
        item?.cancel()
        acknowledge()
        return true
    }
}

@MainActor
final class SystemSleepObserver {
    private var root: io_connect_t = 0
    private var port: IONotificationPortRef?
    private var notifier: io_object_t = 0
    private var pending: SleepAcknowledgement?
    private var onSleep: ((@escaping () -> Void) -> Void)?
    private var onTimeout: (() -> Void)?

    /// The network shutdown gets at most four seconds before sleep is allowed.
    /// The watchdog runs outside the main actor and never waits for I/O callbacks.
    func start(onSleep: @escaping (@escaping () -> Void) -> Void,
               onTimeout: @escaping () -> Void) -> Bool {
        guard root == 0 else { return true }
        self.onSleep = onSleep; self.onTimeout = onTimeout
        root = IORegisterForSystemPower(Unmanaged.passUnretained(self).toOpaque(), &port,
                                       systemSleepCallback, &notifier)
        guard root != 0, let port, let source = IONotificationPortGetRunLoopSource(port)?.takeUnretainedValue() else {
            stop(); return false
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        return true
    }

    fileprivate func received(_ message: UInt32, token: Int) {
        guard root != 0 else { return }
        if message == canSystemSleepMessage {
            IOAllowPowerChange(root, token)
        } else if message == systemWillSleepMessage {
            pending?.complete()
            let connection = root
            let acknowledgement = SleepAcknowledgement { IOAllowPowerChange(connection, token) }
            pending = acknowledgement
            acknowledgement.arm(seconds: 4) { [weak self] in
                Task { @MainActor in self?.onTimeout?() }
            }
            if let onSleep { onSleep { acknowledgement.complete() } }
            else { acknowledgement.complete() }
        }
    }

    func stop() {
        pending?.complete(); pending = nil
        if let port {
            if let source = IONotificationPortGetRunLoopSource(port)?.takeUnretainedValue() {
                CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
            }
            if notifier != 0 { IODeregisterForSystemPower(&notifier) }
            if root != 0 { IOServiceClose(root) }
            IONotificationPortDestroy(port)
        }
        root = 0; port = nil; onSleep = nil; onTimeout = nil
    }
}

private func systemSleepCallback(_ context: UnsafeMutableRawPointer?, _ service: io_service_t,
                                 _ message: UInt32, _ argument: UnsafeMutableRawPointer?) {
    guard let context else { return }
    let observer = Unmanaged<SystemSleepObserver>.fromOpaque(context).takeUnretainedValue()
    let token = Int(bitPattern: argument)
    Task { @MainActor in observer.received(message, token: token) }
}
