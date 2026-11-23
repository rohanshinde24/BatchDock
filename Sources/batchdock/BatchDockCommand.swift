import ArgumentParser
import BatchDockCore
import BatchDockServer
import BatchDockWorker
import Darwin
import Foundation

@main
struct BatchDockCommand: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    commandName: "batchdock",
    abstract: "Run recoverable inference batches across participating Macs.",
    subcommands: [
      Initialize.self, Serve.self, Doctor.self, Worker.self, Submit.self, Status.self, Export.self,
      Cancel.self,
    ])
}

struct OwnerOptions: ParsableArguments {
  @Option(help: "Coordinator origin; remote access can use an SSH forward.") var coordinator =
    "http://127.0.0.1:8787"
  @Option(name: .customLong("state-dir"), help: "Private local state directory.") var stateDir =
    ".batchdock"
  @Option(
    name: .customLong("token-file"), help: "Owner token file; defaults to STATE_DIR/owner-token.")
  var tokenFile: String?
  func client() throws -> APIClient {
    guard let url = URL(string: coordinator) else {
      throw ValidationError("Invalid coordinator URL.")
    }
    return try APIClient(
      origin: url, token: LocalState.readToken(tokenFile ?? stateDir + "/owner-token"))
  }
}

struct Initialize: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    commandName: "init", abstract: "Create private local coordinator state and an owner credential."
  )
  @Option(name: .customLong("state-dir")) var stateDir = ".batchdock"
  mutating func run() async throws {
    let path = try ServerState.initialize(at: stateDir)
    print("Initialized coordinator state. Owner credential file: \(path)")
  }
}

struct Serve: AsyncParsableCommand {
  static let configuration = CommandConfiguration(abstract: "Run the coordinator on loopback.")
  @Option(name: .customLong("state-dir")) var stateDir = ".batchdock"
  @Option var port = 8787
  @Option(
    name: .customLong("lease-seconds"),
    help: "Ownership lease duration; use a short value only for local fault tests.")
  var leaseSeconds = 60.0
  mutating func run() async throws {
    guard (1024...65535).contains(port) else {
      throw ValidationError("Port must be between 1024 and 65535.")
    }
    guard leaseSeconds >= 1, leaseSeconds <= 600 else {
      throw ValidationError("Lease duration must be between 1 and 600 seconds.")
    }
    try await BatchDockServer.run(stateDirectory: stateDir, port: port, leaseSeconds: leaseSeconds)
  }
}

struct Doctor: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    abstract: "Inspect local Ollama models and optionally write a pinned manifest.")
  @Option(name: .customLong("ollama-url")) var ollamaUrl = "http://127.0.0.1:11434"
  @Option(help: "Exact installed model tag for a generated manifest.") var model: String?
  @Option(name: .customLong("write-manifest")) var writeManifest: String?
  @Option(help: "System instruction in a generated manifest.") var system = "Answer concisely."
  @Option(name: .customLong("max-output-tokens")) var maxOutputTokens = 256
  mutating func run() async throws {
    guard let origin = URL(string: ollamaUrl) else {
      throw ValidationError("Invalid Ollama origin.")
    }
    let snapshot = try await OllamaClient(origin: origin).snapshot()
    if let path = writeManifest {
      guard let model else {
        throw ValidationError("--write-manifest requires --model with an exact installed tag.")
      }
      guard let identity = snapshot.models.first(where: { $0.tag == model }) else {
        throw ValidationError("The requested exact model tag is not installed.")
      }
      let manifest = BatchManifest(
        model: identity,
        generation: GenerationConfig(
          system: system, options: InferenceOptions(numPredict: maxOutputTokens)))
      try manifest.validate()
      try LocalState.atomicWrite(CoreJSON.encoder().encode(manifest), to: path)
      print("Wrote manifest for \(identity.tag) to \(path).")
    } else {
      struct Report: Encodable {
        let ollamaVersion: String
        let models: [ModelIdentity]
      }
      try printJSON(Report(ollamaVersion: snapshot.version, models: snapshot.models))
    }
  }
}

