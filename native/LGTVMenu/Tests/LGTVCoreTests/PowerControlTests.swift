import Foundation
@testable import LGTVCore
import XCTest

final class PowerControlTests: XCTestCase {
    private let mac = ["02", "12", "34", "56", "78", "9a"].joined(separator: ":")
    private let host = "192.168.50.20"

    func testMagicPacketMatchesWireFormat() throws {
        let bytes = Array(try MagicPacket.bytes(for: mac))
        XCTAssertEqual(bytes.count, 102)
        XCTAssertEqual(Array(bytes.prefix(6)), Array(repeating: 255, count: 6))
        for offset in stride(from: 6, to: 102, by: 6) {
            XCTAssertEqual(Array(bytes[offset..<(offset + 6)]), [2, 18, 52, 86, 120, 154])
        }
    }

    func testMACFormatsAndNormalization() throws {
        XCTAssertEqual(try MagicPacket.bytes(for: mac), try MagicPacket.bytes(for: "02123456789a"))
        XCTAssertEqual(try MagicPacket.bytes(for: mac), try MagicPacket.bytes(for: "02-12-34-56-78-9A"))
        XCTAssertEqual(try MagicPacket.normalized(mac), mac.uppercased())
    }

    func testMalformedMulticastAndZeroMACsFailClosed() {
        for input in ["", "02123456789", "02123456789aff", "zz123456789a", "01123456789a",
                      "ffffffffffff", "000000000000", "2:12:34:56:78:9a", "02-12:34:56:78:9a"] {
            XCTAssertThrowsError(try MagicPacket.bytes(for: input), input)
        }
    }

    func testOnlyExplicitPrivateLANAddressesAreAccepted() {
        for address in [host, "10.20.30.40", "172.16.1.2", "172.31.1.2", "169.254.1.2"] {
            XCTAssertTrue(TVConfiguration.isLANAddress(address), address)
        }
        for address in ["127.0.0.1", "0.0.0.0", "255.255.255.255", "8.8.8.8", "172.32.1.2",
                        "192.168.1.256", "192.168.001.2", "192.168.1.2/path", "tv.local", "::1", "192.168.1.2@evil"] {
            XCTAssertFalse(TVConfiguration.isLANAddress(address), address)
        }
    }

    func testStartupAndDisplayOnlyWakeNeverTrigger() {
        var policy = WakePolicy()
        XCTAssertFalse(policy.didWake())
        XCTAssertFalse(policy.screensDidWake())
        policy.screensDidSleep()
        XCTAssertFalse(policy.screensDidWake())
    }

    func testDarkWakeWaitsForScreenWakeAndDeduplicates() {
        var policy = WakePolicy()
        policy.willSleep()
        XCTAssertFalse(policy.didWake())
        XCTAssertFalse(policy.didWake())
        XCTAssertTrue(policy.screensDidWake())
        XCTAssertFalse(policy.screensDidWake())
        XCTAssertFalse(policy.didWake())
    }

    func testScreenAndSystemNotificationsCanArriveInEitherOrder() {
        var policy = WakePolicy()
        policy.willSleep()
        XCTAssertFalse(policy.screensDidWake())
        XCTAssertTrue(policy.didWake())
        XCTAssertFalse(policy.didWake())
    }

    func testManualPowerOffSuppressesPendingWakeUntilNextSleep() {
        var policy = WakePolicy()
        policy.willSleep()
        XCTAssertFalse(policy.didWake())
        policy.suppressUntilNextSleep()
        XCTAssertFalse(policy.screensDidWake())
        XCTAssertFalse(policy.didWake())
        policy.willSleep()
        XCTAssertFalse(policy.didWake())
        XCTAssertTrue(policy.screensDidWake())
    }

    func testMultipleDarkWakeCyclesStillNeedScreenWake() {
        var policy = WakePolicy()
        for _ in 0..<3 {
            policy.willSleep()
            XCTAssertFalse(policy.didWake())
        }
        XCTAssertTrue(policy.screensDidWake())
    }

    func testIdentityChangeStopsUseOfSavedCredentials() throws {
        XCTAssertEqual(try WebOSProtocol.validateIdentity(expected: "uuid:TV-ID", actual: "tv-id"), "tv-id")
        XCTAssertThrowsError(try WebOSProtocol.validateIdentity(expected: "original", actual: "replacement"))
        XCTAssertThrowsError(try WebOSProtocol.validateIdentity(expected: nil, actual: nil))
        XCTAssertThrowsError(try WebOSProtocol.validateIdentity(expected: "saved", actual: ""))
    }

    func testHelloReplyWithoutIDReachesIdentityValidation() throws {
        // Same message shape observed on a real TV, with a synthetic identity.
        let hello: [String: Any] = [
            "type": "hello", "payload": ["deviceOS": "webOS", "deviceUUID": "synthetic-tv"]
        ]
        XCTAssertTrue(WebOSProtocol.matchesResponse(hello, requestID: "hello"))
        let payload = try XCTUnwrap(hello["payload"] as? [String: Any])
        XCTAssertEqual(try WebOSProtocol.validateIdentity(expected: nil, actual: payload["deviceUUID"] as? String), "synthetic-tv")
        XCTAssertThrowsError(try WebOSProtocol.validateIdentity(expected: "other-tv", actual: payload["deviceUUID"] as? String))
        XCTAssertTrue(WebOSProtocol.matchesResponse(["id": NSNull(), "type": "hello"], requestID: "hello"))
    }

