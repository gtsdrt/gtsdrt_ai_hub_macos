import Foundation

// MARK: - 登录

struct LoginResponse: Decodable {
    let token: String
}

struct GoogleAuthStartResponse: Decodable {
    let authorizationURL: String
    let state: String
    let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case authorizationURL = "authorization_url"
        case state
        case expiresIn = "expires_in"
    }
}

struct GoogleAuthStatusResponse: Decodable {
    let status: String
    let token: String?
    let email: String?
    let error: String?
}

struct GitHubAuthStartResponse: Decodable {
    let state: String
    let userCode: String
    let verificationURI: String
    let expiresIn: Int
    let interval: Int

    enum CodingKeys: String, CodingKey {
        case state
        case userCode = "user_code"
        case verificationURI = "verification_uri"
        case expiresIn = "expires_in"
        case interval
    }
}

struct GitHubAuthStatusResponse: Decodable {
    let status: String
    let token: String?
    let login: String?
    let email: String?
    let error: String?
}

struct APIErrorBody: Decodable {
    let error: String?
}

// MARK: - 对话请求 / 异步任务

struct ChatRequestPayload: Encodable {
    var prompt: String
    var conversationID: String?
    var aiProvider: String?
    var systemPrompt: String?
    var webhookURL: String?
    /// 是否挂载运维工具（Azure / Meraki）；后端字段 enable_tools
    var enableTools: Bool = false

    enum CodingKeys: String, CodingKey {
        case prompt
        case conversationID = "conversation_id"
        case aiProvider = "ai_provider"
        case systemPrompt = "system_prompt"
        case webhookURL = "webhook_url"
        case enableTools = "enable_tools"
    }
}

struct ChatAsyncResponse: Decodable {
    let instanceID: String
    let status: String
    let conversationID: String?

    enum CodingKeys: String, CodingKey {
        case instanceID = "instance_id"
        case status
        case conversationID = "conversation_id"
    }
}

struct TaskResult: Decodable {
    let status: String?
    let response: String?
    let conversationID: String?
    let toolCallsLog: [ToolCallLog]?
    let iterationsUsed: Int?
    /// 后端是否真的挂载了工具（异步结果里可能没有这个字段）
    let toolsEnabled: Bool?

    enum CodingKeys: String, CodingKey {
        case status
        case response
        case conversationID = "conversation_id"
        case toolCallsLog = "tool_calls_log"
        case iterationsUsed = "iterations_used"
        case toolsEnabled = "tools_enabled"
    }
}

/// /api/chat 同步响应
struct ChatResponse: Decodable {
    let status: String?
    let response: String?
    let conversationID: String?
    let provider: String?
    let model: String?
    let mock: Bool?
    let toolsEnabled: Bool?
    let toolCallsLog: [ToolCallLog]?
    let iterationsUsed: Int?

    enum CodingKeys: String, CodingKey {
        case status
        case response
        case conversationID = "conversation_id"
        case provider
        case model
        case mock
        case toolsEnabled = "tools_enabled"
        case toolCallsLog = "tool_calls_log"
        case iterationsUsed = "iterations_used"
    }

    /// 同步响应 → 一条 assistant 消息（带上工具调用记录）
    /// - Parameter toolsRequested: 本次请求是否开了工具开关（客户端才知道）
    func assistantMessage(toolsRequested: Bool? = nil) -> ChatMessage {
        ChatMessage(
            role: "assistant",
            content: response ?? "",
            toolCallsLog: toolCallsLog?.isEmpty == false ? toolCallsLog : nil,
            iterationsUsed: iterationsUsed,
            toolsEnabled: toolsEnabled,
            toolsRequested: toolsRequested
        )
    }
}

struct TaskStatusResponse: Decodable {
    let instanceID: String?
    let status: String
    let message: String?
    let error: String?
    let result: TaskResult?

    enum CodingKeys: String, CodingKey {
        case instanceID = "instance_id"
        case status
        case message
        case error
        case result
    }
}

// MARK: - 会话

