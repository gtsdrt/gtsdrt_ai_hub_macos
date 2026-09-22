import Foundation

enum APIError: LocalizedError {
    case invalidBaseURL(String)
    case http(status: Int, message: String)
    case unauthorized
    case decoding(String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .invalidBaseURL(let value):
            return "后端地址无效：\(value)"
        case .http(let status, let message):
            return "请求失败（HTTP \(status)）：\(message)"
        case .unauthorized:
            return "登录已失效，请重新登录"
        case .decoding(let message):
            return "解析响应失败：\(message)"
        case .transport(let message):
            return "无法连接本地后端：\(message)"
        }
    }
}

/// 调用本地 Python 后端（默认 http://127.0.0.1:8000/api）
final class APIClient {
    var baseURLString: String
    var token: String?

    private let session: URLSession
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()
    private let timeout: TimeInterval

    init(baseURLString: String, token: String? = nil, timeout: TimeInterval = 30) {
        self.baseURLString = baseURLString
        self.token = token
        self.timeout = timeout

        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.waitsForConnectivity = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        self.session = URLSession(configuration: configuration)
    }

    // MARK: - 端点

    func url(for path: String) throws -> URL {
        var base = baseURLString.trimmingCharacters(in: .whitespacesAndNewlines)
        while base.hasSuffix("/") {
            base.removeLast()
        }
        guard !base.isEmpty, let url = URL(string: base + path) else {
            throw APIError.invalidBaseURL(baseURLString)
        }
        return url
    }

    func health() async throws -> HealthResponse {
        try await send(path: "/api/health", method: "GET", body: nil, authorized: false)
    }

    func login(username: String, password: String) async throws -> LoginResponse {
        let body = try encoder.encode(LoginPayload(username: username, password: password))
        return try await send(path: "/api/login", method: "POST", body: body, authorized: false)
    }

    func startGoogleLogin() async throws -> GoogleAuthStartResponse {
        try await send(path: "/api/auth/google/start", method: "POST", body: nil, authorized: false)
    }

    func googleLoginStatus(state: String) async throws -> GoogleAuthStatusResponse {
        let encoded = state.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? state
        return try await send(
            path: "/api/auth/google/status?state=\(encoded)",
            method: "GET",
            body: nil,
            authorized: false
        )
    }

    func startGitHubLogin() async throws -> GitHubAuthStartResponse {
        try await send(path: "/api/auth/github/start", method: "POST", body: nil, authorized: false)
    }

    func githubLoginStatus(state: String) async throws -> GitHubAuthStatusResponse {
        let encoded = state.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? state
        return try await send(
            path: "/api/auth/github/status?state=\(encoded)",
            method: "GET",
            body: nil,
            authorized: false
        )
    }

    func startChat(payload: ChatRequestPayload) async throws -> ChatAsyncResponse {
        let body = try encoder.encode(payload)
        return try await send(path: "/api/chat_async", method: "POST", body: body, authorized: true)
    }

    /// 同步对话（阻塞，长回复请用 startChat + taskStatus 轮询）
    func chat(payload: ChatRequestPayload) async throws -> ChatResponse {
        let body = try encoder.encode(payload)
        return try await send(path: "/api/chat", method: "POST", body: body, authorized: true)
    }

    /// 连接测试：直接调用工具、不走 AI 循环（target: azure / meraki / deepseek / kimi）
    func testConnection(target: String) async throws -> TestConnectionResponse {
        let body = try encoder.encode(TestConnectionRequest(target: target))
        return try await send(path: "/api/test_connection", method: "POST", body: body, authorized: true)
    }

    func taskStatus(instanceID: String) async throws -> TaskStatusResponse {
        let encoded = instanceID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? instanceID
        return try await send(path: "/api/ai_task_status?instance_id=\(encoded)", method: "GET", body: nil, authorized: true)
    }

    func conversations() async throws -> [ConversationSummary] {
        let response: ConversationListResponse = try await send(
            path: "/api/chat_conversations", method: "GET", body: nil, authorized: true
        )
        return response.conversations
    }

    func history(conversationID: String) async throws -> [ChatMessage] {
        let encoded = conversationID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? conversationID
        let response: HistoryResponse = try await send(
            path: "/api/chat_history?conversation_id=\(encoded)", method: "GET", body: nil, authorized: true
        )
        return response.messages
    }

    func deleteConversation(conversationID: String) async throws {
        let encoded = conversationID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? conversationID
        _ = try await sendRaw(path: "/api/chat_conversation?conversation_id=\(encoded)", method: "DELETE", body: nil, authorized: true)
    }

    // MARK: - 底层请求

    private struct LoginPayload: Encodable {
        let username: String
        let password: String
    }

    private struct TestConnectionRequest: Encodable {
        let target: String
    }

    private func send<T: Decodable>(
        path: String,
        method: String,
        body: Data?,
        authorized: Bool
    ) async throws -> T {
        let data = try await sendRaw(path: path, method: method, body: body, authorized: authorized)
        if data.isEmpty {
            throw APIError.decoding("响应为空")
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            let preview = String(data: data.prefix(200), encoding: .utf8) ?? ""
            throw APIError.decoding("\(error.localizedDescription)｜原始响应：\(preview)")
        }
    }

    private func sendRaw(
        path: String,
        method: String,
        body: Data?,
        authorized: Bool
    ) async throws -> Data {
        var request = URLRequest(url: try url(for: path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if authorized, let token, !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("响应类型异常")
        }

        guard (200..<300).contains(http.statusCode) else {
            if http.statusCode == 401 {
                throw APIError.unauthorized
            }
            let message = (try? decoder.decode(APIErrorBody.self, from: data))?.error
                ?? String(data: data.prefix(300), encoding: .utf8)
                ?? "未知错误"
            throw APIError.http(status: http.statusCode, message: message)
        }

        return data
    }
}
