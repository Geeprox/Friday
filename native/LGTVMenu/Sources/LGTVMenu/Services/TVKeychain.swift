import Foundation
import LGTVCore
import OSLog
import Security

enum TVKeychain {
    private static let worker = KeychainWorker()
    private static let log = Logger(subsystem: "local.friday.LGTVMenu", category: "Keychain")
    private static let service = "local.friday.LGTVMenu.pairing"
    private static func query(_ id: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service, kSecAttrAccount as String: id]
    }

    static func read(_ id: String) async throws -> String? {
        try await execute {
            var lookup = query(id)
            lookup[kSecReturnData as String] = true
            lookup[kSecMatchLimit as String] = kSecMatchLimitOne
            var result: CFTypeRef?
            let status = SecItemCopyMatching(lookup as CFDictionary, &result)
            log.notice("keychain_read_finished status=\(status, privacy: .public)")
            if status == errSecItemNotFound { return nil }
            guard status == errSecSuccess, let data = result as? Data,
                  let key = String(data: data, encoding: .utf8) else { throw TVError.keychain }
            return key
        }
    }

    static func save(_ key: String, id: String) async throws {
        try await execute {
            let attributes: [String: Any] = [kSecValueData as String: Data(key.utf8)]
            let status = SecItemUpdate(query(id) as CFDictionary, attributes as CFDictionary)
            if status == errSecItemNotFound {
                let addition = query(id).merging(attributes) { _, new in new }
                guard SecItemAdd(addition as CFDictionary, nil) == errSecSuccess else { throw TVError.keychain }
            } else if status != errSecSuccess { throw TVError.keychain }
        }
    }

    static func delete(_ id: String) async throws {
        try await execute {
            let status = SecItemDelete(query(id) as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else { throw TVError.keychain }
        }
    }

    private static func execute<Value: Sendable>(_ operation: @escaping @Sendable () throws -> Value) async throws -> Value {
        do {
            return try await OperationDeadline.run(seconds: 2, cancel: {}) { completion in
                worker.perform(operation, completion: completion)
            }
        } catch is CancellationError { throw CancellationError() }
        catch { throw TVError.keychain }
    }
}

private final class KeychainWorker: @unchecked Sendable {
    private let queue = DispatchQueue(label: "local.friday.LGTVMenu.keychain", qos: .userInitiated)
    private let lock = NSLock()
    private var busy = false

    func perform<Value: Sendable>(_ operation: @escaping @Sendable () throws -> Value,
                                 completion: @escaping @Sendable (Result<Value, Error>) -> Void) {
        lock.lock()
        guard !busy else { lock.unlock(); completion(.failure(TVError.keychain)); return }
        busy = true
        lock.unlock()
        queue.async { [self] in
            // This utility uses the legacy login keychain. Data-protection-only UI
            // flags do not reliably suppress its dialogs. Never request a password
            // from a locked-screen/background operation; keep all I/O off the UI thread.
            let result = Result<Value, Error> {
                guard SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else { throw TVError.keychain }
                return try operation()
            }
            lock.lock(); busy = false; lock.unlock()
            completion(result)
        }
    }
}
