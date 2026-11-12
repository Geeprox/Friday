import CryptoKit
import Foundation
import LGTVCore
import OSLog
import Security

private final class TVTrustDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    private let host: String
    private let expected: String?
    private let pairing: Bool
    private let lock = NSLock()
    private var captured: String?

    init(host: String, expected: String?, pairing: Bool) {
        self.host = host; self.expected = expected; self.pairing = pairing
    }

    var fingerprint: String? {
        lock.lock(); defer { lock.unlock() }
        return captured
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == host, challenge.protectionSpace.port == 3001,
              let trust = challenge.protectionSpace.serverTrust,
              let certificates = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let certificate = certificates.first else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data)
            .map { String(format: "%02x", $0) }.joined()
        // Trust on first use is limited to the user's explicit TV pairing.
        guard expected == digest || (expected == nil && pairing) else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        lock.lock(); captured = digest; lock.unlock()
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

@MainActor
final class WebOSClient {
    private let log = Logger(subsystem: "local.friday.LGTVMenu", category: "Pairing")
    private let session: URLSession
    private let socket: URLSessionWebSocketTask
    private let trust: TVTrustDelegate
    private let audioControl: Bool
    private(set) var configuration: TVConfiguration
    private(set) var clientKey: String?

    init(configuration: TVConfiguration, clientKey: String?, pairing: Bool,
         audioControl: Bool = false, persistent: Bool = false) throws {
        guard TVConfiguration.isLANAddress(configuration.host) else { throw TVError.invalidAddress }
        self.configuration = configuration
        self.clientKey = clientKey
        self.audioControl = audioControl
        trust = TVTrustDelegate(host: configuration.host, expected: configuration.certificateFingerprint, pairing: pairing)
        let options = URLSessionConfiguration.ephemeral
        options.timeoutIntervalForRequest = 5
        options.timeoutIntervalForResource = persistent ? 3600 : (pairing ? 70 : 8)
        options.connectionProxyDictionary = [:]
        session = URLSession(configuration: options, delegate: trust, delegateQueue: nil)
        let scheme = configuration.useTLS ? "wss" : "ws"
        let port = configuration.useTLS ? 3001 : 3000
        let url = URL(string: "\(scheme)://\(configuration.host):\(port)/")!
        socket = session.webSocketTask(with: url)
        socket.maximumMessageSize = 65536
        socket.resume()
    }

    func close() {
        socket.cancel(with: .goingAway, reason: nil)
        session.invalidateAndCancel()
    }

    func connect(pairing: Bool, onPrompt: (() -> Void)? = nil) async throws {
        if !pairing && clientKey == nil { throw TVError.pairingRequired }
        try await send(["id": "hello", "type": "hello", "payload": [:]])
        let hello = try await matching(id: "hello", seconds: 5)
        guard hello["type"] as? String == "hello",
              let info = hello["payload"] as? [String: Any] else { throw TVError.invalidResponse }
        configuration.deviceUUID = try WebOSProtocol.validateIdentity(
            expected: configuration.deviceUUID, actual: info["deviceUUID"] as? String)
        // Recent webOS versions expect this read before registration.
        do { _ = try await request("system/getSystemInfo", id: "system-info", seconds: 3) }
        catch TVError.commandRejected { /* Older firmware may not expose this optional read. */ }
        try Task.checkCancellation()
        try await send(WebOSProtocol.registration(clientKey: clientKey, audioControl: audioControl))
        let deadline = ProcessInfo.processInfo.systemUptime + (pairing ? 60 : 4)
        while ProcessInfo.processInfo.systemUptime < deadline {
            let response = try await matching(id: "register", seconds: deadline - ProcessInfo.processInfo.systemUptime)
            let type = response["type"] as? String
            let payload = response["payload"] as? [String: Any] ?? [:]
            if type == "registered", let key = payload["client-key"] as? String, !key.isEmpty {
                log.notice("registration_accepted")
                clientKey = key
                configuration.certificateFingerprint = trust.fingerprint
                if configuration.useTLS && configuration.certificateFingerprint == nil { throw TVError.certificateChanged }
                return
            }
            if type == "error" { throw pairing ? TVError.pairingRejected : TVError.pairingRequired }
            if payload["pairingType"] as? String == "PROMPT" && !pairing {
                // Background work never waits for or approves a new pairing.
                throw TVError.pairingRequired
            }
            if payload["pairingType"] as? String == "PROMPT" {
                log.notice("registration_prompt_state_received")
                onPrompt?()
            }
        }
        throw TVError.timeout
    }

    func networkMAC() async -> String? {
        guard let payload = try? await request("com.webos.service.connectionmanager/getinfo") else { return nil }
        return WebOSProtocol.activeMAC(connection: payload, host: configuration.host)
    }

    func reportsActive() async throws -> Bool {
        WebOSProtocol.reportsActive(try await request("com.webos.service.tvpower/power/getPowerState"))
    }

