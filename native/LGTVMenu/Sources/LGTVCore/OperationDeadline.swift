import Foundation

/// Bounds callback-based I/O even when cancellation never invokes its callback.
/// A task-group race cannot provide this guarantee: leaving a group waits for all children.
public enum OperationDeadline {
    public static func run<Value: Sendable>(
        seconds: Double,
        cancel: @escaping @Sendable () -> Void,
        start: @escaping @Sendable (@escaping @Sendable (Result<Value, Error>) -> Void) -> Void
    ) async throws -> Value {
        let gate = DeadlineGate<Value>(cancel: cancel)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                gate.start(continuation, seconds: seconds, operation: start)
            }
        } onCancel: {
            gate.finish(.failure(CancellationError()), cancelOperation: true)
        }
    }
}

private final class DeadlineGate<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private let cancel: @Sendable () -> Void
    private var continuation: CheckedContinuation<Value, Error>?
    private var result: Result<Value, Error>?
    private var timer: DispatchWorkItem?
    private var cancelRequested = false

    init(cancel: @escaping @Sendable () -> Void) { self.cancel = cancel }

    func start(_ continuation: CheckedContinuation<Value, Error>, seconds: Double,
               operation: @escaping @Sendable (@escaping @Sendable (Result<Value, Error>) -> Void) -> Void) {
        let timer = DispatchWorkItem { [weak self] in
            self?.finish(.failure(TVError.timeout), cancelOperation: true)
        }
        lock.lock()
        if let result {
            lock.unlock()
            continuation.resume(with: result)
            return
        }
        self.continuation = continuation
        self.timer = timer
        lock.unlock()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + max(0.001, seconds), execute: timer)
        operation { [weak self] result in
            self?.finish(result, cancelOperation: false)
        }
        // Cancellation can race with registration of the underlying operation.
        // Repeating an idempotent I/O cancel after start prevents a late registration surviving it.
        lock.lock()
        let cancelled = cancelRequested
        lock.unlock()
        if cancelled { cancel() }
    }

    func finish(_ result: Result<Value, Error>, cancelOperation: Bool) {
        lock.lock()
        guard self.result == nil else { lock.unlock(); return }
        self.result = result
        cancelRequested = cancelOperation
        let continuation = self.continuation
        self.continuation = nil
        let timer = self.timer
        self.timer = nil
        lock.unlock()
        timer?.cancel()
        if cancelOperation { cancel() }
        continuation?.resume(with: result)
    }
}
