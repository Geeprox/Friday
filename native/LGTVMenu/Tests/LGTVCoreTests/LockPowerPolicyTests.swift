import XCTest
@testable import LGTVCore

final class LockPowerPolicyTests: XCTestCase {
    func testLaunchAndIdleDoNotOperateTV() {
        var policy = LockPowerPolicy()
        XCTAssertFalse(policy.needsObservation)
        XCTAssertFalse(policy.beginShutdown(at: 100))
        XCTAssertFalse(policy.userActivity(at: 100, idleSeconds: 0))
        XCTAssertFalse(policy.screenUnlocked())
    }

    func testOneDelayedShutdownForDuplicateLockNotifications() {
        var policy = LockPowerPolicy()
        XCTAssertTrue(policy.screenLocked(at: 10))
        XCTAssertFalse(policy.screenLocked(at: 11))
        XCTAssertFalse(policy.beginShutdown(at: 11.99))
        XCTAssertTrue(policy.beginShutdown(at: 12))
        XCTAssertFalse(policy.beginShutdown(at: 13))
    }

    func testUnlockWithinDelayCancelsShutdownWithoutWake() {
        var policy = LockPowerPolicy()
        policy.screenLocked(at: 10)
        XCTAssertFalse(policy.screenUnlocked())
        XCTAssertFalse(policy.beginShutdown(at: 12))
        XCTAssertFalse(policy.needsObservation)
    }

    func testLockKeyReleaseAndOldIdleDoNotImmediatelyReopenTV() {
        var policy = lockedAndShuttingDown()
        XCTAssertFalse(policy.userActivity(at: 12.4, idleSeconds: 0))
        XCTAssertFalse(policy.userActivity(at: 30, idleSeconds: 17.4))
        XCTAssertTrue(policy.needsObservation)
    }

    func testKeyboardOrMouseActivityRestoresBeforeUnlockExactlyOnce() {
        var policy = lockedAndShuttingDown()
        XCTAssertTrue(policy.userActivity(at: 20, idleSeconds: 0.2))
        XCTAssertFalse(policy.needsObservation)
        XCTAssertFalse(policy.userActivity(at: 21, idleSeconds: 0))
        XCTAssertFalse(policy.screenUnlocked())
    }

    func testUnlockAlsoRestoresWhenNoIdleSourceIsAvailable() {
        var policy = lockedAndShuttingDown()
        XCTAssertFalse(policy.userActivity(at: 20, idleSeconds: nil))
        XCTAssertTrue(policy.screenUnlocked())
        XCTAssertFalse(policy.screenUnlocked())
    }

    func testInvalidIdleSamplesNeverRequestWake() {
        var policy = lockedAndShuttingDown()
        for value in [-1.0, Double.nan, Double.infinity] {
            XCTAssertFalse(policy.userActivity(at: 20, idleSeconds: value))
        }
        XCTAssertFalse(policy.userActivity(at: .infinity, idleSeconds: 0))
    }

    func testSystemSleepReplacesLockDelayWithImmediateShutdown() {
        var policy = LockPowerPolicy()
        policy.screenLocked(at: 10)
        XCTAssertTrue(policy.systemWillSleep(at: 11))
        XCTAssertTrue(policy.beginShutdown(at: 11))
        XCTAssertEqual(policy.lastShutdownReason, .systemSleep)
        XCTAssertFalse(policy.beginShutdown(at: 100))
    }

    func testSleepPreservesAlreadyRequestedRestoration() {
        var policy = lockedAndShuttingDown()
        XCTAssertFalse(policy.systemWillSleep(at: 13))
        XCTAssertFalse(policy.userActivity(at: 100, idleSeconds: 90))
        XCTAssertTrue(policy.userActivity(at: 101, idleSeconds: 0))
    }

    func testNormalWakeConsumesLockRestorationWithoutSecondWake() {
        var policy = lockedAndShuttingDown()
        XCTAssertTrue(policy.consumeRestoration())
        XCTAssertFalse(policy.userActivity(at: 30, idleSeconds: 0))
        XCTAssertFalse(policy.screenUnlocked())
    }

    func testManualOffOrDisableOverridesLockAutomation() {
        var policy = lockedAndShuttingDown()
        policy.suppress()
        XCTAssertFalse(policy.userActivity(at: 30, idleSeconds: 0))
        XCTAssertFalse(policy.screenUnlocked())
        XCTAssertTrue(policy.screenLocked(at: 40))
        XCTAssertTrue(policy.beginShutdown(at: 42))
    }

