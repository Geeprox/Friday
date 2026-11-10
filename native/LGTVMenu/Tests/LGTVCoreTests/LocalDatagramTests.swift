import Darwin
import Foundation
import XCTest
@testable import LGTVMenu

final class LocalDatagramTests: XCTestCase {
    // Uses only loopback; no TV, LAN broadcast, or other device is contacted.
    func testInterfaceBoundDatagramReachesLocalReceiver() throws {
        var loopback = in_addr()
        XCTAssertEqual(inet_pton(AF_INET, "127.0.0.1", &loopback), 1)
        let interface = LANTransport.Interface(index: if_nametoindex("lo0"), address: loopback,
                                               mask: in_addr(s_addr: UInt32.max), broadcast: loopback)
        let receiver = try LANTransport.boundWakeSocket(interface)
        defer { Darwin.close(receiver) }
        let sender = try LANTransport.boundWakeSocket(interface)
        defer { Darwin.close(sender) }

        var boundIndex: UInt32 = 0
        var length = socklen_t(MemoryLayout<UInt32>.size)
        XCTAssertEqual(getsockopt(sender, IPPROTO_IP, IP_BOUND_IF, &boundIndex, &length), 0)
        XCTAssertEqual(boundIndex, if_nametoindex("lo0"))
        var address = sockaddr_in()
        length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let result = withUnsafeMutablePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(receiver, $0, &length) }
        }
        XCTAssertEqual(result, 0)
        XCTAssertEqual(address.sin_addr.s_addr, loopback.s_addr)
        let payload = Data("local transport verification".utf8)
        XCTAssertTrue(LANTransport.send(payload, socket: sender, destination: address))
        var timeout = timeval(tv_sec: 1, tv_usec: 0)
        XCTAssertEqual(setsockopt(receiver, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                                 socklen_t(MemoryLayout<timeval>.size)), 0)
        var bytes = [UInt8](repeating: 0, count: 256)
        let count = recv(receiver, &bytes, bytes.count, 0)
        XCTAssertEqual(count, payload.count)
        if count >= 0 { XCTAssertEqual(Data(bytes.prefix(count)), payload) }
    }

    func testMissingInterfaceDoesNotUseUnboundFallback() {
        let empty = in_addr(s_addr: 0)
        let interface = LANTransport.Interface(index: 0, address: empty, mask: empty, broadcast: empty)
        XCTAssertThrowsError(try LANTransport.boundWakeSocket(interface))
    }
}
