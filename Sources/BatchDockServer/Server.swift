import BatchDockCore
import BatchDockStore
import Darwin
import Foundation
import Hummingbird

public enum ServerState {
  @discardableResult public static func initialize(at directory: String) throws -> String {
    let url = URL(fileURLWithPath: directory, isDirectory: true)
    try FileManager.default.createDirectory(
      at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    let tokenFile = url.appendingPathComponent("owner-token")
    if !FileManager.default.fileExists(atPath: tokenFile.path) {
      try Data((QueueStore.token() + "\n").utf8).write(to: tokenFile, options: .atomic)
    }
    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tokenFile.path)
    return tokenFile.path
  }
}

private final class CoordinatorLock: @unchecked Sendable {
  let descriptor: Int32
  init(path: String) throws {
    descriptor = Darwin.open(path, O_CREAT | O_RDWR, 0o600)
    guard descriptor >= 0 else {
      throw APIError(status: 500, code: "lock_failed", message: "Cannot open coordinator lock")
    }
    guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
      Darwin.close(descriptor)
      throw APIError.conflict(
        "coordinator_running", "A coordinator already owns this state directory")
    }
  }
  deinit {
    flock(descriptor, LOCK_UN)
    Darwin.close(descriptor)
  }
}

public enum BatchDockServer {
  public static func run(stateDirectory: String, port: Int, leaseSeconds: Double = 60) async throws
  {
    guard (1...65535).contains(port), (0.1...600).contains(leaseSeconds) else {
      throw APIError.badRequest("invalid_configuration", "Port or lease duration is out of bounds")
    }
    let tokenFile = try ServerState.initialize(at: stateDirectory)
    let ownerToken = try String(contentsOfFile: tokenFile, encoding: .utf8).trimmingCharacters(
      in: .whitespacesAndNewlines)
    guard ownerToken.count == 64 else {
      throw APIError.badRequest("invalid_owner_token", "Owner token file is invalid")
    }
    let lock = try CoordinatorLock(
      path: URL(fileURLWithPath: stateDirectory).appendingPathComponent("coordinator.lock").path)
    defer { withExtendedLifetime(lock) {} }
    let store = try QueueStore(
      path: URL(fileURLWithPath: stateDirectory).appendingPathComponent("queue.sqlite").path,
      leaseSeconds: leaseSeconds)
    let app = Application(
      router: router(store: store, ownerToken: ownerToken),
      configuration: .init(address: .hostname("127.0.0.1", port: port)))
    try await withThrowingTaskGroup(of: Void.self) { group in
      group.addTask { try await app.runService() }
      group.addTask {
        while !Task.isCancelled {
          try await Task.sleep(for: .seconds(min(1, leaseSeconds / 2)))
          try await store.expire()
        }
      }
      defer { group.cancelAll() }
      _ = try await group.next()
    }
  }

  public static func router(store: QueueStore, ownerToken: String) -> Router<BasicRequestContext> {
    let router = Router()
    let ownerHash = CoreJSON.hash(ownerToken)
    router.get("health") { _, _ in response(["status": "ok"]) }
    router.post("v1/batches") { request, _ in
      await safely {
        try requireOwner(request, hash: ownerHash)
        guard let key = request.headers[.init("Idempotency-Key")!] else {
          throw APIError.badRequest("missing_request_key", "Provide Idempotency-Key")
        }
        let body: SubmitBatchRequest = try await decode(
          request, limit: 50 * 1024 * 1024, strictBatch: true)
        let result = try await store.submit(body, key: key)
        return response(result, status: result.replayed ? .ok : .created)
      }
    }
    router.get("v1/batches/{id}") { request, context in
      await safely {
        try requireOwner(request, hash: ownerHash)
        return response(try await store.status(batchId: try context.parameters.require("id")))
      }
    }
    router.post("v1/batches/{id}/cancel") { request, context in
      await safely {
        try requireOwner(request, hash: ownerHash)
        return response(try await store.cancel(batchId: try context.parameters.require("id")))
      }
    }
    router.get("v1/batches/{id}/export") { request, context in
      await safely {
        try requireOwner(request, hash: ownerHash)
        let records = try await store.export(batchId: try context.parameters.require("id"))
        var data = Data()
        for row in records {
          data.append(try CoreJSON.encoder().encode(row))
          data.append(10)
        }
        return Response(
          status: .ok, headers: [.contentType: "application/x-ndjson"],
          body: .init(byteBuffer: ByteBuffer(bytes: data)))
      }
    }
    router.post("v1/workers") { request, _ in
      await safely {
        try requireOwner(request, hash: ownerHash)
        let body: WorkerEnrollmentRequest = try await decode(request)
        return response(try await store.enroll(name: body.name), status: .created)
      }
    }
    router.post("v1/workers/{id}/revoke") { request, context in
      await safely {
        try requireOwner(request, hash: ownerHash)
        try await store.revoke(workerId: try context.parameters.require("id"))
        return response(AttemptResponse(state: "revoked"))
      }
    }
    router.post("v1/claims") { request, _ in
      await safely {
        let token = try bearer(request)
        let body: ClaimRequest = try await decode(request, limit: 64 * 1024)
        guard let lease = try await store.claim(body, token: token) else {
          return Response(status: .noContent)
        }
        return response(lease)
      }
    }
    router.post("v1/attempts/{id}/heartbeat") { request, context in
      await safely {
        let token = try bearer(request)
        let body: HeartbeatRequest = try await decode(request)
        return response(
          try await store.heartbeat(
            attemptId: try context.parameters.require("id"), request: body, token: token))
      }
    }
    router.post("v1/attempts/{id}/complete") { request, context in
      await safely {
        let token = try bearer(request)
        let body: CompletionRequest = try await decode(request, limit: 2 * 1024 * 1024)
        return response(
          try await store.complete(
            attemptId: try context.parameters.require("id"), request: body, token: token))
      }
    }
    router.post("v1/attempts/{id}/fail") { request, context in
      await safely {
        let token = try bearer(request)
        let body: FailureRequest = try await decode(request)
        return response(
          try await store.fail(
            attemptId: try context.parameters.require("id"), request: body, token: token))
      }
    }
    return router
  }

