import BatchDockCore
import Foundation
import GRDB

/// Serializes coordinator decisions while SQLite transactions make each decision durable.
public actor QueueStore {
  private let database: DatabaseQueue
  private let now: @Sendable () -> Double
  private let leaseDuration: Double
  private let epoch: Int
  private let exportByteLimit: Int
  private var deadlines: [String: (lease: Double, limit: Double)] = [:]
  private var retryReady: [String: Double] = [:]

  public init(
    path: String, leaseSeconds: Double = 60, exportByteLimit: Int = 64 * 1024 * 1024,
    now: (@Sendable () -> Double)? = nil
  ) throws {
    self.leaseDuration = leaseSeconds
    self.exportByteLimit = max(0, exportByteLimit)
    if let now {
      self.now = now
    } else {
      let origin = ContinuousClock.now
      self.now = {
        let c = origin.duration(to: .now).components
        return Double(c.seconds) + Double(c.attoseconds) / 1e18
      }
    }
    var configuration = Configuration()
    configuration.prepareDatabase { db in
      try db.execute(
        sql: "PRAGMA foreign_keys = ON; PRAGMA synchronous = FULL; PRAGMA busy_timeout = 5000")
    }
    self.database = try DatabaseQueue(path: path, configuration: configuration)
    try database.writeWithoutTransaction { db in try db.execute(sql: "PRAGMA journal_mode = WAL") }
    var migrator = DatabaseMigrator()
    migrator.registerMigration("queue-v1") { db in
      try db.execute(
        sql: """
          CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
          INSERT INTO metadata VALUES ('epoch', 0);
          CREATE TABLE batches (id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
            manifest TEXT NOT NULL, config_hash TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE workers (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE jobs (batch_id TEXT NOT NULL REFERENCES batches(id), id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            prompt TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','leased','succeeded','failed','cancelled')),
            generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0), attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            current_attempt_id TEXT REFERENCES attempts(id) DEFERRABLE INITIALLY DEFERRED,
            error_code TEXT, error_message TEXT, PRIMARY KEY(batch_id,id), UNIQUE(batch_id,ordinal));
          CREATE TABLE attempts (id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, job_id TEXT NOT NULL,
            worker_id TEXT NOT NULL REFERENCES workers(id), incarnation TEXT NOT NULL, epoch INTEGER NOT NULL,
            generation INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('leased','succeeded','failed','expired','cancelled')),
            FOREIGN KEY(batch_id,job_id) REFERENCES jobs(batch_id,id), UNIQUE(batch_id,job_id,generation));
          CREATE UNIQUE INDEX one_worker_slot ON attempts(worker_id) WHERE state='leased';
          CREATE TABLE claims (worker_id TEXT NOT NULL REFERENCES workers(id), incarnation TEXT NOT NULL, claim_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, attempt_id TEXT NOT NULL REFERENCES attempts(id), PRIMARY KEY(worker_id,incarnation,claim_id));
          CREATE TABLE results (batch_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id),
            output TEXT NOT NULL, output_hash TEXT NOT NULL, completion_hash TEXT NOT NULL, metrics TEXT NOT NULL,
            FOREIGN KEY(batch_id,job_id) REFERENCES jobs(batch_id,id), PRIMARY KEY(batch_id,job_id));
          """)
    }
    migrator.registerMigration("failure-receipts-v1") { db in
      try db.execute(
        sql:
          "CREATE TABLE failure_receipts(attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), request_hash TEXT NOT NULL, response_state TEXT NOT NULL)"
      )
    }
    try migrator.migrate(database)
    self.epoch = try database.write { db in
      try db.execute(sql: "UPDATE metadata SET value=value+1 WHERE key='epoch'")
      try db.execute(sql: "UPDATE attempts SET state='expired' WHERE state='leased'")
      try db.execute(
        sql:
          "UPDATE jobs SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END, current_attempt_id=NULL,error_code='coordinator_restarted',error_message='Coordinator restarted during inference' WHERE state='leased'"
      )
      return try Int.fetchOne(db, sql: "SELECT value FROM metadata WHERE key='epoch'")!
    }
  }

  public func enroll(name: String) throws -> WorkerEnrollment {
    guard !name.isEmpty, name.utf8.count <= 200 else {
      throw APIError.badRequest("invalid_name", "Worker name must contain 1 to 200 bytes")
    }
    let worker = WorkerEnrollment(workerId: UUID().uuidString, token: Self.token())
    try database.write { db in
      try db.execute(
        sql: "INSERT INTO workers(id,name,token_hash) VALUES(?,?,?)",
        arguments: [worker.workerId, name, CoreJSON.hash(worker.token)])
    }
    return worker
  }

  public func submit(_ request: SubmitBatchRequest, key: String) throws -> SubmitBatchResponse {
    guard UUID(uuidString: key) != nil else {
      throw APIError.badRequest("invalid_request_key", "Idempotency-Key must be a UUID")
    }
    try request.validate()
    let requestHash = try CoreJSON.digest(request)
    let manifest = try json(request.manifest)
    let configHash = try CoreJSON.digest(request.manifest)
    return try database.write { db in
      if let row = try Row.fetchOne(
        db, sql: "SELECT id,request_hash FROM batches WHERE request_key=?", arguments: [key])
      {
        guard row["request_hash"] as String == requestHash else {
          throw APIError.conflict(
            "idempotency_conflict", "Request key already identifies different input")
        }
        return .init(batchId: row["id"], replayed: true)
      }
      let id = UUID().uuidString
      try db.execute(
        sql:
          "INSERT INTO batches(id,request_key,request_hash,manifest,config_hash) VALUES(?,?,?,?,?)",
        arguments: [id, key, requestHash, manifest, configHash])
      for (ordinal, record) in request.records.enumerated() {
        try db.execute(
          sql: "INSERT INTO jobs(batch_id,id,ordinal,prompt,state) VALUES(?,?,?,?,'queued')",
          arguments: [id, record.id, ordinal, record.prompt])
      }
      return .init(batchId: id, replayed: false)
    }
  }

  public func claim(_ request: ClaimRequest, token: String) throws -> Lease? {
    guard UUID(uuidString: request.claimId) != nil, UUID(uuidString: request.incarnation) != nil,
      request.models.count <= 100
    else { throw APIError.badRequest("invalid_claim", "Claim and incarnation IDs must be UUIDs") }
    try expire()
    let hash = try CoreJSON.digest(request)
    let decisionTime = now()
    let claim: Lease? = try database.write { db in
      let workerId = try authenticate(token: token, db: db)
      if let row = try Row.fetchOne(
        db,
        sql:
          "SELECT request_hash,attempt_id FROM claims WHERE worker_id=? AND incarnation=? AND claim_id=?",
        arguments: [workerId, request.incarnation, request.claimId])
      {
        guard row["request_hash"] as String == hash else {
          throw APIError.conflict(
            "idempotency_conflict", "Claim key already identifies another request")
        }
        let attemptId: String = row["attempt_id"]
        guard var lease = try makeLease(attemptId: attemptId, db: db),
          let deadline = deadlines[attemptId]
        else {
          throw APIError.conflict(
            "stale_attempt", "Previous claim is no longer active; use a new claim ID")
        }
        lease.leaseSeconds = max(0, min(deadline.lease, deadline.limit) - decisionTime)
        return lease
      }
      let active = try Int.fetchOne(
        db, sql: "SELECT COUNT(*) FROM attempts WHERE worker_id=? AND state='leased'",
        arguments: [workerId])!
      guard active == 0 else {
        throw APIError.conflict("worker_busy", "Worker already owns an active attempt")
      }
      let rows = try Row.fetchAll(
        db,
        sql:
          "SELECT j.*,b.manifest,b.config_hash FROM jobs j JOIN batches b ON b.id=j.batch_id WHERE j.state='queued' AND j.attempts<3 AND b.cancelled=0 ORDER BY b.rowid,j.ordinal"
      )
      for row in rows {
        let manifest: BatchManifest = try decode(row["manifest"])
        let batchId: String = row["batch_id"]
        let jobId: String = row["id"]
        guard request.models.contains(manifest.model),
          (retryReady[Self.jobKey(batchId, jobId)] ?? 0) <= decisionTime
        else { continue }
        let generation: Int = (row["generation"] as Int) + 1
        let attemptId = UUID().uuidString
        try db.execute(
          sql:
            "INSERT INTO attempts(id,batch_id,job_id,worker_id,incarnation,epoch,generation,state) VALUES(?,?,?,?,?,?,?,'leased')",
          arguments: [attemptId, batchId, jobId, workerId, request.incarnation, epoch, generation])
        try db.execute(
          sql:
            "UPDATE jobs SET state='leased',generation=?,attempts=attempts+1,current_attempt_id=?,error_code=NULL,error_message=NULL WHERE batch_id=? AND id=? AND state='queued'",
          arguments: [generation, attemptId, batchId, jobId])
        guard db.changesCount == 1 else {
          throw APIError.conflict("claim_conflict", "Job ownership changed")
        }
        try db.execute(
          sql: "INSERT INTO claims VALUES(?,?,?,?,?)",
          arguments: [workerId, request.incarnation, request.claimId, hash, attemptId])
        return try makeLease(attemptId: attemptId, db: db)
      }
      return nil
    }
    if let claim, deadlines[claim.attemptId] == nil {
      deadlines[claim.attemptId] = (
        decisionTime + min(leaseDuration, Double(claim.attemptLimitSeconds)),
        decisionTime + Double(claim.attemptLimitSeconds)
      )
    }
    return claim
  }

  public func heartbeat(attemptId: String, request: HeartbeatRequest, token: String) throws
    -> AttemptResponse
  {
    try expire()
    let current = now()
    try database.read { db in
      _ = try requireAttempt(attemptId, identity: request.identity, token: token, db: db)
    }
    guard let old = deadlines[attemptId], current < old.limit else {
      throw APIError.conflict("stale_attempt", "Attempt expired")
    }
    let renewed = min(current + leaseDuration, old.limit)
    deadlines[attemptId] = (renewed, old.limit)
    return .init(state: "leased", leaseSeconds: renewed - current)
  }

  public func complete(attemptId: String, request: CompletionRequest, token: String) throws
    -> AttemptResponse
  {
    guard request.output.utf8.count <= 256 * 1024,
      request.outputHash == CoreJSON.hash(request.output), request.metrics.count <= 32,
      request.metrics.values.allSatisfy({ $0.isFinite && $0 >= 0 })
    else {
      throw APIError.badRequest("invalid_output", "Output hash, size, or metrics are invalid")
    }
    try expire()
    let completionHash = try CoreJSON.digest(request)
    let response = try database.write { db -> AttemptResponse in
      let workerId = try authenticate(token: token, db: db)
      if let result = try Row.fetchOne(
        db,
        sql:
          "SELECT r.*,a.worker_id,a.incarnation,a.epoch,a.generation FROM results r JOIN attempts a ON a.id=r.attempt_id WHERE r.attempt_id=?",
        arguments: [attemptId])
      {
        guard result["worker_id"] as String == workerId,
          result["incarnation"] as String == request.identity.workerIncarnation,
          result["epoch"] as Int == request.identity.epoch,
          result["generation"] as Int == request.identity.generation,
          result["batch_id"] as String == request.identity.batchId,
          result["job_id"] as String == request.identity.jobId,
          result["completion_hash"] as String == completionHash
        else {
          throw APIError.conflict("completion_conflict", "Completion differs from accepted result")
        }
        return .init(state: "succeeded", replayed: true)
      }
      let attempt = try requireAttempt(attemptId, identity: request.identity, token: token, db: db)
      let manifest: BatchManifest = try decode(attempt["manifest"])
      guard manifest.model.digest == request.modelDigest,
        manifest.model.ollamaVersion == request.ollamaVersion
      else { throw APIError.conflict("model_mismatch", "Result model does not match batch") }
      try db.execute(
        sql:
          "UPDATE jobs SET state='succeeded' WHERE batch_id=? AND id=? AND state='leased' AND current_attempt_id=? AND generation=?",
        arguments: [
          request.identity.batchId, request.identity.jobId, attemptId, request.identity.generation,
        ])
      guard db.changesCount == 1 else {
        throw APIError.conflict("stale_attempt", "Attempt ownership changed")
      }
      try db.execute(
        sql: "INSERT INTO results VALUES(?,?,?,?,?,?,?)",
        arguments: [
          request.identity.batchId, request.identity.jobId, attemptId, request.output,
          request.outputHash, completionHash, try json(request.metrics),
        ])
      try db.execute(
        sql: "UPDATE attempts SET state='succeeded' WHERE id=?", arguments: [attemptId])
      return .init(state: "succeeded")
    }
    deadlines.removeValue(forKey: attemptId)
    return response
  }

  public func fail(attemptId: String, request: FailureRequest, token: String) throws
    -> AttemptResponse
  {
    guard !request.code.isEmpty, request.code.utf8.count <= 128, request.message.utf8.count <= 1024
    else { throw APIError.badRequest("invalid_error", "Error exceeds limits") }
    try expire()
    let requestHash = try CoreJSON.digest(request)
    let response = try database.write { db -> AttemptResponse in
      let workerId = try authenticate(token: token, db: db)
      if let receipt = try Row.fetchOne(
        db,
        sql:
          "SELECT f.*,a.worker_id FROM failure_receipts f JOIN attempts a ON a.id=f.attempt_id WHERE f.attempt_id=?",
        arguments: [attemptId])
      {
        guard receipt["worker_id"] as String == workerId,
          receipt["request_hash"] as String == requestHash
        else { throw APIError.conflict("failure_conflict", "Failure differs from accepted report") }
        return .init(state: receipt["response_state"], replayed: true)
      }
      let attempt = try requireAttempt(attemptId, identity: request.identity, token: token, db: db)
      let attempts: Int = attempt["attempts"]
      let state = request.retryable && attempts < 3 ? "queued" : "failed"
      try db.execute(
        sql:
          "UPDATE jobs SET state=?,current_attempt_id=NULL,error_code=?,error_message=? WHERE batch_id=? AND id=?",
        arguments: [
          state, request.code, request.message, request.identity.batchId, request.identity.jobId,
        ])
      try db.execute(sql: "UPDATE attempts SET state='failed' WHERE id=?", arguments: [attemptId])
      try db.execute(
        sql: "INSERT INTO failure_receipts VALUES(?,?,?)",
        arguments: [attemptId, requestHash, state])
      return .init(state: state)
    }
    deadlines.removeValue(forKey: attemptId)
    if response.state == "queued", !response.replayed {
      retryReady[Self.jobKey(request.identity.batchId, request.identity.jobId)] =
        now() + Double.random(in: 0.25...1)
    }
    return response
  }

  public func cancel(batchId: String) throws -> BatchStatus {
    try expire()
    let attemptIds = try database.write { db -> [String] in
      guard
        try Bool.fetchOne(
          db, sql: "SELECT EXISTS(SELECT 1 FROM batches WHERE id=?)", arguments: [batchId]) == true
      else { throw APIError.notFound }
      let ids = try String.fetchAll(
        db, sql: "SELECT id FROM attempts WHERE batch_id=? AND state='leased'", arguments: [batchId]
      )
      try db.execute(sql: "UPDATE batches SET cancelled=1 WHERE id=?", arguments: [batchId])
      try db.execute(
        sql: "UPDATE attempts SET state='cancelled' WHERE batch_id=? AND state='leased'",
        arguments: [batchId])
      try db.execute(
        sql:
          "UPDATE jobs SET state='cancelled',current_attempt_id=NULL,error_code='cancelled',error_message='Cancelled by owner' WHERE batch_id=? AND state IN ('queued','leased')",
        arguments: [batchId])
      return ids
    }
    for id in attemptIds { deadlines.removeValue(forKey: id) }
    return try status(batchId: batchId)
  }

  public func revoke(workerId: String) throws {
    let ids = try database.write { db -> [String] in
      try db.execute(sql: "UPDATE workers SET enabled=0 WHERE id=?", arguments: [workerId])
      guard db.changesCount == 1 else { throw APIError.notFound }
      let ids = try String.fetchAll(
        db, sql: "SELECT id FROM attempts WHERE worker_id=? AND state='leased'",
        arguments: [workerId])
      for id in ids { try reclaim(id, code: "worker_revoked", db: db) }
      return ids
    }
    for id in ids { deadlines.removeValue(forKey: id) }
  }

  public func status(batchId: String) throws -> BatchStatus {
    try expire()
    return try database.read { db in
      guard
        let cancelled = try Bool.fetchOne(
          db, sql: "SELECT cancelled FROM batches WHERE id=?", arguments: [batchId])
      else { throw APIError.notFound }
      var counts: [String: Int] = [:]
      for row in try Row.fetchAll(
        db, sql: "SELECT state,COUNT(*) AS count FROM jobs WHERE batch_id=? GROUP BY state",
        arguments: [batchId])
      { counts[row["state"]] = row["count"] }
      let pending = (counts["queued"] ?? 0) + (counts["leased"] ?? 0)
      let state =
        cancelled
        ? "cancelled"
        : pending > 0
          ? "running" : (counts["failed"] ?? 0) > 0 ? "completed_with_errors" : "succeeded"
      return .init(
        batchId: batchId, state: state, counts: counts,
        blockedReason: pending > 0 && (counts["leased"] ?? 0) == 0
          ? "Waiting for an available worker with matching model digest and Ollama version" : nil)
    }
  }

  public func export(batchId: String) throws -> [ExportRecord] {
    try expire()
    return try database.read { db in
      guard
        let batch = try Row.fetchOne(
          db, sql: "SELECT * FROM batches WHERE id=?", arguments: [batchId])
      else { throw APIError.notFound }
      let manifest: BatchManifest = try decode(batch["manifest"])
      // Six bytes cover the worst JSON escape expansion of each UTF-8 byte.
      // Fixed per-record overhead covers field names, UUIDs, hashes, counters and punctuation.
      let upperBound =
        try Int.fetchOne(
          db,
          sql: """
            SELECT COALESCE(SUM(1024+6*(length(CAST(j.id AS BLOB))+
              COALESCE(length(CAST(r.output AS BLOB)),0)+COALESCE(length(CAST(j.error_code AS BLOB)),0)+
              COALESCE(length(CAST(j.error_message AS BLOB)),0))+COALESCE(length(CAST(r.metrics AS BLOB)),0)),0)
            FROM jobs j LEFT JOIN results r ON r.batch_id=j.batch_id AND r.job_id=j.id WHERE j.batch_id=?
            """, arguments: [batchId]) ?? 0
      guard upperBound <= exportByteLimit else {
        throw APIError(
          status: 413, code: "export_too_large",
          message:
            "Buffered export exceeds its conservative size limit; split input into smaller batches")
      }
      return try Row.fetchAll(
        db,
        sql:
          "SELECT j.id,j.ordinal,j.state,j.error_code,j.error_message,r.output,r.metrics,r.attempt_id AS accepted_attempt_id FROM jobs j LEFT JOIN results r ON r.batch_id=j.batch_id AND r.job_id=j.id WHERE j.batch_id=? ORDER BY j.ordinal",
        arguments: [batchId]
      ).map { row in
        let code: String? = row["error_code"]
        let metrics: String? = row["metrics"]
        return ExportRecord(
          id: row["id"], ordinal: row["ordinal"], state: row["state"], output: row["output"],
          error: code.map { JobError(code: $0, message: row["error_message"] ?? "") },
          acceptedAttemptId: row["accepted_attempt_id"], modelDigest: manifest.model.digest,
          configHash: batch["config_hash"], metrics: try metrics.map { try decode($0) })
      }
    }
  }

  public func expire() throws {
    let time = now()
    let expired = deadlines.filter { time >= $0.value.lease || time >= $0.value.limit }.map(\.key)
    guard !expired.isEmpty else { return }
    try database.write { db in for id in expired { try reclaim(id, code: "lease_expired", db: db) }
    }
    for id in expired { deadlines.removeValue(forKey: id) }
  }

  private func requireAttempt(_ id: String, identity: AttemptIdentity, token: String, db: Database)
    throws -> Row
  {
    let workerId = try authenticate(token: token, db: db)
    guard
      let row = try Row.fetchOne(
        db,
        sql:
          "SELECT a.*,j.attempts,b.manifest FROM attempts a JOIN jobs j ON j.batch_id=a.batch_id AND j.id=a.job_id JOIN batches b ON b.id=a.batch_id WHERE a.id=? AND a.state='leased' AND j.state='leased' AND j.current_attempt_id=a.id AND b.cancelled=0",
        arguments: [id]),
      row["worker_id"] as String == workerId,
      row["incarnation"] as String == identity.workerIncarnation,
      row["epoch"] as Int == epoch, identity.epoch == epoch,
      row["generation"] as Int == identity.generation,
      row["batch_id"] as String == identity.batchId, row["job_id"] as String == identity.jobId,
      let deadline = deadlines[id], now() < deadline.lease, now() < deadline.limit
    else {
      throw APIError.conflict("stale_attempt", "Attempt is expired, cancelled, or no longer owned")
    }
    return row
  }

  private func authenticate(token: String, db: Database) throws -> String {
    guard
      let id = try String.fetchOne(
        db, sql: "SELECT id FROM workers WHERE token_hash=? AND enabled=1",
        arguments: [CoreJSON.hash(token)])
    else { throw APIError.unauthorized }
    return id
  }
  private func makeLease(attemptId: String, db: Database) throws -> Lease? {
    guard
      let row = try Row.fetchOne(
        db,
        sql:
          "SELECT a.*,j.prompt,b.manifest,b.config_hash FROM attempts a JOIN jobs j ON j.batch_id=a.batch_id AND j.id=a.job_id JOIN batches b ON b.id=a.batch_id WHERE a.id=? AND a.state='leased'",
        arguments: [attemptId])
    else { return nil }
    let manifest: BatchManifest = try decode(row["manifest"])
    let batchId: String = row["batch_id"]
    let jobId: String = row["job_id"]
    let attemptEpoch: Int = row["epoch"]
    let generation: Int = row["generation"]
    let incarnation: String = row["incarnation"]
    let configHash: String = row["config_hash"]
    let prompt: String = row["prompt"]
    return Lease(
      attemptId: attemptId, batchId: batchId, jobId: jobId, epoch: attemptEpoch,
      generation: generation, workerIncarnation: incarnation,
      leaseSeconds: min(leaseDuration, Double(manifest.attemptLimitSeconds)),
      attemptLimitSeconds: manifest.attemptLimitSeconds, configHash: configHash, manifest: manifest,
      prompt: prompt)
  }
  private func reclaim(_ attemptId: String, code: String, db: Database) throws {
    guard
      let row = try Row.fetchOne(
        db, sql: "SELECT batch_id,job_id FROM attempts WHERE id=? AND state='leased'",
        arguments: [attemptId])
    else { return }
    let batch: String = row["batch_id"]
    let job: String = row["job_id"]
    try db.execute(sql: "UPDATE attempts SET state='expired' WHERE id=?", arguments: [attemptId])
    try db.execute(
      sql:
        "UPDATE jobs SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END,current_attempt_id=NULL,error_code=?,error_message='Worker ownership ended before a result was accepted' WHERE batch_id=? AND id=? AND current_attempt_id=? AND state='leased'",
      arguments: [code, batch, job, attemptId])
  }
  private func json<T: Encodable>(_ value: T) throws -> String {
    String(decoding: try CoreJSON.encoder().encode(value), as: UTF8.self)
  }
  private func decode<T: Decodable>(_ text: String) throws -> T {
    try CoreJSON.decoder().decode(T.self, from: Data(text.utf8))
  }
  private static func jobKey(_ batch: String, _ job: String) -> String {
    "\(batch.utf8.count):\(batch)\(job)"
  }
  public static func token() -> String {
    var generator = SystemRandomNumberGenerator()
    return (0..<32).map { _ in UInt8.random(in: 0...255, using: &generator) }.map {
      String(format: "%02x", $0)
    }.joined()
  }
}
