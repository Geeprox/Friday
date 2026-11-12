import Foundation
import XCTest
@testable import LGTVCore

final class CredentialMigrationTests: XCTestCase {
    func testLegacySavedConfigurationStillFindsItsExistingCredential() throws {
        let legacy = Data(#"{"id":"fixture-tv","name":"Fixture","host":"192.0.2.50","macAddress":"","useTLS":true,"deviceUUID":"fixture-device"}"#.utf8)
        let saved = try JSONDecoder().decode(TVConfiguration.self, from: legacy)
        XCTAssertEqual(saved.keychainAccount, "fixture-tv")
        XCTAssertNil(saved.pairingKeyID)
        XCTAssertEqual(saved.deviceUUID, "fixture-device")
    }

    func testCredentialRotationPersistsWithoutChangingTheTVIdentity() throws {
        let before = TVConfiguration(id: "fixture-tv", name: "Fixture", host: "192.0.2.50",
                                     deviceUUID: "fixture-device", pairingKeyID: "previous-account")
        var after = before
        after.pairingKeyID = "new-account"
        let decoded = try JSONDecoder().decode(TVConfiguration.self, from: JSONEncoder().encode(after))
        XCTAssertEqual(decoded.id, before.id)
        XCTAssertEqual(decoded.deviceUUID, before.deviceUUID)
        XCTAssertEqual(before.keychainAccount, "previous-account")
        XCTAssertEqual(decoded.keychainAccount, "new-account")
    }
}
