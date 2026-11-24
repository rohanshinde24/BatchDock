import BatchDockCore
import Foundation

public struct OllamaSnapshot: Sendable {
  public let version: String
  public let models: [ModelIdentity]
}

public struct InferenceResult: Codable, Sendable {
  public let output: String
  public let metrics: [String: Double]
  public let done: Bool
  public let finishReason: String?

  public func requireComplete() throws {
    guard done else {
      throw WorkerError("incomplete_inference", "Runtime generation did not finish.")
    }
    guard let finishReason else {
      throw WorkerError("missing_finish_reason", "Runtime omitted its finish reason.")
    }
    switch finishReason {
    case "stop": return
    case "length":
      throw WorkerError(
        "generation_length_limited",
        "Generation reached a limit; partial output is retained locally.")
    default:
      throw WorkerError("unknown_finish_reason", "Runtime returned an unsupported finish reason.")
    }
  }
}

struct GenerationObservation: Codable, Sendable {
  let attemptId: String
  let identity: AttemptIdentity
  let configHash: String
  let result: InferenceResult

  func persist(to path: String) throws {
    try LocalState.atomicWrite(CoreJSON.encoder().encode(self), to: path)
  }
}

public struct OllamaClient: Sendable {
  private let client: APIClient
  public init(origin: URL) throws { client = try APIClient(origin: origin, requireLoopback: true) }

  public func snapshot() async throws -> OllamaSnapshot {
    struct Version: Decodable, Sendable { let version: String }
    struct Tags: Decodable, Sendable {
      struct Model: Decodable, Sendable {
        let name: String
        let digest: String
      }
      let models: [Model]
    }
    guard let version = try await client.request("GET", "/api/version", as: Version.self),
      let tags = try await client.request(
        "GET", "/api/tags", maximumBytes: 4 * 1024 * 1024, as: Tags.self)
    else {
      throw WorkerError(
        "ollama_unavailable", "Ollama did not return its version and installed models.")
    }
    return OllamaSnapshot(
      version: version.version,
      models: tags.models.map {
        ModelIdentity(tag: $0.name, digest: $0.digest, ollamaVersion: version.version)
      })
  }

  public func verify(_ identity: ModelIdentity) async throws {
    let actual = try await snapshot()
    guard
      actual.models.contains(where: {
        $0.tag == identity.tag && $0.digest == identity.digest
          && $0.ollamaVersion == identity.ollamaVersion
      })
    else {
      throw WorkerError(
        "model_changed", "The installed model or Ollama version does not match this attempt.")
    }
  }

  public func generate(_ lease: Lease) async throws -> InferenceResult {
    struct Request: Encodable {
      let model: String
      let prompt: String
      let system: String
      let stream = false
      let truncate = false
      let shift = false
      let options: InferenceOptions
    }
    struct Response: Decodable, Sendable {
      let response: String
      let done: Bool
      let doneReason: String?
      let totalDuration: Double?
      let loadDuration: Double?
      let promptEvalCount: Double?
      let evalCount: Double?
      let evalDuration: Double?
    }
    try await verify(lease.manifest.model)
    let payload = Request(
      model: lease.manifest.model.tag, prompt: lease.prompt,
      system: lease.manifest.generation.system, options: lease.manifest.generation.options)
    guard
      let response = try await client.request(
        "POST", "/api/generate", body: CoreJSON.encoder().encode(payload),
        timeout: Double(lease.attemptLimitSeconds), maximumBytes: 512 * 1024, as: Response.self)
    else {
      throw WorkerError("incomplete_inference", "Ollama did not complete the response.")
    }
    guard response.response.utf8.count <= 256 * 1024 else {
      throw WorkerError("oversized_output", "Inference output exceeds 256 KiB.")
    }
    try await verify(lease.manifest.model)
    var metrics: [String: Double] = [:]
    metrics["total_duration_ns"] = response.totalDuration
    metrics["load_duration_ns"] = response.loadDuration
    metrics["prompt_tokens"] = response.promptEvalCount
    metrics["output_tokens"] = response.evalCount
    metrics["eval_duration_ns"] = response.evalDuration
    return InferenceResult(
      output: response.response, metrics: metrics, done: response.done,
      finishReason: response.doneReason)
  }
}
