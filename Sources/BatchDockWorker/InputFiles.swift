import BatchDockCore
import Foundation

public enum InputFiles {
  public static func read(manifestPath: String, recordsPath: String) throws -> SubmitBatchRequest {
    let manifestData = try boundedRead(manifestPath, maximum: 128 * 1024)
    guard let object = try JSONSerialization.jsonObject(with: manifestData) as? [String: Any],
      Set(object.keys) == ["protocol_version", "model", "generation", "attempt_limit_seconds"],
      let model = object["model"] as? [String: Any],
      Set(model.keys) == ["tag", "digest", "ollama_version"],
      let generation = object["generation"] as? [String: Any],
      Set(generation.keys) == ["system", "format", "options"],
      let options = generation["options"] as? [String: Any],
      Set(options.keys) == ["temperature", "seed", "num_ctx", "num_predict"]
    else {
      throw WorkerError("invalid_manifest", "Manifest contains missing or unsupported fields.")
    }
    let manifest = try CoreJSON.decoder().decode(BatchManifest.self, from: manifestData)
    let data = try boundedRead(recordsPath, maximum: 50 * 1024 * 1024)
    guard let text = String(data: data, encoding: .utf8) else {
      throw WorkerError("invalid_utf8", "Input JSONL must use UTF-8.")
    }
    var lines = text.components(separatedBy: "\n")
    if lines.last == "" { lines.removeLast() }
    var records: [InputRecord] = []
    for (offset, line) in lines.enumerated() {
      guard let bytes = line.data(using: .utf8),
        let record = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
        Set(record.keys) == ["id", "prompt"]
      else {
        throw WorkerError(
          "invalid_record", "JSONL line \(offset + 1) must contain exactly id and prompt.")
      }
      records.append(try CoreJSON.decoder().decode(InputRecord.self, from: bytes))
    }
    let request = SubmitBatchRequest(manifest: manifest, records: records)
    try request.validate()
    return request
  }

  public static func decodeExport(_ data: Data) throws -> [ExportRecord] {
    let rows: [ExportRecord]
    if let decoded = try? CoreJSON.decoder().decode([ExportRecord].self, from: data) {
      rows = decoded
    } else {
      guard let text = String(data: data, encoding: .utf8) else {
        throw WorkerError("invalid_export", "Export must use UTF-8.")
      }
      rows = try text.split(separator: "\n").map {
        try CoreJSON.decoder().decode(ExportRecord.self, from: Data($0.utf8))
      }
    }
    guard !rows.isEmpty else {
      throw WorkerError("invalid_export", "Export did not contain records.")
    }
    guard rows.enumerated().allSatisfy({ $0.offset == $0.element.ordinal }),
      Set(rows.map(\.id)).count == rows.count
    else {
      throw WorkerError(
        "invalid_export", "Export is not an ordered snapshot with unique input IDs.")
    }
    return rows
  }

  private static func boundedRead(_ path: String, maximum: Int) throws -> Data {
    let file = try FileHandle(forReadingFrom: URL(fileURLWithPath: path))
    defer { try? file.close() }
    var data = Data()
    while let chunk = try file.read(upToCount: min(65536, maximum + 1 - data.count)), !chunk.isEmpty
    {
      data.append(chunk)
      guard data.count <= maximum else {
        throw WorkerError("input_too_large", "Input file exceeds its configured size limit.")
      }
    }
    return data
  }
}