    func testMissingIDDoesNotAcceptRegistrationOrCommandReplies() {
        for type in ["hello", "registered", "response", "error"] {
            for request in ["register", "power-off", "read-status"] {
                XCTAssertFalse(WebOSProtocol.matchesResponse(["type": type], requestID: request))
                XCTAssertFalse(WebOSProtocol.matchesResponse(["id": NSNull(), "type": type], requestID: request))
            }
        }
        XCTAssertFalse(WebOSProtocol.matchesResponse(["type": "registered"], requestID: "hello"))
    }

    func testTaggedRepliesStillRequireTheMatchingID() {
        XCTAssertTrue(WebOSProtocol.matchesResponse(["id": "register", "type": "registered"], requestID: "register"))
        XCTAssertTrue(WebOSProtocol.matchesResponse(["id": "hello", "type": "error"], requestID: "hello"))
        XCTAssertFalse(WebOSProtocol.matchesResponse(["id": "other", "type": "hello"], requestID: "hello"))
        XCTAssertFalse(WebOSProtocol.matchesResponse(["id": 7, "type": "hello"], requestID: "hello"))
    }

    func testRegistrationRequestsOnlyPowerAndNetworkReadPermissions() throws {
        let fresh = WebOSProtocol.registration(clientKey: nil)
        let payload = try XCTUnwrap(fresh["payload"] as? [String: Any])
        XCTAssertNil(payload["client-key"])
        XCTAssertEqual(payload["pairingType"] as? String, "PROMPT")
        let manifest = try XCTUnwrap(payload["manifest"] as? [String: Any])
        XCTAssertEqual(Set(try XCTUnwrap(manifest["permissions"] as? [String])),
                       Set(["CONTROL_POWER", "READ_POWER_STATE", "READ_NETWORK_STATE"]))
        let existing = WebOSProtocol.registration(clientKey: "synthetic-pairing-key")
        XCTAssertEqual((existing["payload"] as? [String: Any])?["client-key"] as? String, "synthetic-pairing-key")
    }

    func testActiveMACUsesIPMatchedInterface() {
        let other = ["02", "aa", "bb", "cc", "dd", "ee"].joined(separator: ":")
        let response: [String: Any] = [
            "wired": ["state": "connected", "ipAddress": host, "macAddress": mac],
            "wifi": ["state": "connected", "ipAddress": "192.168.50.21", "macAddress": other]
        ]
        XCTAssertEqual(WebOSProtocol.activeMAC(connection: response, host: host), mac.uppercased())
        XCTAssertNil(WebOSProtocol.activeMAC(connection: response, host: "192.168.50.22"))
        XCTAssertNil(WebOSProtocol.activeMAC(connection: ["wired": ["state": "connected", "ipAddress": "192.168.50.21", "macAddress": mac]], host: host))
        XCTAssertEqual(WebOSProtocol.activeMAC(connection: ["wifi": ["state": "connected", "macAddress": mac]], host: host), mac.uppercased())
        XCTAssertNil(WebOSProtocol.activeMAC(connection: ["wired": ["state": "disconnected", "macAddress": mac]], host: host))
    }

    func testPowerReadbackNeedsExplicitActiveState() {
        XCTAssertTrue(WebOSProtocol.reportsActive(["state": "Active"]))
        for payload: [String: Any] in [[:], ["state": "Suspend"], ["returnValue": true], ["state": "Screen Off"]] {
            XCTAssertFalse(WebOSProtocol.reportsActive(payload))
        }
    }

    private func advertisement(location: String? = nil, sender: String? = nil, extra: String = "") -> DiscoveryAdvertisement? {
        DiscoveryAdvertisement(response: "HTTP/1.1 200 OK\r\nST: \(DiscoveryAdvertisement.service)\r\n" +
            "USN: uuid:synthetic-tv::\(DiscoveryAdvertisement.service)\r\n" +
            "LOCATION: \(location ?? "http://\(host):1990/description.xml")\r\n\(extra)\r\n", sender: sender ?? host)
    }

    func testSSDPRequiresLocalSenderAndSameHostDescription() throws {
        let found = try XCTUnwrap(advertisement())
        XCTAssertEqual(found.id, "synthetic-tv")
        XCTAssertEqual(found.host, host)
        for location in ["http://8.8.8.8/", "http://127.0.0.1/", "file:///etc/passwd", "https://\(host)/",
                         "http://\(host)@example.org/", "http://\(host):0/"] {
            XCTAssertNil(advertisement(location: location), location)
        }
        XCTAssertNil(advertisement(sender: "8.8.8.8"))
        XCTAssertNil(advertisement(extra: "LOCATION: http://\(host)/another\r\n"))
    }

    func testDescriptionMustMatchAdvertisedStableID() throws {
        let found = try XCTUnwrap(advertisement())
        let valid = Data("<root><device><friendlyName>Synthetic LG</friendlyName><UDN>uuid:synthetic-tv</UDN></device></root>".utf8)
        XCTAssertEqual(found.device(description: valid)?.name, "Synthetic LG")
        XCTAssertNil(found.device(description: Data("<root><UDN>uuid:other</UDN></root>".utf8)))
        XCTAssertNil(found.device(description: Data(repeating: 32, count: 262145)))
    }
}