struct ChatMessage: Identifiable, Codable, Hashable {
    let role: String
    let content: String
    /// 工具调用记录；旧历史数据里没有该字段时为 nil
    var toolCallsLog: [ToolCallLog]?
    /// 实际发生的模型调用轮数
    var iterationsUsed: Int?
    /// 后端回报的工具开关状态；历史数据里没有这个字段时为 nil
    var toolsEnabled: Bool?
    /// 客户端发这条消息时工具开关是否开着（仅内存里有）
    var toolsRequested: Bool?
    var id = UUID()

    enum CodingKeys: String, CodingKey {
        case role
        case content
        case toolCallsLog = "tool_calls_log"
        case iterationsUsed = "iterations_used"
        case toolsEnabled = "tools_enabled"
        case toolsRequested = "tools_requested"
    }

    init(
        role: String,
        content: String,
        toolCallsLog: [ToolCallLog]? = nil,
        iterationsUsed: Int? = nil,
        toolsEnabled: Bool? = nil,
        toolsRequested: Bool? = nil
    ) {
        self.role = role
        self.content = content
        self.toolCallsLog = toolCallsLog
        self.iterationsUsed = iterationsUsed
        self.toolsEnabled = toolsEnabled
        self.toolsRequested = toolsRequested
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        role = (try? container.decode(String.self, forKey: .role)) ?? "assistant"
        content = (try? container.decode(String.self, forKey: .content)) ?? ""
        toolCallsLog = try? container.decode([ToolCallLog].self, forKey: .toolCallsLog)
        iterationsUsed = try? container.decode(Int.self, forKey: .iterationsUsed)
        toolsEnabled = try? container.decode(Bool.self, forKey: .toolsEnabled)
        toolsRequested = try? container.decode(Bool.self, forKey: .toolsRequested)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(role, forKey: .role)
        try container.encode(content, forKey: .content)
        try container.encodeIfPresent(toolCallsLog, forKey: .toolCallsLog)
        try container.encodeIfPresent(iterationsUsed, forKey: .iterationsUsed)
        try container.encodeIfPresent(toolsEnabled, forKey: .toolsEnabled)
        try container.encodeIfPresent(toolsRequested, forKey: .toolsRequested)
    }
}

struct ConversationSummary: Identifiable, Decodable, Hashable {
    let conversationID: String
    let title: String
    let updatedAt: String?
    let messageCount: Int?

    var id: String { conversationID }

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case title
        case updatedAt = "updated_at"
        case messageCount = "message_count"
    }
}

struct ConversationListResponse: Decodable {
    let conversations: [ConversationSummary]
}

struct HistoryResponse: Decodable {
    let conversationID: String?
    let messageCount: Int?
    let messages: [ChatMessage]

    enum CodingKeys: String, CodingKey {
        case conversationID = "conversation_id"
        case messageCount = "message_count"
        case messages
    }
}

// MARK: - 健康检查（判断后端是否在跑、是不是 mock 模式）

struct HealthProvider: Decodable {
    let label: String?
    let configured: Bool?
    let model: String?
}

struct HealthAI: Decodable {
    let mockMode: String?
    let defaultProvider: String?
    let providers: [String: HealthProvider]?

    enum CodingKeys: String, CodingKey {
        case mockMode = "mock_mode"
        case defaultProvider = "default_provider"
        case providers
    }
}

struct HealthResponse: Decodable {
    let status: String
    let storage: StorageValue?
    let storageDetail: StorageDetail?
    let ai: HealthAI?
    let azure: ToolGroupStatus?
    let meraki: ToolGroupStatus?
    let nexusDashboard: ToolGroupStatus?
    let container: ToolGroupStatus?
    /// 以下三个字段是较新的 /api/health 才有的，旧后端没有
    let deepseek: ToolGroupStatus?
    let kimi: ToolGroupStatus?
    let python: PythonInfo?

    enum CodingKeys: String, CodingKey {
        case status
        case storage
        case storageDetail = "storage_detail"
        case ai
        case azure
        case meraki
        case nexusDashboard = "nexus_dashboard"
        case container
        case deepseek
        case kimi
        case python
    }

