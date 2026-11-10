import Foundation
import XCTest
@testable import LGTVCore

private final class PendingCallback: @unchecked Sendable {
    private let lock = NSLock()
    private var completion: (@Sendable (Result<Int, Error>) -> Void)?
    private var cancellations = 0
    func register(_ completion: @escaping @Sendable (Result<Int, Error>) -> Void) {
        lock.lock(); self.completion = completion; lock.unlock()
    }
    func cancel() { lock.lock(); cancellations += 1; lock.unlock() }
    var cancellationCount: Int { lock.lock(); defer { lock.unlock() }; return cancellations }
    func reply(_ result: Result<Int, Error>) {
        lock.lock(); let callback = completion; lock.unlock()
        callback?(result)
    }
}

final class OperationDeadlineTests: XCTestCase {
    func testTimeoutReturnsWithoutAnyCancellationCallback() async throws {
        let pending = PendingCallback()
        let began = ProcessInfo.processInfo.systemUptime
        do {
            let _: Int = try await OperationDeadline.run(seconds: 0.04, cancel: { pending.cancel() }, start: {
                pending.register($0) // Intentionally never replies, including on cancel.
            })
            XCTFail("Missing timeout")
        } catch { XCTAssertEqual(error as? TVError, .timeout) }
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - began, 1)
        XCTAssertGreaterThanOrEqual(pending.cancellationCount, 1)
        // The system may later deliver an I/O callback; it must not resume twice.
        pending.reply(.success(42))
        pending.reply(.failure(TVError.invalidResponse))
    }

    func testSuccessfulSynchronousCallbackDoesNotCancelIO() async throws {
        let pending = PendingCallback()
        let value: Int = try await OperationDeadline.run(seconds: 1, cancel: { pending.cancel() }) {
            $0(.success(42))
        }
        XCTAssertEqual(value, 42)
        XCTAssertEqual(pending.cancellationCount, 0)
    }

    func testParentCancellationReturnsWithoutIOCompletion() async throws {
        let pending = PendingCallback()
        let started = expectation(description: "operation started")
        let task = Task<Int, Error> {
            try await OperationDeadline.run(seconds: 5, cancel: { pending.cancel() }) {
                pending.register($0)
                started.fulfill()
            }
        }
        await fulfillment(of: [started], timeout: 1)
        task.cancel()
        do { _ = try await task.value; XCTFail("Missing cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertGreaterThanOrEqual(pending.cancellationCount, 1)
        pending.reply(.success(42))
    }

    func testFailedCallbackIsPreservedAndLaterReplyIgnored() async throws {
        let pending = PendingCallback()
        do {
            let _: Int = try await OperationDeadline.run(seconds: 1, cancel: { pending.cancel() }) {
                pending.register($0)
                $0(.failure(TVError.commandRejected))
            }
            XCTFail("Missing callback error")
        } catch { XCTAssertEqual(error as? TVError, .commandRejected) }
        pending.reply(.success(42))
    }
}
