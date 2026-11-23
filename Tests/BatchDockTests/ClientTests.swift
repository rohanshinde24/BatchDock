import BatchDockCore
import Foundation
import Testing

@testable import BatchDockWorker

@Suite struct ClientTests {
  @Test func testJSONLImportPreservesIdentifiersAndRejectsUnknownFields() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let manifest = BatchManifest(
      model: .init(
        tag: "model:tiny", digest: String(repeating: "a", count: 64), ollamaVersion: "test"))
    let manifestPath = root.appendingPathComponent("manifest.json").path
    let recordsPath = root.appendingPathComponent("records.jsonl").path
    try CoreJSON.encoder().encode(manifest).write(to: URL(fileURLWithPath: manifestPath))
    try Data(
      "{\"id\":\"00017\",\"prompt\":\"first\"}\n{\"id\":\"00018\",\"prompt\":\"second\"}\n".utf8
    ).write(to: URL(fileURLWithPath: recordsPath))
    let request = try InputFiles.read(manifestPath: manifestPath, recordsPath: recordsPath)
    #expect(request.records.map(\.id) == ["00017", "00018"])
    try Data("{\"id\":\"00017\",\"prompt\":\"first\",\"shell\":\"invalid\"}\n".utf8).write(
      to: URL(fileURLWithPath: recordsPath))
    #expect(throws: WorkerError.self) {
      try InputFiles.read(manifestPath: manifestPath, recordsPath: recordsPath)
    }
  }

  @Test func testExportRejectsDuplicateOrOutOfOrderInputs() throws {
    func row(_ id: String, _ ordinal: Int) -> ExportRecord {
      .init(
        id: id, ordinal: ordinal, state: "queued", output: nil, error: nil, acceptedAttemptId: nil,
        modelDigest: "digest", configHash: "hash", metrics: nil)
    }
    #expect(throws: WorkerError.self) {
      try InputFiles.decodeExport(CoreJSON.encoder().encode([row("one", 1)]))
    }
    #expect(throws: WorkerError.self) {
      try InputFiles.decodeExport(CoreJSON.encoder().encode([row("one", 0), row("one", 1)]))
    }
    let bytes = try CoreJSON.encoder().encode(row("one", 0)) + Data("\n".utf8)
    #expect(try InputFiles.decodeExport(bytes).map(\.id) == ["one"])
  }

  @Test func testPrivateAtomicFilesAndExclusiveWorkerLock() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try LocalState.ensureDirectory(root.path)
    defer { try? FileManager.default.removeItem(at: root) }
    let path = root.appendingPathComponent("token").path
    try LocalState.atomicWrite(Data("private-token\n".utf8), to: path)
    #expect(try LocalState.readToken(path) == "private-token")
    let lock = try ProcessLock(path: root.appendingPathComponent("worker.lock").path)
    #expect(throws: WorkerError.self) {
      try ProcessLock(path: root.appendingPathComponent("worker.lock").path)
    }
    withExtendedLifetime(lock) {}
    try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path)
    #expect(throws: WorkerError.self) { try LocalState.readToken(path) }
  }

  @Test func testOriginBoundaryRejectsCredentialsRedirectPathsAndPublicHTTP() throws {
    for raw in [
      "http://192.0.2.1:8787", "http://user:secret@localhost:8787", "http://localhost:8787/path",
      "http://localhost:8787?secret=value",
    ] {
      let url = try #require(URL(string: raw))
      #expect(throws: WorkerError.self) { try APIClient(origin: url) }
    }
    _ = try APIClient(origin: #require(URL(string: "http://127.0.0.1:8787")))
    let remote = try #require(URL(string: "https://example.test"))
    #expect(throws: WorkerError.self) { try APIClient(origin: remote, requireLoopback: true) }
  }
}