    /// 存储后端：老格式是字符串 "sqlite"，新格式是 {"type": "sqlite"}
    var storageType: String? {
        storage?.type ?? storageDetail?.resolvedType
    }

    var pythonVersion: String? {
        guard let version = python?.version?.trimmingCharacters(in: .whitespaces),
              !version.isEmpty else { return nil }
        return version
    }

    /// 后端是不是 PyInstaller 打包的内嵌后端（它的 executable 就是 backend_server）
    var isEmbeddedBackend: Bool {
        guard let executable = python?.executable, !executable.isEmpty else { return false }
        return executable.contains("backend_server")
    }

    /// 已配置 API Key 的 provider：新旧两种健康检查结构都认
    var configuredProviders: [String] {
        var names = Set(
            (ai?.providers ?? [:]).compactMap { name, provider in
                provider.configured == true ? name : nil
            }
        )
        if deepseek?.configured == true { names.insert("deepseek") }
        if kimi?.configured == true { names.insert("kimi") }
        return names.sorted()
    }
}

/// /api/health 里的 storage 字段：当前后端返回字符串，未来可能是对象
enum StorageValue: Decodable {
    case name(String)
    case detail(StorageDetail)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let name = try? container.decode(String.self) {
            self = .name(name)
        } else {
            self = .detail(try container.decode(StorageDetail.self))
        }
    }

    var type: String? {
        switch self {
        case .name(let name): return name
        case .detail(let detail): return detail.resolvedType
        }
    }
}

/// /api/health 里的 python 字段（当前后端还没有）
struct PythonInfo: Decodable {
    let version: String?
    let executable: String?
}

/// 工具组的凭据状态（/api/health 里的 azure / meraki 字段）
struct ToolGroupStatus: Decodable {
    let configured: Bool?
    let credential: String?
    let hint: String?
    /// 多容器注册表才有：注册了几个容器
    let containerCount: Int?
    /// explicit（环境变量）/ default_chain（az login、托管身份等）/ probing（后台检测中）/ missing
    let credentialSource: String?
    /// 探测失败原因（仅在 missing 时有值）
    let probeError: String?

    enum CodingKeys: String, CodingKey {
        case configured
        case credential
        case hint
        case containerCount = "container_count"
        case credentialSource = "credential_source"
        case probeError = "probe_error"
    }
}

struct StorageDetail: Decodable {
    let backend: String?
    let type: String?
    let dbPath: String?
    let error: String?
    let taskTTLHours: Int?

    enum CodingKeys: String, CodingKey {
        case backend
        case type
        case dbPath = "db_path"
        case error
        case taskTTLHours = "task_ttl_hours"
    }

    var resolvedType: String? { type ?? backend }
}

// MARK: - 连接测试（POST /api/test_connection）

/// 一条明细：Azure 是订阅、Meraki 是组织、余额是币种 + 余额
struct TestConnectionDetail: Decodable, Hashable {
    let name: String?
    let id: String?
    let state: String?
    let currency: String?
    let totalBalance: AnyCodable?

    enum CodingKeys: String, CodingKey {
        case name
        case id
        case state
        case currency
        case totalBalance = "total_balance"
    }

    /// 详情行文案（列表里只显示名字，余额显示「CNY 9.71」）
    var displayLine: String {
        if let currency, let balance = totalBalance?.stringValue, !balance.isEmpty {
            return "\(currency) \(balance)"
        }
        if let name, !name.isEmpty {
            if let state, !state.isEmpty { return "\(name)（\(state)）" }
            return name
        }
        return id ?? ""
    }
}

struct TestConnectionResponse: Decodable {
    /// "success" / "error" / "not_implemented"
    let status: String?
    let message: String?
    let details: [TestConnectionDetail]?
    let durationMs: Int?

    enum CodingKeys: String, CodingKey {
        case status
        case message
        case details
        case durationMs = "duration_ms"
    }
}
