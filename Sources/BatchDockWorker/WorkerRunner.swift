import BatchDockCore
import Foundation

public actor WorkerControl {
  private var stopping = false
  private var paused = false
  public init() {}
  public func stop() { stopping = true }
  public func togglePause() { paused.toggle() }
  public func state() -> (stopping: Bool, paused: Bool) { (stopping, paused) }
}

public struct WorkerRunner: Sendable {
  private let coordinator: APIClient
  private let ollama: OllamaClient
  private let stateDirectory: String
  private let incarnation = UUID().uuidString
  private let control: WorkerControl
  private let log: @Sendable (String) -> Void
  private let pollInterval: Double
  private let idleGraceSeconds: Double

  public init(
    coordinator: URL, ollama: URL, token: String, stateDirectory: String,
    control: WorkerControl = WorkerControl(), pollInterval: Double = 2,
    idleGraceSeconds: Double = 10,
    log: @escaping @Sendable (String) -> Void = { print($0) }
  ) throws {
    guard pollInterval.isFinite, (0.1...60).contains(pollInterval), idleGraceSeconds.isFinite,
      (1...3600).contains(idleGraceSeconds)
    else {
      throw WorkerError(
        "invalid_worker_options",
        "Polling must be between 0.1 and 60 seconds; idle grace must be between 1 and 3600 seconds."
      )
    }
    self.coordinator = try APIClient(origin: coordinator, token: token)
    self.ollama = try OllamaClient(origin: ollama)
    self.stateDirectory = stateDirectory
    self.control = control
    self.pollInterval = pollInterval
    self.idleGraceSeconds = idleGraceSeconds
    self.log = log
  }

  public func run(stopWhenIdle: Bool = false) async throws {
    try LocalState.ensureDirectory(stateDirectory)
    let processLock = try ProcessLock(path: stateDirectory + "/worker.lock")
    defer { withExtendedLifetime(processLock) {} }
    try await replaySpools()
    let snapshot = try await ollama.snapshot()
    guard !snapshot.models.isEmpty else {
      throw WorkerError("no_models", "Install an Ollama model before starting this worker.")
    }
    log("Worker ready: \(snapshot.models.count) compatible local model(s); one active slot.")
    var pendingClaim: ClaimRequest?
    var retry = 0
    var idleSince: ContinuousClock.Instant?
    while !(await control.state().stopping) {
      if await control.state().paused {
        try await Task.sleep(for: .seconds(1))
        continue
      }
      if pendingClaim == nil {
        let current = try await ollama.snapshot()
        pendingClaim = ClaimRequest(
          claimId: UUID().uuidString, incarnation: incarnation, models: current.models)
      }
      do {
        let started = ContinuousClock.now
        let data = try await coordinator.requestData(
          "POST", "/v1/claims", body: CoreJSON.encoder().encode(pendingClaim!))
        retry = 0
        guard let data else {
          pendingClaim = nil
          if idleSince == nil { idleSince = ContinuousClock.now }
          if stopWhenIdle, let idleSince,
            ContinuousClock.now >= idleSince.advanced(by: .seconds(idleGraceSeconds))
          {
            return
          }
          try await Task.sleep(for: .seconds(pollInterval))
          continue
        }
        idleSince = nil
        guard let lease = try? CoreJSON.decoder().decode(Lease.self, from: data) else {
          if (try? CoreJSON.decoder().decode(AttemptResponse.self, from: data)) != nil {
            pendingClaim = nil
            continue
          }
          throw WorkerError("invalid_claim", "Coordinator returned an unsupported claim response.")
        }
        pendingClaim = nil
        try await execute(lease, claimStarted: started)
      } catch let failure as HTTPFailure {
        if failure.status == 401 || failure.status == 403 { throw failure }
        if !failure.retryable && failure.status != 409 { throw failure }
        if failure.status == 409 { pendingClaim = nil }
        log("Claim temporarily unavailable; retrying without exposing input data.")
        try await retryDelay(min(retry, 3))
        retry += 1
      } catch is CancellationError { return } catch let failure as WorkerError {
        throw failure
      } catch {
        log("Coordinator unavailable; preserving the pending claim identity.")
        try await retryDelay(min(retry, 3))
        retry += 1
      }
    }
  }

