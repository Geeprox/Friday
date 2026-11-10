// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LGTVMenu",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "LGTVMenu", targets: ["LGTVMenu"])],
    targets: [
        .target(name: "LGTVCore"),
        .target(name: "LGTVDSP"),
        .executableTarget(name: "LGTVMenu", dependencies: ["LGTVCore", "LGTVDSP"]),
        .testTarget(name: "LGTVCoreTests", dependencies: ["LGTVCore", "LGTVMenu", "LGTVDSP"])
    ]
)
