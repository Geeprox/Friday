import Foundation

struct DesktopClient {
    func request(_ path: String, method: String = "GET") async throws -> DesktopStatus {
        var request = URLRequest(url: FridayPaths.endpoint.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = method == "GET" ? 4 : 30
        request.setValue(FridayPaths.endpoint.absoluteString, forHTTPHeaderField: "Origin")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200 else {
            throw CocoaError(.fileReadUnknown)
        }
        return try JSONDecoder().decode(DesktopStatus.self, from: data)
    }
}