  private static func bearer(_ request: Request) throws -> String {
    guard let value = request.headers[.authorization], value.hasPrefix("Bearer "),
      value.count <= 1024
    else { throw APIError.unauthorized }
    return String(value.dropFirst(7))
  }
  private static func requireOwner(_ request: Request, hash: String) throws {
    guard CoreJSON.hash(try bearer(request)) == hash else { throw APIError.unauthorized }
  }
  private static func decode<T: Decodable>(
    _ request: Request, limit: Int = 8192, strictBatch: Bool = false
  ) async throws -> T {
    let buffer = try await request.body.collect(upTo: limit)
    let data = Data(buffer.readableBytesView)
    do {
      if strictBatch { try validateBatchFields(data) }
      return try CoreJSON.decoder().decode(T.self, from: data)
    } catch let error as APIError { throw error } catch {
      throw APIError.badRequest("invalid_json", "Request body does not match the protocol")
    }
  }
  private static func validateBatchFields(_ data: Data) throws {
    guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
      throw APIError.badRequest("invalid_json", "Expected JSON object")
    }
    func allowed(_ value: Any?, _ keys: Set<String>) throws -> [String: Any] {
      guard let object = value as? [String: Any], Set(object.keys).isSubset(of: keys) else {
        throw APIError.badRequest("unsupported_field", "Request contains unsupported fields")
      }
      return object
    }
    _ = try allowed(root, ["manifest", "records"])
    let manifest = try allowed(
      root["manifest"], ["protocol_version", "model", "generation", "attempt_limit_seconds"])
    _ = try allowed(manifest["model"], ["tag", "digest", "ollama_version"])
    let generation = try allowed(manifest["generation"], ["system", "format", "options"])
    _ = try allowed(generation["options"], ["temperature", "seed", "num_ctx", "num_predict"])
    guard let records = root["records"] as? [Any] else {
      throw APIError.badRequest("invalid_records", "Expected records array")
    }
    for record in records { _ = try allowed(record, ["id", "prompt"]) }
  }
  private static func response<T: Encodable>(_ value: T, status: HTTPResponse.Status = .ok)
    -> Response
  {
    do {
      return Response(
        status: status, headers: [.contentType: "application/json"],
        body: .init(byteBuffer: ByteBuffer(bytes: try CoreJSON.encoder().encode(value))))
    } catch { return Response(status: .internalServerError) }
  }
  private static func safely(_ action: () async throws -> Response) async -> Response {
    do { return try await action() } catch let error as APIError {
      return response(error, status: .init(code: error.status))
    } catch let error as HTTPError {
      return response(
        APIError(
          status: error.status.code, code: "http_error", message: "Request cannot be processed"),
        status: error.status)
    } catch {
      return response(
        APIError(
          status: 503, code: "temporarily_unavailable",
          message: "Coordinator could not complete the operation; retry with the same request ID"),
        status: .serviceUnavailable)
    }
  }
}
