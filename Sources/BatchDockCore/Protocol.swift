import CryptoKit
import Foundation

public enum CoreJSON {
  public static func encoder() -> JSONEncoder {
    let e = JSONEncoder()
    e.keyEncodingStrategy = .convertToSnakeCase
    e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    return e
  }
  public static func decoder() -> JSONDecoder {
    let d = JSONDecoder()
    d.keyDecodingStrategy = .convertFromSnakeCase
    return d
  }
  public static func hash(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
  }
  public static func hash(_ string: String) -> String { hash(Data(string.utf8)) }
  public static func digest<T: Encodable>(_ value: T) throws -> String {
    hash(try encoder().encode(value))
  }
}

public struct ModelIdentity: Codable, Sendable, Equatable {
  public var tag: String
  public var digest: String
  public var ollamaVersion: String
  public init(tag: String, digest: String, ollamaVersion: String) {
    self.tag = tag
    self.digest = digest
    self.ollamaVersion = ollamaVersion
  }
}
public struct InferenceOptions: Codable, Sendable, Equatable {
  public var temperature: Double
  public var seed: Int
  public var numCtx: Int
  public var numPredict: Int
  public init(temperature: Double = 0, seed: Int = 42, numCtx: Int = 4096, numPredict: Int = 256) {
    self.temperature = temperature
    self.seed = seed
    self.numCtx = numCtx
    self.numPredict = numPredict
  }
}
public struct GenerationConfig: Codable, Sendable, Equatable {
  public var system: String
  public var format: String
  public var options: InferenceOptions
  public init(system: String = "", format: String = "text", options: InferenceOptions = .init()) {
    self.system = system
    self.format = format
    self.options = options
  }
}
public struct BatchManifest: Codable, Sendable, Equatable {
  public var protocolVersion: Int
  public var model: ModelIdentity
  public var generation: GenerationConfig
  public var attemptLimitSeconds: Int
  public init(
    protocolVersion: Int = 1, model: ModelIdentity, generation: GenerationConfig = .init(),
    attemptLimitSeconds: Int = 600
  ) {
    self.protocolVersion = protocolVersion
    self.model = model
    self.generation = generation
    self.attemptLimitSeconds = attemptLimitSeconds
  }
  public func validate() throws {
    guard protocolVersion == 1, generation.format == "text", !model.tag.isEmpty,
      !model.ollamaVersion.isEmpty,
      model.digest.utf8.count == 64,
      model.digest.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
      (1...600).contains(attemptLimitSeconds), (1...4096).contains(generation.options.numCtx),
      (1...4096).contains(generation.options.numPredict),
      (0...2).contains(generation.options.temperature), generation.system.utf8.count <= 32_000
    else { throw APIError.badRequest("invalid_manifest", "Unsupported or out-of-bounds manifest") }
  }
}
public struct InputRecord: Codable, Sendable, Equatable {
  public var id: String
  public var prompt: String
  public init(id: String, prompt: String) {
    self.id = id
    self.prompt = prompt
  }
}
public struct SubmitBatchRequest: Codable, Sendable {
  public var manifest: BatchManifest
  public var records: [InputRecord]
  public init(manifest: BatchManifest, records: [InputRecord]) {
    self.manifest = manifest
    self.records = records
  }
  public func validate() throws {
    try manifest.validate()
    guard !records.isEmpty, records.count <= 10_000, Set(records.map(\.id)).count == records.count
    else {
      throw APIError.badRequest("invalid_records", "Provide 1 to 10000 records with unique IDs")
    }
    guard
      records.allSatisfy({
        !$0.id.isEmpty && $0.id.utf8.count <= 256 && $0.prompt.utf8.count <= 32_000
      })
    else { throw APIError.badRequest("invalid_records", "Record ID or prompt exceeds limits") }
    guard try CoreJSON.encoder().encode(self).count <= 50 * 1024 * 1024 else {
      throw APIError.badRequest("input_too_large", "Batch exceeds 50 MiB")
    }
  }
}
public struct SubmitBatchResponse: Codable, Sendable {
  public var batchId: String
  public var replayed: Bool
  public init(batchId: String, replayed: Bool) {
    self.batchId = batchId
    self.replayed = replayed
  }
}
public struct WorkerEnrollmentRequest: Codable, Sendable {
  public var name: String
  public init(name: String) { self.name = name }
}
public struct WorkerEnrollment: Codable, Sendable {
  public var workerId: String
  public var token: String
  public init(workerId: String, token: String) {
    self.workerId = workerId
    self.token = token
  }
}
public struct ClaimRequest: Codable, Sendable {
  public var claimId: String
  public var incarnation: String
  public var models: [ModelIdentity]
  public init(claimId: String, incarnation: String, models: [ModelIdentity]) {
    self.claimId = claimId
    self.incarnation = incarnation
    self.models = models
  }
}
public struct AttemptIdentity: Codable, Sendable, Equatable {
  public var batchId: String
  public var jobId: String
  public var epoch: Int
  public var generation: Int
  public var workerIncarnation: String
  public init(
    batchId: String, jobId: String, epoch: Int, generation: Int, workerIncarnation: String
  ) {
    self.batchId = batchId
    self.jobId = jobId
    self.epoch = epoch
    self.generation = generation
    self.workerIncarnation = workerIncarnation
  }
}
public struct Lease: Codable, Sendable {
  public var attemptId: String
  public var batchId: String
  public var jobId: String
  public var epoch: Int
  public var generation: Int
  public var workerIncarnation: String
  public var leaseSeconds: Double
  public var attemptLimitSeconds: Int
  public var configHash: String
  public var manifest: BatchManifest
  public var prompt: String
  public init(
    attemptId: String, batchId: String, jobId: String, epoch: Int, generation: Int,
    workerIncarnation: String, leaseSeconds: Double, attemptLimitSeconds: Int, configHash: String,
    manifest: BatchManifest, prompt: String
  ) {
    self.attemptId = attemptId
    self.batchId = batchId
    self.jobId = jobId
    self.epoch = epoch
    self.generation = generation
    self.workerIncarnation = workerIncarnation
    self.leaseSeconds = leaseSeconds
    self.attemptLimitSeconds = attemptLimitSeconds
    self.configHash = configHash
    self.manifest = manifest
    self.prompt = prompt
  }
  public var identity: AttemptIdentity {
    .init(
      batchId: batchId, jobId: jobId, epoch: epoch, generation: generation,
      workerIncarnation: workerIncarnation)
  }
}
public struct CompletionRequest: Codable, Sendable {
  public var identity: AttemptIdentity
  public var output: String
  public var outputHash: String
  public var modelDigest: String
  public var ollamaVersion: String
  public var metrics: [String: Double]
  public init(
    identity: AttemptIdentity, output: String, outputHash: String, modelDigest: String,
    ollamaVersion: String, metrics: [String: Double] = [:]
  ) {
    self.identity = identity
    self.output = output
    self.outputHash = outputHash
    self.modelDigest = modelDigest
    self.ollamaVersion = ollamaVersion
    self.metrics = metrics
  }
}
public struct FailureRequest: Codable, Sendable {
  public var identity: AttemptIdentity
  public var code: String
  public var message: String
  public var retryable: Bool
  public init(identity: AttemptIdentity, code: String, message: String, retryable: Bool) {
    self.identity = identity
    self.code = code
    self.message = message
    self.retryable = retryable
  }
}
public struct HeartbeatRequest: Codable, Sendable {
  public var identity: AttemptIdentity
  public init(identity: AttemptIdentity) { self.identity = identity }
}
public struct AttemptResponse: Codable, Sendable {
  public var state: String
  public var replayed: Bool
  public var leaseSeconds: Double?
  public init(state: String, replayed: Bool = false, leaseSeconds: Double? = nil) {
    self.state = state
    self.replayed = replayed
    self.leaseSeconds = leaseSeconds
  }
}
public struct BatchStatus: Codable, Sendable {
  public var batchId: String
  public var state: String
  public var counts: [String: Int]
  public var blockedReason: String?
  public init(batchId: String, state: String, counts: [String: Int], blockedReason: String? = nil) {
    self.batchId = batchId
    self.state = state
    self.counts = counts
    self.blockedReason = blockedReason
  }
}
public struct JobError: Codable, Sendable, Equatable {
  public var code: String
  public var message: String
  public init(code: String, message: String) {
    self.code = code
    self.message = message
  }
}
public struct ExportRecord: Codable, Sendable {
  public var id: String
  public var ordinal: Int
  public var state: String
  public var output: String?
  public var error: JobError?
  public var acceptedAttemptId: String?
  public var modelDigest: String
  public var configHash: String
  public var metrics: [String: Double]?
  public init(
    id: String, ordinal: Int, state: String, output: String?, error: JobError?,
    acceptedAttemptId: String?, modelDigest: String, configHash: String, metrics: [String: Double]?
  ) {
    self.id = id
    self.ordinal = ordinal
    self.state = state
    self.output = output
    self.error = error
    self.acceptedAttemptId = acceptedAttemptId
    self.modelDigest = modelDigest
    self.configHash = configHash
    self.metrics = metrics
  }
  private enum CodingKeys: String, CodingKey {
    case id, ordinal, state, output, error, acceptedAttemptId, modelDigest, configHash, metrics
  }
  public func encode(to encoder: any Encoder) throws {
    var c = encoder.container(keyedBy: CodingKeys.self)
    try c.encode(id, forKey: .id)
    try c.encode(ordinal, forKey: .ordinal)
    try c.encode(state, forKey: .state)
    try c.encode(output, forKey: .output)
    try c.encode(error, forKey: .error)
    try c.encode(acceptedAttemptId, forKey: .acceptedAttemptId)
    try c.encode(modelDigest, forKey: .modelDigest)
    try c.encode(configHash, forKey: .configHash)
    try c.encode(metrics, forKey: .metrics)
  }
}
public struct APIError: Error, Codable, Sendable, CustomStringConvertible {
  public var status: Int
  public var code: String
  public var message: String
  public init(status: Int, code: String, message: String) {
    self.status = status
    self.code = code
    self.message = message
  }
  public static func badRequest(_ code: String, _ message: String) -> Self {
    .init(status: 400, code: code, message: message)
  }
  public static func conflict(_ code: String, _ message: String) -> Self {
    .init(status: 409, code: code, message: message)
  }
  public static var unauthorized: Self {
    .init(status: 401, code: "unauthorized", message: "Missing or invalid credential")
  }
  public static var notFound: Self {
    .init(status: 404, code: "not_found", message: "Resource not found")
  }
  public var description: String { "\(code): \(message)" }
}
