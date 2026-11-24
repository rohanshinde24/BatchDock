import BatchDockCore
import Foundation
import Testing

@testable import BatchDockWorker

@Suite struct FinishReasonTests {
  @Test(arguments: ["length", "", "future_reason"])
  func nonStopReasonNeverQualifiesAsComplete(reason: String) throws {
    let result = InferenceResult(output: "partial", metrics: [:], done: true, finishReason: reason)
    #expect(throws: WorkerError.self) { try result.requireComplete() }
  }

  @Test func missingReasonAndUnfinishedStopAreRejected() {
    let missing = InferenceResult(output: "partial", metrics: [:], done: true, finishReason: nil)
    let unfinished = InferenceResult(
      output: "partial", metrics: [:], done: false, finishReason: "stop")
    #expect(throws: WorkerError.self) { try missing.requireComplete() }
    #expect(throws: WorkerError.self) { try unfinished.requireComplete() }
  }

  @Test func observationRetainsReasonAndPartialOutputBeforeRejection() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let path = root.appendingPathComponent("generation.json").path
    let result = InferenceResult(
      output: "{\"incomplete\":", metrics: ["output_tokens": 1], done: true, finishReason: "length")
    let identity = AttemptIdentity(
      batchId: "batch", jobId: "item", epoch: 1, generation: 1, workerIncarnation: "worker")
    let record = GenerationObservation(
      attemptId: "attempt", identity: identity, configHash: "config", result: result)
    try record.persist(to: path)
    #expect(throws: WorkerError.self) { try result.requireComplete() }
    let saved = try CoreJSON.decoder().decode(
      GenerationObservation.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
    #expect(saved.result.finishReason == "length")
    #expect(saved.result.output == result.output)
    #expect(saved.identity.jobId == "item")
    let mode =
      try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? NSNumber
    #expect((mode?.intValue ?? 0o777) & 0o077 == 0)
  }

  @Test func naturalStopIsExplicitlyAccepted() throws {
    try InferenceResult(output: "kettle", metrics: [:], done: true, finishReason: "stop")
      .requireComplete()
  }
}