struct Worker: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    abstract: "Enroll, revoke, or run a worker.", subcommands: [Add.self, Run.self, Revoke.self])

  struct Add: AsyncParsableCommand {
    static let configuration = CommandConfiguration(
      abstract: "Enroll one worker and save its credential to a private file.")
    @OptionGroup var owner: OwnerOptions
    @Option var name: String
    @Option(name: .customLong("write-token")) var writeToken: String
    mutating func run() async throws {
      guard !FileManager.default.fileExists(atPath: writeToken) else {
        throw ValidationError("The token file already exists; choose a new path.")
      }
      let client = try owner.client()
      guard
        let enrolled = try await client.request(
          "POST", "/v1/workers",
          body: CoreJSON.encoder().encode(WorkerEnrollmentRequest(name: name)),
          as: WorkerEnrollment.self)
      else {
        throw WorkerError("enrollment_failed", "Coordinator returned no enrollment.")
      }
      do { try LocalState.atomicWrite(Data((enrolled.token + "\n").utf8), to: writeToken) } catch {
        throw WorkerError(
          "credential_write_failed",
          "Worker \(enrolled.workerId) was enrolled, but its credential file could not be written. Revoke that worker before enrolling again."
        )
      }
      print("Enrolled worker \(enrolled.workerId). Credential saved to \(writeToken).")
    }
  }

  struct Revoke: AsyncParsableCommand {
    static let configuration = CommandConfiguration(
      abstract: "Revoke a worker credential and invalidate its active attempt.")
    @OptionGroup var owner: OwnerOptions
    @Argument var workerId: String
    mutating func run() async throws {
      try validateIdentifier(workerId)
      let client = try owner.client()
      _ = try await client.requestData("POST", "/v1/workers/\(workerId)/revoke")
      print("Worker revoked.")
    }
  }

  struct Run: AsyncParsableCommand {
    static let configuration = CommandConfiguration(
      abstract: "Process whole inference requests through this Mac's local Ollama.")
    @Option var coordinator = "http://127.0.0.1:8787"
    @Option(name: .customLong("ollama-url")) var ollamaUrl = "http://127.0.0.1:11434"
    @Option(name: .customLong("token-file")) var tokenFile: String
    @Option(name: .customLong("state-dir")) var stateDir = ".batchdock"
    @Flag(
      name: .customLong("stop-when-idle"),
      help: "Exit after a grace period with no compatible queued work.") var stopWhenIdle = false
    @Option(
      name: .customLong("idle-grace-seconds"),
      help: "Idle confirmation period; allow pending retry backoff to elapse.")
    var idleGraceSeconds = 10.0
    mutating func run() async throws {
      guard let coordinatorURL = URL(string: coordinator), let ollamaURL = URL(string: ollamaUrl)
      else { throw ValidationError("Invalid origin URL.") }
      let token = try LocalState.readToken(tokenFile)
      let control = WorkerControl()
      let signals = installSignals(control)
      defer { signals.forEach { $0.cancel() } }
      let worker = try WorkerRunner(
        coordinator: coordinatorURL, ollama: ollamaURL, token: token,
        stateDirectory: stateDir + "/workers/" + String(CoreJSON.hash(token).prefix(20)),
        control: control, idleGraceSeconds: idleGraceSeconds,
        log: { writeError($0) })
      try await worker.run(stopWhenIdle: stopWhenIdle)
    }
  }
}

struct Submit: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    abstract: "Submit a manifest and JSONL input with a durable request identity.")
  @OptionGroup var owner: OwnerOptions
  @Option var manifest: String
  @Option var input: String
  @Option(
    name: .customLong("request-id"),
    help:
      "Stable submission identifier; otherwise reuse the locally saved identity for identical input."
  ) var requestId: String?
  mutating func run() async throws {
    let request = try InputFiles.read(manifestPath: manifest, recordsPath: input)
    let payload = try CoreJSON.encoder().encode(request)
    let requestKey: String
    if let requestId {
      requestKey = requestId
    } else {
      try LocalState.ensureDirectory(owner.stateDir + "/submissions")
      let path =
        owner.stateDir + "/submissions/"
        + CoreJSON.hash(owner.coordinator + ":" + CoreJSON.hash(payload)) + ".request-id"
      requestKey = try savedRequestIdentity(path)
    }
    guard UUID(uuidString: requestKey) != nil else {
      throw ValidationError("Request ID must be a UUID.")
    }
    let client = try owner.client()
    writeError("Submission identity: \(requestKey)")
    guard
      let response = try await client.request(
        "POST", "/v1/batches", body: payload,
        headers: ["Idempotency-Key": requestKey], as: SubmitBatchResponse.self)
    else {
      throw WorkerError(
        "submission_unknown",
        "No submission result returned. Retry the same input and request identity.")
    }
    try printJSON(response)
  }
}

