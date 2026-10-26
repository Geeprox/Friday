import Foundation

enum FridayPaths {
    static let base = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Friday", isDirectory: true)
    static let endpoint = URL(string: "http://127.0.0.1:8740")!
    static let authorization = URL(string: "http://127.0.0.1:8740/desktop/authorization")!
    static var resources: URL { Bundle.main.resourceURL! }
    static var product: URL { resources.appendingPathComponent("product", isDirectory: true) }
    static let environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"]
}
