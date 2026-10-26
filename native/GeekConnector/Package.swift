// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "FridayGeekConnector",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "FridayGeekConnector", targets: ["FridayGeekConnector"])],
    targets: [.executableTarget(name: "FridayGeekConnector")]
)