    func audioState() async throws -> TVAudioState {
        let volume = try await request("audio/getVolume")
        var state = TVAudioState.parse(volume)
        if let status = try? await request("audio/getStatus") { state = TVAudioState.parse(status, previous: state) ?? state }
        guard let state else { throw TVError.audioUnavailable }
        return state
    }

    func setVolume(_ volume: Int) async throws {
        guard (0...100).contains(volume) else { throw TVError.invalidVolume }
        _ = try await request("audio/setVolume", payload: ["volume": volume])
    }

    func setMuted(_ muted: Bool) async throws {
        _ = try await request("audio/setMute", payload: ["mute": muted])
    }

    func monitorAudio(_ onUpdate: @escaping @MainActor ([String: Any]) -> Void) async throws {
        let reader = Task { @MainActor in
            try await send(["id": "audio-status", "type": "subscribe", "uri": "ssap://audio/getStatus", "payload": [:]])
            try await send(["id": "audio-volume", "type": "subscribe", "uri": "ssap://audio/getVolume", "payload": [:]])
            while true {
                try Task.checkCancellation()
                let bytes = try await receive(seconds: 3600)
                guard let response = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                      let id = response["id"] as? String, ["audio-status", "audio-volume"].contains(id) else { continue }
                guard response["type"] as? String == "response",
                      let payload = response["payload"] as? [String: Any], payload["returnValue"] as? Bool != false else { throw TVError.audioUnavailable }
                onUpdate(payload)
            }
        }
        var heartbeatFailure: Error?
        let heartbeat = Task { @MainActor in
            do {
                while true {
                    try await Task.sleep(nanoseconds: 15_000_000_000)
                    let task = socket
                    let _: Data = try await OperationDeadline.run(seconds: 4, cancel: { task.cancel(with: .goingAway, reason: nil) }) { completion in
                        task.sendPing { error in
                            if let error { completion(.failure(error)) } else { completion(.success(Data())) }
                        }
                    }
                }
            } catch is CancellationError { }
            catch { heartbeatFailure = error; reader.cancel() }
        }
        defer { reader.cancel(); heartbeat.cancel() }
        do {
            try await withTaskCancellationHandler { try await reader.value } onCancel: { reader.cancel() }
        } catch {
            if let heartbeatFailure { throw heartbeatFailure }
            throw error
        }
    }

    /// A dropped socket after send is ambiguous; distinguish it from an ACK.
    func turnOff() async throws -> Bool {
        let id = UUID().uuidString
        try await send(["id": id, "type": "request", "uri": "ssap://system/turnOff", "payload": [:]])
        let response: [String: Any]
        do { response = try await matching(id: id, seconds: 4) }
        catch is CancellationError { throw CancellationError() }
        catch { return false }
        guard response["type"] as? String == "response",
              let payload = response["payload"] as? [String: Any],
              payload["returnValue"] as? Bool == true else { throw TVError.commandRejected }
        return true
    }

    private func request(_ uri: String, id: String = UUID().uuidString, seconds: Double = 4,
                         payload: [String: Any] = [:]) async throws -> [String: Any] {
        try await send(["id": id, "type": "request", "uri": "ssap://" + uri, "payload": payload])
        let response = try await matching(id: id, seconds: seconds)
        if uri.hasPrefix("audio/"), let error = response["error"] as? String,
           error.hasPrefix("401") || error.hasPrefix("403") { throw TVError.audioPermissionRequired }
        guard response["type"] as? String == "response", let payload = response["payload"] as? [String: Any],
              payload["returnValue"] as? Bool != false else { throw TVError.commandRejected }
        return payload
    }

    private func send(_ value: [String: Any]) async throws {
        try Task.checkCancellation()
        let data = try JSONSerialization.data(withJSONObject: value)
        let task = socket
        // URLSession queues sends while connecting. Bound this separately from the
        // much longer interval reserved for the human's TV pairing prompt.
        let _: Data = try await OperationDeadline.run(seconds: 5, cancel: {
            task.cancel(with: .goingAway, reason: nil)
        }) { completion in
            task.send(.string(String(decoding: data, as: UTF8.self))) { error in
                if let error { completion(.failure(error)) }
                else { completion(.success(Data())) }
            }
        }
    }

    private func matching(id: String, seconds: Double) async throws -> [String: Any] {
        let deadline = ProcessInfo.processInfo.systemUptime + max(0, seconds)
        for _ in 0..<32 {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { throw TVError.timeout }
            let bytes = try await receive(seconds: remaining)
            guard let value = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
                throw TVError.invalidResponse
            }
            if WebOSProtocol.matchesResponse(value, requestID: id) { return value }
        }
        throw TVError.invalidResponse
    }

    private func receive(seconds: Double) async throws -> Data {
        let task = socket
        return try await OperationDeadline.run(seconds: seconds, cancel: {
            task.cancel(with: .goingAway, reason: nil)
        }) { completion in
            task.receive { result in
                completion(result.flatMap { message in
                    switch message {
                    case .data(let bytes): return .success(bytes)
                    case .string(let string): return .success(Data(string.utf8))
                    @unknown default: return .failure(TVError.invalidResponse)
                    }
                })
            }
        }
    }
}
