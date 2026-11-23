import Darwin
import Foundation

public enum LocalState {
  public static func ensureDirectory(_ path: String) throws {
    try FileManager.default.createDirectory(
      atPath: path, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: path)
  }

  public static func readToken(_ path: String) throws -> String {
    let attributes = try FileManager.default.attributesOfItem(atPath: path)
    if let mode = attributes[.posixPermissions] as? NSNumber, mode.intValue & 0o077 != 0 {
      throw WorkerError(
        "credential_permissions",
        "Credential files must be private. Run chmod 600 on the credential file.")
    }
    let token = try String(contentsOfFile: path, encoding: .utf8).trimmingCharacters(
      in: .whitespacesAndNewlines)
    guard !token.isEmpty else {
      throw WorkerError("missing_credential", "Credential file is empty.")
    }
    return token
  }

  public static func atomicWrite(_ data: Data, to path: String) throws {
    let destination = URL(fileURLWithPath: path)
    let parent = destination.deletingLastPathComponent().path
    try FileManager.default.createDirectory(atPath: parent, withIntermediateDirectories: true)
    let temporary = destination.deletingLastPathComponent().appendingPathComponent(
      ".\(destination.lastPathComponent).\(UUID().uuidString).tmp"
    ).path
    let descriptor = Darwin.open(temporary, O_WRONLY | O_CREAT | O_EXCL, 0o600)
    guard descriptor >= 0 else {
      throw WorkerError("local_write", "Cannot create a private temporary file.")
    }
    var isOpen = true
    defer {
      if isOpen { Darwin.close(descriptor) }
      try? FileManager.default.removeItem(atPath: temporary)
    }
    try data.withUnsafeBytes { raw in
      guard let base = raw.baseAddress else { return }
      var offset = 0
      while offset < raw.count {
        let count = Darwin.write(descriptor, base.advanced(by: offset), raw.count - offset)
        if count < 0 && errno == EINTR { continue }
        guard count > 0 else { throw WorkerError("local_write", "Cannot persist the local file.") }
        offset += count
      }
    }
    guard fsync(descriptor) == 0 else {
      throw WorkerError("local_write", "Cannot synchronize the local file.")
    }
    guard Darwin.close(descriptor) == 0 else {
      isOpen = false
      throw WorkerError("local_write", "Cannot close the local file.")
    }
    isOpen = false
    guard Darwin.rename(temporary, path) == 0 else {
      throw WorkerError("local_write", "Cannot replace the local file.")
    }
    let directory = Darwin.open(parent, O_RDONLY)
    if directory >= 0 {
      _ = fsync(directory)
      Darwin.close(directory)
    }
  }
}

public final class ProcessLock: @unchecked Sendable {
  private let descriptor: Int32
  public init(
    path: String,
    conflictMessage: String =
      "This worker identity is already running with the same state directory."
  ) throws {
    let opened = Darwin.open(path, O_RDWR | O_CREAT, 0o600)
    guard opened >= 0 else { throw WorkerError("worker_lock", "Cannot open worker lock.") }
    guard flock(opened, LOCK_EX | LOCK_NB) == 0 else {
      Darwin.close(opened)
      throw WorkerError("local_state_locked", conflictMessage)
    }
    descriptor = opened
  }
  deinit {
    flock(descriptor, LOCK_UN)
    Darwin.close(descriptor)
  }
}

public struct WorkerError: Error, LocalizedError, Sendable {
  public let code: String
  public let message: String
  public init(_ code: String, _ message: String) {
    self.code = code
    self.message = message
  }
  public var errorDescription: String? { "\(code): \(message)" }
}
