import Foundation
import Hummingbird
import HummingbirdTesting
import Testing

@testable import BatchDockCore
@testable import BatchDockServer
@testable import BatchDockStore

private final class FakeClock: @unchecked Sendable {
  private let lock = NSLock()
  private var value: Double = 0
  func now() -> Double { lock.withLock { value } }
  func advance(_ seconds: Double) { lock.withLock { value += seconds } }
}

private func temporaryDatabase() throws -> String {
  let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
  try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
  return directory.appendingPathComponent("queue.sqlite").path
}
private let model = ModelIdentity(
  tag: "test:small", digest: String(repeating: "a", count: 64), ollamaVersion: "test-runtime")
private func batch(_ count: Int = 1) -> SubmitBatchRequest {
  .init(
    manifest: .init(model: model),
    records: (0..<count).map { .init(id: "item-\($0)", prompt: "prompt \($0)") })
}
private func claim(_ id: String = UUID().uuidString, incarnation: String = UUID().uuidString)
  -> ClaimRequest
{ .init(claimId: id, incarnation: incarnation, models: [model]) }
private func completion(_ lease: Lease, output: String = "result") -> CompletionRequest {
  .init(
    identity: lease.identity, output: output, outputHash: CoreJSON.hash(output),
    modelDigest: model.digest, ollamaVersion: model.ollamaVersion, metrics: ["duration": 1])
}

