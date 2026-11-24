// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "Lumen",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "Lumen", targets: ["Lumen"])],
    targets: [.executableTarget(name: "Lumen")]
)
