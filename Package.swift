// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "BatchDock",
  platforms: [.macOS(.v14)],
  products: [
    .executable(name: "batchdock", targets: ["batchdock"]),
    .library(name: "BatchDockCore", targets: ["BatchDockCore"]),
  ],
  dependencies: [
    .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.5.0"),
    .package(url: "https://github.com/hummingbird-project/hummingbird.git", from: "2.0.0"),
    .package(url: "https://github.com/groue/GRDB.swift.git", from: "7.0.0"),
    .package(url: "https://github.com/apple/swift-log.git", exact: "1.16.2"),
    .package(url: "https://github.com/swiftlang/swift-testing.git", exact: "6.2.1"),
  ],
  targets: [
    .target(name: "BatchDockCore"),
    .target(
      name: "BatchDockStore",
      dependencies: ["BatchDockCore", .product(name: "GRDB", package: "GRDB.swift")]),
    .target(
      name: "BatchDockServer",
      dependencies: [
        "BatchDockCore", "BatchDockStore", .product(name: "Hummingbird", package: "hummingbird"),
      ]),
    .target(name: "BatchDockWorker", dependencies: ["BatchDockCore"]),
    .executableTarget(
      name: "batchdock",
      dependencies: [
        "BatchDockCore", "BatchDockWorker", "BatchDockServer",
        .product(name: "ArgumentParser", package: "swift-argument-parser"),
      ]),
    .testTarget(
      name: "BatchDockTests",
      dependencies: [
        "BatchDockCore", "BatchDockStore", "BatchDockServer", "BatchDockWorker",
        .product(name: "HummingbirdTesting", package: "hummingbird"),
        .product(name: "Testing", package: "swift-testing"),
      ]),
  ]
)
