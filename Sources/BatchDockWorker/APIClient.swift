import BatchDockCore
import Foundation

public struct HTTPFailure: Error, LocalizedError, Sendable {
  public let status: Int
  public let code: String
  public var retryable: Bool { status == 429 || status >= 500 }
  public var errorDescription: String? { "Coordinator returned HTTP \(status) (\(code))." }
}

public struct APIClient: Sendable {
  public let origin: URL
  private let token: String?
  private let session: URLSession

  public init(origin: URL, token: String? = nil, requireLoopback: Bool = false) throws {
    guard let host = origin.host?.lowercased(), origin.user == nil, origin.password == nil,
      origin.query == nil, origin.fragment == nil, ["", "/"].contains(origin.path)
    else {
      throw WorkerError(
        "invalid_origin", "Use an origin URL without credentials, a path, or query parameters.")
    }
    let loopback = ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host)
    guard origin.scheme == "https" || (origin.scheme == "http" && loopback),
      !requireLoopback || loopback
    else {
      throw WorkerError(
        "insecure_origin",
        "HTTP is restricted to loopback; use an SSH forward for a remote coordinator. Ollama must be local."
      )
    }
    self.origin = origin
    self.token = token
    let configuration = URLSessionConfiguration.ephemeral
    configuration.httpShouldSetCookies = false
    configuration.httpMaximumConnectionsPerHost = 4
    self.session = URLSession(
      configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
  }

  public func request<Response: Decodable & Sendable>(
    _ method: String, _ path: String, body: Data? = nil,
    headers: [String: String] = [:], timeout: TimeInterval = 15,
    maximumBytes: Int = 1_048_576, as: Response.Type
  ) async throws -> Response? {
    let data = try await requestData(
      method, path, body: body, headers: headers, timeout: timeout, maximumBytes: maximumBytes)
    guard let data else { return nil }
    return try CoreJSON.decoder().decode(Response.self, from: data)
  }

  public func requestData(
    _ method: String, _ path: String, body: Data? = nil,
    headers: [String: String] = [:], timeout: TimeInterval = 15,
    maximumBytes: Int = 1_048_576
  ) async throws -> Data? {
    guard path.hasPrefix("/"), !path.hasPrefix("//"),
      var components = URLComponents(url: origin, resolvingAgainstBaseURL: false)
    else {
      throw WorkerError("invalid_path", "Invalid API path.")
    }
    components.path = path
    guard let url = components.url else { throw WorkerError("invalid_path", "Invalid API path.") }
    var request = URLRequest(url: url, timeoutInterval: timeout)
    request.httpMethod = method
    request.httpBody = body
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
    for (name, value) in headers { request.setValue(value, forHTTPHeaderField: name) }
    let (stream, response) = try await session.bytes(for: request)
    guard let response = response as? HTTPURLResponse else {
      throw WorkerError("invalid_response", "Server did not return an HTTP response.")
    }
    if response.statusCode == 204 { return nil }
    let bound = (200..<300).contains(response.statusCode) ? maximumBytes : min(maximumBytes, 8192)
    var data = Data()
    if response.expectedContentLength > Int64(bound) {
      throw WorkerError("oversized_response", "Server response exceeds the configured size limit.")
    }
    for try await byte in stream {
      guard data.count < bound else {
        throw WorkerError(
          "oversized_response", "Server response exceeds the configured size limit.")
      }
      data.append(byte)
    }
    guard (200..<300).contains(response.statusCode) else {
      let code =
        (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["code"] as? String
        ?? "http_error"
      throw HTTPFailure(status: response.statusCode, code: String(code.prefix(128)))
    }
    return data
  }
}

private final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}

public func retryDelay(_ attempt: Int) async throws {
  let base = min(pow(2.0, Double(attempt)), 8.0)
  try await Task.sleep(for: .milliseconds(Int((base + Double.random(in: 0...0.25)) * 1000)))
}