  private func execute(_ lease: Lease, claimStarted: ContinuousClock.Instant) async throws {
    do {
      let result = try await withThrowingTaskGroup(of: InferenceResult.self) { group in
        group.addTask { try await ollama.generate(lease) }
        group.addTask {
          var deadline = claimStarted.advanced(by: .seconds(lease.leaseSeconds * 0.8))
          let attemptDeadline = claimStarted.advanced(by: .seconds(lease.attemptLimitSeconds))
          var nextHeartbeat = ContinuousClock.now.advanced(
            by: .seconds(min(15, lease.leaseSeconds / 3)))
          while true {
            try Task.checkCancellation()
            if await control.state().stopping { throw CancellationError() }
            let now = ContinuousClock.now
            guard now < deadline, now < attemptDeadline else {
              throw WorkerError("lease_lost", "Ownership renewal or the attempt deadline elapsed.")
            }
            if now >= nextHeartbeat {
              let started = ContinuousClock.now
              do {
                guard
                  let reply = try await coordinator.request(
                    "POST", "/v1/attempts/\(lease.attemptId)/heartbeat",
                    body: CoreJSON.encoder().encode(HeartbeatRequest(identity: lease.identity)),
                    timeout: min(5, max(0.05, seconds(started.duration(to: deadline)))),
                    as: AttemptResponse.self), reply.state == "leased"
                else {
                  throw WorkerError("lease_lost", "The coordinator ended this attempt.")
                }
                deadline = started.advanced(
                  by: .seconds((reply.leaseSeconds ?? lease.leaseSeconds) * 0.8))
                nextHeartbeat = ContinuousClock.now.advanced(
                  by: .seconds(min(15, lease.leaseSeconds / 3)))
              } catch let error as HTTPFailure {
                if !error.retryable {
                  throw WorkerError("lease_lost", "The coordinator rejected ownership renewal.")
                }
                nextHeartbeat = ContinuousClock.now.advanced(by: .seconds(1))
              } catch let error as WorkerError { throw error } catch {
                nextHeartbeat = ContinuousClock.now.advanced(by: .seconds(1))
              }
            }
            try await Task.sleep(
              for: .milliseconds(max(25, min(1000, Int(lease.leaseSeconds * 1000 / 6)))))
          }
        }
        defer { group.cancelAll() }
        guard let result = try await group.next() else {
          throw WorkerError("incomplete_inference", "Inference did not return a result.")
        }
        return result
      }
      // Retain the runtime outcome before rejecting incomplete generation or publishing success.
      try GenerationObservation(
        attemptId: lease.attemptId, identity: lease.identity,
        configHash: lease.configHash, result: result
      )
      .persist(to: stateDirectory + "/generation-\(lease.attemptId).json")
      try result.requireComplete()
      let completion = CompletionRequest(
        identity: lease.identity, output: result.output, outputHash: CoreJSON.hash(result.output),
        modelDigest: lease.manifest.model.digest, ollamaVersion: lease.manifest.model.ollamaVersion,
        metrics: result.metrics)
      let spool = Spool(attemptId: lease.attemptId, completion: completion)
      let path = spoolPath(lease.attemptId)
      try LocalState.atomicWrite(CoreJSON.encoder().encode(spool), to: path)
      try await submitSpool(spool, path: path)
      log("Attempt completed and reconciled: \(lease.attemptId)")
    } catch {
      // A completion already persisted to disk must remain an immutable completion retry.
      // Reporting inference failure after an ambiguous completion could requeue accepted work.
      if FileManager.default.fileExists(atPath: spoolPath(lease.attemptId)) {
        log("Completion remains safely spooled; restart the worker to reconcile it.")
        await control.stop()
        throw WorkerError(
          "completion_pending",
          "The worker stopped with a durable completion awaiting reconciliation.")
      }
      let failure = classify(error)
      _ = try? await coordinator.request(
        "POST", "/v1/attempts/\(lease.attemptId)/fail",
        body: CoreJSON.encoder().encode(
          FailureRequest(
            identity: lease.identity, code: failure.code,
            message: failure.message, retryable: failure.retryable)),
        as: AttemptResponse.self)
      log("Attempt ended: \(failure.code)")
    }
  }

  private struct Spool: Codable, Sendable {
    let attemptId: String
    let completion: CompletionRequest
  }

  private func spoolPath(_ attempt: String) -> String {
    stateDirectory + "/completion-\(attempt).json"
  }

  private func seconds(_ duration: Duration) -> Double {
    Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18
  }

  private func replaySpools() async throws {
    for file in try FileManager.default.contentsOfDirectory(atPath: stateDirectory).sorted()
    where file.hasPrefix("completion-") && file.hasSuffix(".json") {
      let path = stateDirectory + "/" + file
      let data = try Data(contentsOf: URL(fileURLWithPath: path))
      let spool = try CoreJSON.decoder().decode(Spool.self, from: data)
      try await submitSpool(spool, path: path)
    }
  }

  private func submitSpool(_ spool: Spool, path: String) async throws {
    let body = try CoreJSON.encoder().encode(spool.completion)
    for attempt in 0..<8 {
      do {
        guard
          let response = try await coordinator.request(
            "POST", "/v1/attempts/\(spool.attemptId)/complete", body: body, as: AttemptResponse.self
          ),
          response.state == "succeeded"
        else {
          throw WorkerError("invalid_completion", "Completion returned an unsupported state.")
        }
        try FileManager.default.removeItem(atPath: path)
        return
      } catch let error as HTTPFailure {
        if error.status == 409 || error.status == 404 {
          try FileManager.default.removeItem(atPath: path)
          log("Discarded a definitively stale completion: \(spool.attemptId)")
          return
        }
        if !error.retryable { throw error }
      } catch let error as WorkerError { throw error } catch is CancellationError {
        throw CancellationError()
      } catch {}
      if attempt < 7 { try await retryDelay(attempt) }
    }
    throw WorkerError(
      "completion_pending",
      "Completion response remained ambiguous; its payload is preserved locally.")
  }

  private func classify(_ error: Error) -> (code: String, message: String, retryable: Bool) {
    if error is CancellationError { return ("worker_stopped", "Worker was interrupted.", true) }
    if let error = error as? WorkerError {
      return (
        error.code, error.message,
        !([
          "model_changed", "oversized_output", "oversized_response", "invalid_response",
          "incomplete_inference", "generation_length_limited", "missing_finish_reason",
          "unknown_finish_reason",
        ]).contains(
          error.code)
      )
    }
    if let error = error as? HTTPFailure {
      return (
        "inference_http_\(error.status)", "Inference runtime returned an HTTP error.",
        error.retryable
      )
    }
    if error is DecodingError {
      return ("invalid_inference_json", "Inference runtime returned malformed JSON.", false)
    }
    return ("inference_unavailable", "Inference runtime could not complete the request.", true)
  }
}