    func testScreensaverThenSecurityLockKeepsVideoPlaying() {
        var policy = LockPowerPolicy()
        policy.screensaverStarted()
        XCTAssertFalse(policy.screenLocked(at: 10))
        XCTAssertFalse(policy.beginShutdown(at: 1000))
        XCTAssertFalse(policy.userActivity(at: 1001, idleSeconds: 0))
        XCTAssertFalse(policy.needsObservation)
    }

    func testScreensaverArrivingAfterLockCancelsOnlyLockCountdown() {
        var policy = LockPowerPolicy()
        policy.screenLocked(at: 10)
        policy.screensaverStarted()
        XCTAssertFalse(policy.beginShutdown(at: 12))
        XCTAssertFalse(policy.needsObservation)
    }

    func testExitingSaverToPasswordScreenResumesDelayedLockOff() {
        var policy = LockPowerPolicy()
        policy.screensaverStarted()
        policy.screenLocked(at: 10)
        XCTAssertTrue(policy.screensaverStopped(at: 40))
        XCTAssertFalse(policy.beginShutdown(at: 41.9))
        XCTAssertTrue(policy.beginShutdown(at: 42))
        XCTAssertEqual(policy.lastShutdownReason, .lock)
    }

    func testSaverExitToUnlockedDesktopDoesNotPowerOff() {
        var policy = LockPowerPolicy()
        policy.screensaverStarted()
        policy.screenLocked(at: 10)
        policy.screensaverStopped(at: 40)
        XCTAssertFalse(policy.screenUnlocked())
        XCTAssertFalse(policy.beginShutdown(at: 42))
        XCTAssertFalse(policy.needsObservation)
    }

    func testDisplaySleepOverridesSaverAndRestoresOnActivity() {
        var policy = LockPowerPolicy()
        policy.screensaverStarted()
        policy.screenLocked(at: 10)
        XCTAssertTrue(policy.displayDidSleep(at: 20))
        policy.screensaverStarted() // Duplicate/late notification must not cancel real sleep.
        XCTAssertTrue(policy.beginShutdown(at: 20))
        XCTAssertEqual(policy.lastShutdownReason, .displaySleep)
        policy.displayDidWake()
        XCTAssertTrue(policy.userActivity(at: 30, idleSeconds: 0))
    }

    func testSystemSleepOverridesSaverAndAllowsNextCycleAfterWake() {
        var policy = LockPowerPolicy()
        policy.screensaverStarted()
        policy.screenLocked(at: 10)
        XCTAssertTrue(policy.systemWillSleep(at: 20))
        XCTAssertTrue(policy.beginShutdown(at: 20))
        XCTAssertEqual(policy.lastShutdownReason, .systemSleep)
        policy.systemDidWake()
        XCTAssertTrue(policy.consumeRestoration())
        policy.screenUnlocked()
        policy.screensaverStopped(at: 30)
        XCTAssertTrue(policy.screenLocked(at: 40))
        XCTAssertTrue(policy.beginShutdown(at: 42))
    }

    func testLockDisplayAndSystemSleepOnlyRequestOneShutdown() {
        var policy = lockedAndShuttingDown()
        XCTAssertFalse(policy.displayDidSleep(at: 20))
        XCTAssertFalse(policy.systemWillSleep(at: 21))
        XCTAssertFalse(policy.beginShutdown(at: 22))
        XCTAssertTrue(policy.consumeRestoration())
        XCTAssertFalse(policy.consumeRestoration())
    }

    func testManualOffDoesNotRearmFromItsOwnDisplaySleep() {
        var policy = LockPowerPolicy()
        policy.suppress()
        XCTAssertFalse(policy.displayDidSleep(at: 10))
        XCTAssertFalse(policy.beginShutdown(at: 10))
        XCTAssertFalse(policy.userActivity(at: 20, idleSeconds: 0))
    }

    func testSaverNotificationsAloneAndDuplicateStopsNeverRequestPower() {
        var policy = LockPowerPolicy()
        policy.screensaverStarted()
        XCTAssertFalse(policy.screensaverStopped(at: 20))
        XCTAssertFalse(policy.screensaverStopped(at: 21))
        XCTAssertFalse(policy.beginShutdown(at: 100))
        XCTAssertFalse(policy.needsObservation)
    }

    private func lockedAndShuttingDown() -> LockPowerPolicy {
        var policy = LockPowerPolicy()
        policy.screenLocked(at: 10)
        XCTAssertTrue(policy.beginShutdown(at: 12))
        return policy
    }
}
