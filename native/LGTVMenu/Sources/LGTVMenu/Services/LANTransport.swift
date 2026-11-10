import Darwin
import Foundation
import LGTVCore
import OSLog

enum LANTransport {
    private static let log = Logger(subsystem: "local.friday.LGTVMenu", category: "Wake")

    struct Interface {
        let index: UInt32
        let address: in_addr
        let mask: in_addr
        let broadcast: in_addr
    }

    static func interfaces() -> [Interface] {
        var first: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&first) == 0, let first else { return [] }
        defer { freeifaddrs(first) }
        var result: [Interface] = []
        var current: UnsafeMutablePointer<ifaddrs>? = first
        while let item = current {
            defer { current = item.pointee.ifa_next }
            let value = item.pointee
            guard let address = value.ifa_addr, address.pointee.sa_family == UInt8(AF_INET),
                  let mask = value.ifa_netmask,
                  value.ifa_flags & UInt32(IFF_UP | IFF_BROADCAST) == UInt32(IFF_UP | IFF_BROADCAST),
                  value.ifa_flags & UInt32(IFF_LOOPBACK | IFF_POINTOPOINT) == 0 else { continue }
            let ip = UnsafeRawPointer(address).assumingMemoryBound(to: sockaddr_in.self).pointee.sin_addr
            let netmask = UnsafeRawPointer(mask).assumingMemoryBound(to: sockaddr_in.self).pointee.sin_addr
            guard TVConfiguration.isLANAddress(string(ip)) else { continue }
            let index = if_nametoindex(value.ifa_name)
            guard index != 0 else { continue }
            result.append(Interface(index: index, address: ip, mask: netmask,
                                    broadcast: in_addr(s_addr: ip.s_addr | ~netmask.s_addr)))
        }
        return result
    }

    static func string(_ address: in_addr) -> String {
        var copy = address
        var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
        guard inet_ntop(AF_INET, &copy, &buffer, socklen_t(buffer.count)) != nil else { return "" }
        return String(cString: buffer)
    }

    static func destination(_ ip: in_addr, port: UInt16) -> sockaddr_in {
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr = ip
        return address
    }

    @discardableResult
    static func send(_ data: Data, socket: Int32, destination: sockaddr_in) -> Bool {
        var address = destination
        return withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { target in
                data.withUnsafeBytes { bytes in
                    sendto(socket, bytes.baseAddress, bytes.count, 0, target,
                           socklen_t(MemoryLayout<sockaddr_in>.size)) == bytes.count
                }
            }
        }
    }

    // A fresh socket per interface keeps both unicast and broadcast on the selected LAN,
    // even when a VPN installs a competing route. Never fall back to an unbound socket.
    static func boundWakeSocket(_ interface: Interface) throws -> Int32 {
        guard interface.index != 0 else { throw TVError.networkUnavailable }
        let socket = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard socket >= 0 else { throw TVError.networkUnavailable }
        var ready = false
        defer { if !ready { Darwin.close(socket) } }
        var enabled: Int32 = 1
        var index = interface.index
        guard setsockopt(socket, SOL_SOCKET, SO_BROADCAST, &enabled,
                         socklen_t(MemoryLayout<Int32>.size)) == 0,
              setsockopt(socket, IPPROTO_IP, IP_BOUND_IF, &index,
                         socklen_t(MemoryLayout<UInt32>.size)) == 0 else { throw TVError.networkUnavailable }
        var source = destination(interface.address, port: 0)
        let bound = withUnsafePointer(to: &source) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(socket, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { throw TVError.networkUnavailable }
        ready = true
        return socket
    }

    static func wake(_ configuration: TVConfiguration) throws {
        guard TVConfiguration.isLANAddress(configuration.host) else { throw TVError.invalidAddress }
        let packet = try MagicPacket.bytes(for: configuration.macAddress)
        var target = in_addr()
        guard inet_pton(AF_INET, configuration.host, &target) == 1 else { throw TVError.invalidAddress }
        let local = interfaces().filter {
            $0.address.s_addr & $0.mask.s_addr == target.s_addr & $0.mask.s_addr
        }
        guard !local.isEmpty else { throw TVError.networkUnavailable }
        var sent = false
        for interface in local {
            do {
                let socket = try boundWakeSocket(interface)
                defer { Darwin.close(socket) }
                // Some LG C4 / Wi-Fi setups wake only from a packet addressed to the TV's IP.
                // Keep subnet broadcast as a fallback when a sleeping TV no longer answers ARP.
                for (mode, address) in [("unicast", target), ("broadcast", interface.broadcast)] {
                    let success = send(packet, socket: socket, destination: destination(address, port: 9))
                    let failure = success ? 0 : errno
                    log.notice("wake_datagram interface=\(interface.index, privacy: .public) mode=\(mode, privacy: .public) sent=\(success, privacy: .public) errno=\(failure, privacy: .public)")
                    sent = success || sent
                }
            } catch {
                log.notice("wake_interface_unavailable interface=\(interface.index, privacy: .public)")
            }
        }
        guard sent else { throw TVError.networkUnavailable }
    }

    static func advertisements() throws -> [DiscoveryAdvertisement] {
        let local = interfaces()
        guard !local.isEmpty else { throw TVError.networkUnavailable }
        let socket = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard socket >= 0 else { throw TVError.discoveryFailed }
        defer { Darwin.close(socket) }
        var timeout = timeval(tv_sec: 0, tv_usec: 250000)
        _ = setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                       socklen_t(MemoryLayout<timeval>.size))
        var ttl: UInt8 = 2
        _ = setsockopt(socket, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, 1)
        var multicast = in_addr()
        _ = inet_pton(AF_INET, "239.255.255.250", &multicast)
        let request = Data(("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n" +
            "MAN: \"ssdp:discover\"\r\nMX: 2\r\nST: \(DiscoveryAdvertisement.service)\r\n\r\n").utf8)
        var sent = false
        for interface in local {
            var address = interface.address
            if setsockopt(socket, IPPROTO_IP, IP_MULTICAST_IF, &address,
                          socklen_t(MemoryLayout<in_addr>.size)) == 0 {
                sent = send(request, socket: socket, destination: destination(multicast, port: 1900)) || sent
            }
        }
        guard sent else { throw TVError.discoveryFailed }
        var result: [String: DiscoveryAdvertisement] = [:]
        let deadline = ProcessInfo.processInfo.systemUptime + 3
        var buffer = [UInt8](repeating: 0, count: 65536)
        while ProcessInfo.processInfo.systemUptime < deadline && result.count < 16 {
            try Task.checkCancellation()
            var sender = sockaddr_in()
            var length = socklen_t(MemoryLayout<sockaddr_in>.size)
            let count = withUnsafeMutablePointer(to: &sender) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { source in
                    recvfrom(socket, &buffer, buffer.count, 0, source, &length)
                }
            }
            if count > 0, let response = String(bytes: buffer.prefix(count), encoding: .utf8),
               let advertisement = DiscoveryAdvertisement(response: response, sender: string(sender.sin_addr)) {
                // Ambiguous UUIDs on different hosts are not automatically merged.
                result[advertisement.id + "@" + advertisement.host] = advertisement
            }
        }
        return Array(result.values)
    }
}

final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

enum TVDiscovery {
    static func scan() async throws -> [DiscoveredTV] {
        let work = Task.detached(priority: .utility) { try LANTransport.advertisements() }
        let advertisements = try await withTaskCancellationHandler {
            try await work.value
        } onCancel: { work.cancel() }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 3
        configuration.timeoutIntervalForResource = 4
        configuration.connectionProxyDictionary = [:]
        let session = URLSession(configuration: configuration, delegate: NoRedirectDelegate(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        var result: [DiscoveredTV] = []
        for advertisement in advertisements {
            try Task.checkCancellation()
            if let (data, response) = try? await session.data(from: advertisement.location),
               (response as? HTTPURLResponse)?.statusCode == 200,
               let tv = advertisement.device(description: data) {
                result.append(tv)
            }
        }
        return result.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }
}