private func savedRequestIdentity(_ path: String) throws -> String {
  let lock = try ProcessLock(
    path: path + ".lock",
    conflictMessage: "Another submission is preparing the same input; retry after it finishes.")
  defer { withExtendedLifetime(lock) {} }
  if FileManager.default.fileExists(atPath: path) {
    return try String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(
      in: .whitespacesAndNewlines)
  }
  let key = UUID().uuidString
  try LocalState.atomicWrite(Data(key.utf8), to: path)
  return key
}

struct Status: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    abstract: "Show authoritative batch counts and blocked state.")
  @OptionGroup var owner: OwnerOptions
  @Argument var batchId: String
  mutating func run() async throws {
    try validateIdentifier(batchId)
    let client = try owner.client()
    if let value = try await client.request("GET", "/v1/batches/\(batchId)", as: BatchStatus.self) {
      try printJSON(value)
    }
  }
}

struct Cancel: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    abstract: "Cancel unfinished work while preserving already accepted results.")
  @OptionGroup var owner: OwnerOptions
  @Argument var batchId: String
  mutating func run() async throws {
    try validateIdentifier(batchId)
    let client = try owner.client()
    if let data = try await client.requestData("POST", "/v1/batches/\(batchId)/cancel") {
      FileHandle.standardOutput.write(data + Data("\n".utf8))
    }
  }
}

struct Export: AsyncParsableCommand {
  static let configuration = CommandConfiguration(
    abstract: "Write a complete ordered JSONL status/result snapshot atomically.")
  @OptionGroup var owner: OwnerOptions
  @Argument var batchId: String
  @Option(help: "Output file; omit to write JSONL to stdout.") var output: String?
  @Flag(name: .customLong("require-success")) var requireSuccess = false
  mutating func run() async throws {
    try validateIdentifier(batchId)
    let client = try owner.client()
    guard
      let data = try await client.requestData(
        "GET", "/v1/batches/\(batchId)/export", maximumBytes: 100 * 1024 * 1024)
    else { throw WorkerError("empty_export", "Coordinator returned no export.") }
    let rows = try InputFiles.decodeExport(data)
    var normalized = Data()
    for row in rows {
      normalized.append(try CoreJSON.encoder().encode(row))
      normalized.append(0x0a)
    }
    if let output {
      try LocalState.atomicWrite(normalized, to: output)
    } else {
      FileHandle.standardOutput.write(normalized)
    }
    if requireSuccess && rows.contains(where: { $0.state != "succeeded" }) { throw ExitCode(2) }
  }
}

func validateIdentifier(_ value: String) throws {
  guard UUID(uuidString: value) != nil else { throw ValidationError("Expected a UUID identifier.") }
}

func printJSON<T: Encodable>(_ value: T) throws {
  FileHandle.standardOutput.write(try CoreJSON.encoder().encode(value) + Data("\n".utf8))
}
func writeError(_ string: String) { FileHandle.standardError.write(Data((string + "\n").utf8)) }

func installSignals(_ control: WorkerControl) -> [DispatchSourceSignal] {
  [SIGINT, SIGTERM, SIGUSR1].map { number in
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
    source.setEventHandler {
      Task {
        if number == SIGUSR1 {
          await control.togglePause()
          writeError("Worker pause toggled.")
        } else {
          await control.stop()
        }
      }
    }
    source.resume()
    return source
  }
}