@Suite struct QueueStoreTests {
  @Test func concurrentClaimsYieldOneOwner() async throws {
    let store = try QueueStore(path: temporaryDatabase())
    let a = try await store.enroll(name: "A")
    let b = try await store.enroll(name: "B")
    _ = try await store.submit(batch(), key: UUID().uuidString)
    async let first = store.claim(claim(), token: a.token)
    async let second = store.claim(claim(), token: b.token)
    let results = try await [first, second]
    #expect(results.compactMap { $0 }.count == 1)
  }
  @Test func submissionIsAtomicAndIdempotent() async throws {
    let store = try QueueStore(path: temporaryDatabase())
    let key = UUID().uuidString
    let first = try await store.submit(batch(), key: key)
    let repeated = try await store.submit(batch(), key: key)
    #expect(first.batchId == repeated.batchId)
    #expect(repeated.replayed)
    await #expect(throws: APIError.self) { try await store.submit(batch(2), key: key) }
    await #expect(throws: APIError.self) {
      try await store.submit(
        .init(
          manifest: .init(model: model),
          records: [.init(id: "x", prompt: "a"), .init(id: "x", prompt: "b")]),
        key: UUID().uuidString)
    }
  }
  @Test func claimReplayDoesNotRenewLease() async throws {
    let clock = FakeClock()
    let store = try QueueStore(path: temporaryDatabase(), leaseSeconds: 10, now: { clock.now() })
    let worker = try await store.enroll(name: "A")
    _ = try await store.submit(batch(), key: UUID().uuidString)
    let request = claim()
    let first = try #require(try await store.claim(request, token: worker.token))
    clock.advance(8)
    let repeated = try #require(try await store.claim(request, token: worker.token))
    #expect(first.attemptId == repeated.attemptId)
    #expect(repeated.leaseSeconds == 2)
    clock.advance(3)
    await #expect(throws: APIError.self) { try await store.claim(request, token: worker.token) }
  }
  @Test func lateWorkerCannotOverwriteNewAttempt() async throws {
    let clock = FakeClock()
    let store = try QueueStore(path: temporaryDatabase(), leaseSeconds: 10, now: { clock.now() })
    let a = try await store.enroll(name: "A")
    let b = try await store.enroll(name: "B")
    let submitted = try await store.submit(batch(), key: UUID().uuidString)
    let old = try #require(try await store.claim(claim(), token: a.token))
    clock.advance(11)
    let current = try #require(try await store.claim(claim(), token: b.token))
    #expect(current.generation > old.generation)
    _ = try await store.complete(
      attemptId: current.attemptId, request: completion(current), token: b.token)
    await #expect(throws: APIError.self) {
      try await store.complete(
        attemptId: old.attemptId, request: completion(old, output: "stale"), token: a.token)
    }
    let rows = try await store.export(batchId: submitted.batchId)
    #expect(rows.count == 1)
    #expect(rows[0].output == "result")
  }
  @Test func completionReplayAndChangedPayload() async throws {
    let store = try QueueStore(path: temporaryDatabase())
    let worker = try await store.enroll(name: "A")
    _ = try await store.submit(batch(), key: UUID().uuidString)
    let lease = try #require(try await store.claim(claim(), token: worker.token))
    let request = completion(lease)
    _ = try await store.complete(attemptId: lease.attemptId, request: request, token: worker.token)
    let repeated = try await store.complete(
      attemptId: lease.attemptId, request: request, token: worker.token)
    #expect(repeated.replayed)
    await #expect(throws: APIError.self) {
      try await store.complete(
        attemptId: lease.attemptId, request: completion(lease, output: "different"),
        token: worker.token)
    }
    var dishonest = completion(lease)
    dishonest.output = "different"
    await #expect(throws: APIError.self) {
      try await store.complete(attemptId: lease.attemptId, request: dishonest, token: worker.token)
    }
  }
  @Test func restartFencesLeasesAndRetainsAcceptedResults() async throws {
    let path = try temporaryDatabase()
    let original = try QueueStore(path: path)
    let worker = try await original.enroll(name: "A")
    let submitted = try await original.submit(batch(2), key: UUID().uuidString)
    let accepted = try #require(try await original.claim(claim(), token: worker.token))
    _ = try await original.complete(
      attemptId: accepted.attemptId, request: completion(accepted), token: worker.token)
    let old = try #require(try await original.claim(claim(), token: worker.token))
    let restarted = try QueueStore(path: path)
    await #expect(throws: APIError.self) {
      try await restarted.complete(
        attemptId: old.attemptId, request: completion(old), token: worker.token)
    }
    let new = try #require(try await restarted.claim(claim(), token: worker.token))
    #expect(new.epoch > old.epoch)
    _ = try await restarted.complete(
      attemptId: new.attemptId, request: completion(new), token: worker.token)
    #expect(
      try await restarted.export(batchId: submitted.batchId).filter { $0.state == "succeeded" }
        .count == 2)
  }
  @Test func cancellationPreservesCompletedRows() async throws {
    let store = try QueueStore(path: temporaryDatabase())
    let worker = try await store.enroll(name: "A")
    let submitted = try await store.submit(batch(3), key: UUID().uuidString)
    let first = try #require(try await store.claim(claim(), token: worker.token))
    _ = try await store.complete(
      attemptId: first.attemptId, request: completion(first), token: worker.token)
    let active = try #require(try await store.claim(claim(), token: worker.token))
    let status = try await store.cancel(batchId: submitted.batchId)
    #expect(status.counts["succeeded"] == 1)
    #expect(status.counts["cancelled"] == 2)
    await #expect(throws: APIError.self) {
      try await store.complete(
        attemptId: active.attemptId, request: completion(active), token: worker.token)
    }
  }
  @Test func attemptBudgetAndWorkerCapacity() async throws {
    let clock = FakeClock()
    let store = try QueueStore(path: temporaryDatabase(), leaseSeconds: 2, now: { clock.now() })
    let worker = try await store.enroll(name: "A")
    let submitted = try await store.submit(batch(), key: UUID().uuidString)
    for _ in 0..<3 {
      _ = try #require(try await store.claim(claim(), token: worker.token))
      await #expect(throws: APIError.self) { try await store.claim(claim(), token: worker.token) }
      clock.advance(3)
    }
    #expect(try await store.status(batchId: submitted.batchId).counts["failed"] == 1)
    #expect(try await store.claim(claim(), token: worker.token) == nil)
  }
  @Test func authenticationAndCompatibility() async throws {
    let store = try QueueStore(path: temporaryDatabase())
    let worker = try await store.enroll(name: "A")
    _ = try await store.submit(batch(), key: UUID().uuidString)
    var incompatible = claim()
    incompatible.models[0].digest = String(repeating: "b", count: 64)
    #expect(try await store.claim(incompatible, token: worker.token) == nil)
    await #expect(throws: APIError.self) { try await store.claim(claim(), token: "invalid") }
    try await store.revoke(workerId: worker.workerId)
    await #expect(throws: APIError.self) { try await store.claim(claim(), token: worker.token) }
  }
  @Test func failureReplaySurvivesRestartAndRejectsChanges() async throws {
    let path = try temporaryDatabase()
    let store = try QueueStore(path: path)
    let worker = try await store.enroll(name: "A")
    _ = try await store.submit(batch(), key: UUID().uuidString)
    let lease = try #require(try await store.claim(claim(), token: worker.token))
    let failure = FailureRequest(
      identity: lease.identity, code: "unavailable", message: "Try again", retryable: true)
    let first = try await store.fail(
      attemptId: lease.attemptId, request: failure, token: worker.token)
    #expect(first.state == "queued")
    #expect(!first.replayed)
    let restarted = try QueueStore(path: path)
    let replay = try await restarted.fail(
      attemptId: lease.attemptId, request: failure, token: worker.token)
    #expect(replay.state == "queued")
    #expect(replay.replayed)
    var changed = failure
    changed.retryable = false
    await #expect(throws: APIError.self) {
      try await restarted.fail(attemptId: lease.attemptId, request: changed, token: worker.token)
    }
  }
  @Test func exportPreflightBoundsEscapedPayloads() async throws {
    let store = try QueueStore(path: temporaryDatabase(), exportByteLimit: 4096)
    let worker = try await store.enroll(name: "A")
    let submitted = try await store.submit(batch(), key: UUID().uuidString)
    #expect(try await store.export(batchId: submitted.batchId).count == 1)
    let lease = try #require(try await store.claim(claim(), token: worker.token))
    _ = try await store.complete(
      attemptId: lease.attemptId,
      request: completion(lease, output: String(repeating: "\u{0001}", count: 1000)),
      token: worker.token)
    do {
      _ = try await store.export(batchId: submitted.batchId)
      Issue.record("Expected export bound")
    } catch let error as APIError {
      #expect(error.status == 413)
      #expect(error.code == "export_too_large")
    }
  }
  @Test func HTTPAuthStrictInputAndReplay() async throws {
    let store = try QueueStore(path: temporaryDatabase())
    let app = Application(router: BatchDockServer.router(store: store, ownerToken: "owner"))
    try await app.test(.router) { client in
      try await client.execute(uri: "/v1/batches/x", method: .get) { response in
        #expect(response.status == .unauthorized)
      }
      let request = try CoreJSON.encoder().encode(batch())
      let key = UUID().uuidString
      let headers: HTTPFields = [
        .authorization: "Bearer owner", .contentType: "application/json",
        .init("Idempotency-Key")!: key,
      ]
      try await client.execute(
        uri: "/v1/batches", method: .post, headers: headers, body: ByteBuffer(bytes: request)
      ) { response in #expect(response.status == .created) }
      try await client.execute(
        uri: "/v1/batches", method: .post, headers: headers, body: ByteBuffer(bytes: request)
      ) { response in #expect(response.status == .ok) }
      var object = try #require(try JSONSerialization.jsonObject(with: request) as? [String: Any])
      object["unsupported"] = true
      let unsupported = try JSONSerialization.data(withJSONObject: object)
      try await client.execute(
        uri: "/v1/batches", method: .post, headers: headers, body: ByteBuffer(bytes: unsupported)
      ) { response in #expect(response.status == .badRequest) }
    }
  }
}
