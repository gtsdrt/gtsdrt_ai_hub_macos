import Foundation

// MARK: - 最小 JSON 值包装

/// 后端 `tool_calls_log[].arguments` 是任意 JSON 对象，
/// 这里用一个最小的 Codable 包装承载它，不引入第三方依赖。
enum AnyCodable: Codable, Hashable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case null
    case array([AnyCodable])
    case object([String: AnyCodable])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()

        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([AnyCodable].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: AnyCodable].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "不支持的 JSON 值"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()

        switch self {
        case .string(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    /// 转回 JSONSerialization 能接受的对象，用于格式化展示
    var jsonValue: Any {
        switch self {
        case .string(let value): return value
        case .int(let value): return value
        case .double(let value): return value
        case .bool(let value): return value
        case .null: return NSNull()
        case .array(let value): return value.map(\.jsonValue)
        case .object(let value): return value.mapValues(\.jsonValue)
        }
    }

    /// 标量值转字符串（对象 / 数组退回 JSON 文本）
    var stringValue: String {
        switch self {
        case .string(let value): return value
        case .int(let value): return String(value)
        case .double(let value): return String(value)
        case .bool(let value): return value ? "true" : "false"
        case .null: return ""
        case .array, .object:
            guard let data = try? JSONSerialization.data(
                withJSONObject: jsonValue,
                options: [.sortedKeys, .withoutEscapingSlashes]
            ) else { return "" }
            return String(data: data, encoding: .utf8) ?? ""
        }
    }
}

// MARK: - 工具调用记录

/// 与后端 `tool_calls_log` 里的一条记录对应
struct ToolCallLog: Codable, Identifiable, Hashable {
    let iteration: Int
    let toolName: String
    let arguments: [String: AnyCodable]
    let resultPreview: String
    /// "success" / "error"
    let resultStatus: String
    let durationMs: Int

    /// 后端不返回 id，解码 / 构造时生成一个，保证列表里能唯一标识
    let id: UUID
    /// 预计算：SwiftUI 每次重绘都会读这两个值，JSON 序列化不能放在 body 里（会卡）
    let argumentsJSON: String
    let resultDisplayText: String

    enum CodingKeys: String, CodingKey {
        case iteration
        case toolName = "tool_name"
        case arguments
        case resultPreview = "result_preview"
        case resultStatus = "result_status"
        case durationMs = "duration_ms"
    }

    init(
        iteration: Int,
        toolName: String,
        arguments: [String: AnyCodable] = [:],
        resultPreview: String,
        resultStatus: String,
        durationMs: Int
    ) {
        self.iteration = iteration
        self.toolName = toolName
        self.arguments = arguments
        self.resultPreview = resultPreview
        self.resultStatus = resultStatus
        self.durationMs = durationMs
        self.id = UUID()
        self.argumentsJSON = Self.makeArgumentsJSON(arguments)
        self.resultDisplayText = Self.makeResultDisplayText(resultPreview)
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        iteration = (try? container.decode(Int.self, forKey: .iteration)) ?? 0
        toolName = (try? container.decode(String.self, forKey: .toolName)) ?? ""
        arguments = (try? container.decode([String: AnyCodable].self, forKey: .arguments)) ?? [:]
        resultPreview = (try? container.decode(String.self, forKey: .resultPreview)) ?? ""
        resultStatus = (try? container.decode(String.self, forKey: .resultStatus)) ?? ""
        durationMs = (try? container.decode(Int.self, forKey: .durationMs)) ?? 0
        id = UUID()
        argumentsJSON = Self.makeArgumentsJSON(arguments)
        resultDisplayText = Self.makeResultDisplayText(resultPreview)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(iteration, forKey: .iteration)
        try container.encode(toolName, forKey: .toolName)
        try container.encode(arguments, forKey: .arguments)
        try container.encode(resultPreview, forKey: .resultPreview)
        try container.encode(resultStatus, forKey: .resultStatus)
        try container.encode(durationMs, forKey: .durationMs)
    }

    var isSuccess: Bool { resultStatus.lowercased() == "success" }

    /// 参数格式化后的 JSON 文本（多行缩进，键名排序）
    private static func makeArgumentsJSON(_ arguments: [String: AnyCodable]) -> String {
        let object = arguments.mapValues(\.jsonValue)
        guard !object.isEmpty,
              let data = try? JSONSerialization.data(
                withJSONObject: object,
                options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
              ),
              let text = String(data: data, encoding: .utf8)
        else {
            return "{}"
        }
        return text
    }

    /// 结果是完整 JSON 时格式化显示；被截断过的内容退回原文
    private static func makeResultDisplayText(_ preview: String) -> String {
        if let data = preview.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data),
           let pretty = try? JSONSerialization.data(
               withJSONObject: object,
               options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
           ),
           let text = String(data: pretty, encoding: .utf8) {
            return text
        }
        return preview.isEmpty ? L("（空结果）") : preview
    }
}
