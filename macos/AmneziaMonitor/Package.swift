// swift-tools-version:5.7
import PackageDescription

// Библиотека общая для тестов и приложения. Само приложение (SwiftUI) собирается
// целью ниже только на macOS и схемой Xcode — см. macos/README.md.
var targets: [Target] = [
    .target(
        name: "CZlibUncompress",
        path: "Sources/CZlibUncompress",
        publicHeadersPath: "include",
        linkerSettings: [.linkedLibrary("z")]
    ),
    .target(
        name: "AmneziaMonitorCore",
        dependencies: ["CZlibUncompress"],
        path: "Sources/AmneziaMonitorCore"
    ),
    .testTarget(
        name: "AmneziaMonitorCoreTests",
        dependencies: ["AmneziaMonitorCore", "CZlibUncompress"],
        path: "Tests/AmneziaMonitorCoreTests"
    ),
]

#if os(macOS)
targets.append(
    .executableTarget(
        name: "AmneziaMonitor",
        dependencies: ["AmneziaMonitorCore"],
        path: "Sources/AmneziaMonitor"
    )
)
#endif

let package = Package(
    name: "AmneziaMonitor",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "AmneziaMonitorCore", targets: ["AmneziaMonitorCore"]),
    ],
    targets: targets
)
