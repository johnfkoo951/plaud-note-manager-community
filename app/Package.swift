// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "PlaudNoteApp",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "PlaudNoteApp", targets: ["PlaudNoteApp"])
    ],
    dependencies: [
        .package(url: "https://github.com/groue/GRDB.swift.git", from: "6.29.0")
    ],
    targets: [
        .executableTarget(
            name: "PlaudNoteApp",
            dependencies: [.product(name: "GRDB", package: "GRDB.swift")],
            path: "Sources/PlaudNoteApp"
        )
    ]
)
