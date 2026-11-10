import Foundation
import XCTest
@testable import LGTVMenu

private final class AcknowledgementCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    func increment() { lock.lock(); value += 1; lock.unlock() }
    var count: Int { lock.lock(); defer { lock.unlock() }; return value }
}

final class SleepAcknowledgementTests: XCTestCase {
    func testUnresponsiveNetworkCannotDelaySleepBeyondDeadline() async {
        let count = AcknowledgementCounter()
        let timedOut = expectation(description: "sleep released by watchdog")
        let lease = SleepAcknowledgement { count.increment() }
        lease.arm(seconds: 0.03) { timedOut.fulfill() }
        await fulfillment(of: [timedOut], timeout: 1)
        XCTAssertEqual(count.count, 1)
        XCTAssertFalse(lease.complete()) // A late network reply cannot acknowledge twice.
    }

    func testSuccessfulShutdownCancelsTimeoutAndReleasesOnce() async {
        let count = AcknowledgementCounter()
        let timedOut = expectation(description: "no timeout after completion")
        timedOut.isInverted = true
        let lease = SleepAcknowledgement { count.increment() }
        lease.arm(seconds: 0.02) { timedOut.fulfill() }
        XCTAssertTrue(lease.complete())
        XCTAssertFalse(lease.complete())
        await fulfillment(of: [timedOut], timeout: 0.06)
        XCTAssertEqual(count.count, 1)
    }

    func testApplicationStopReleasesLeaseBeforeWatchdogRegistration() async {
        let count = AcknowledgementCounter()
        let timedOut = expectation(description: "no timeout after early release")
        timedOut.isInverted = true
        let lease = SleepAcknowledgement { count.increment() }
        XCTAssertTrue(lease.complete())
        lease.arm(seconds: 0.01) { timedOut.fulfill() }
        await fulfillment(of: [timedOut], timeout: 0.03)
        XCTAssertEqual(count.count, 1)
    }
}
