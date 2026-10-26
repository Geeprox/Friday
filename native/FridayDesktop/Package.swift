// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Friday",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "Friday", targets: ["Friday"])],
    targets: [.executableTarget(name: "Friday", path: ".",
        exclude: ["Resources", "backend", "tests", "script", "README.md", "build"],
        sources: ["App", "Views", "Models", "Services", "Support"])]
)
