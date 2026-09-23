import Foundation

enum ChatError: LocalizedError {
    case taskFailed(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .taskFailed(let message):
            return message
        case .cancelled:
            return L("已取消本次请求")
        }
    }
}

/// 对话页的状态机：提交 /api/chat_async 后每 2 秒轮询 /api/ai_task_status
@MainActor
final class ChatViewModel: ObservableObject {
    /// 轮询间隔：2 秒
    static let pollIntervalNanoseconds: UInt64 = 2_000_000_000
    /// 最多轮询 300 次（约 10 分钟）
    static let maxPollAttempts = 300
    /// 轮询超过这个秒数后提示「可能在执行工具调用」
    static let slowPollThreshold: TimeInterval = 30

    /// UserDefaults key
    enum DefaultsKey {
        static let enableTools = "chat.enableTools"
        static let toolsNoticeAcknowledged = "chat.toolsNoticeAcknowledged"
    }

    @Published var messages: [ChatMessage] = []
    @Published var conversations: [ConversationSummary] = []
    @Published var input: String = ""
    @Published var isSending = false
    @Published var phaseText: String = ""
    /// 慢轮询提示（为空时不显示）
    @Published var phaseHint: String = ""
    @Published var errorMessage: String?
    /// 后端还在启动时的友好提示（不是错误）；连上后自动消失
    @Published var backendHint: String?
    @Published var backendSummary: String = ""
    @Published var selectedConversationID: String?

    /// 工具调用开关：开启后请求带 enable_tools，立即影响下一次发送
    @Published var enableTools: Bool {
        didSet {
            guard enableTools != oldValue else { return }
            defaults.set(enableTools, forKey: DefaultsKey.enableTools)
        }
    }

    /// 当前 provider：直接读设置页那份 settings.defaultProvider，避免两处状态不同步
    var provider: String { session.settings.defaultProvider }

    /// 一次性提示：是否已经看过「AI 可能查询 Azure/Meraki」的说明
    @Published private(set) var toolsNoticeAcknowledged: Bool
    /// 第一次开着工具发送时，先弹这个提示
    @Published var showingToolsNotice = false

    private(set) var conversationID: String?

    private let session: SessionStore
    private var sendTask: Task<Void, Never>?
    /// 等后端就绪后自动补刷新的任务
    private var retryTask: Task<Void, Never>?
    private let defaults = UserDefaults.standard
    /// 等待用户在一次性提示里确认的输入
    private var pendingPrompt: String?

    /// 一次对话的结果：正文 + 工具调用记录
    private struct ChatOutcome {
        let content: String
        let toolCallsLog: [ToolCallLog]?
        let iterationsUsed: Int?
        let toolsEnabled: Bool?
    }

    init(session: SessionStore) {
        self.session = session
        self.enableTools = UserDefaults.standard.bool(forKey: DefaultsKey.enableTools)
        self.toolsNoticeAcknowledged = UserDefaults.standard.bool(
            forKey: DefaultsKey.toolsNoticeAcknowledged
        )
    }

    var busy: Bool { isSending }

    // MARK: - 工具开关 / Provider

    var providerOptions: [String] { AppSettings.providerOptions }

    var providerDisplayName: String {
        AppSettings.displayName(for: provider)
    }

    func toggleTools() {
        enableTools.toggle()
    }

    /// 切换 provider，立即影响下一次发送
    func selectProvider(_ newValue: String) {
        guard provider != newValue else { return }
        session.settings.defaultProvider = newValue
        session.settings.persistQuietly()
    }

    /// 用户在一次性提示里点了「继续发送」
    func confirmToolsNotice() {
        toolsNoticeAcknowledged = true
        defaults.set(true, forKey: DefaultsKey.toolsNoticeAcknowledged)
        showingToolsNotice = false

        guard let prompt = pendingPrompt else { return }
        pendingPrompt = nil
        startSending(prompt: prompt)
    }

    /// 用户在一次性提示里点了「取消」
    func dismissToolsNotice() {
        pendingPrompt = nil
        showingToolsNotice = false
    }

    // MARK: - 会话列表

    func refreshConversations() async {
        do {
            conversations = try await session.api.conversations()
            backendHint = nil
        } catch {
            handle(error)
        }
    }

    func refreshHealth() async {
        guard let health = try? await session.api.health(), let ai = health.ai else {
            backendSummary = ""
            return
        }

        let configured = (ai.providers ?? [:])
            .filter { $0.value.configured == true }
            .keys
            .sorted()

        if configured.isEmpty {
            backendSummary = L("mock 模式（后端未配置 API Key）")
        } else {
            backendSummary = L("真实调用：") + configured.joined(separator: ", ")
        }
    }

    // MARK: - 会话操作

    func newConversation() {
        cancelSending()
        conversationID = nil
        selectedConversationID = nil
        messages = []
        errorMessage = nil
        phaseText = ""
        phaseHint = ""
    }

    func open(_ conversation: ConversationSummary) async {
        cancelSending()
        errorMessage = nil
        conversationID = conversation.conversationID
        selectedConversationID = conversation.conversationID
        phaseText = ""
        phaseHint = ""

        do {
            let history = try await session.api.history(conversationID: conversation.conversationID)
            messages = history.filter { $0.role != "system" }
        } catch {
            handle(error)
        }
    }

    func delete(_ conversation: ConversationSummary) async {
        do {
            try await session.api.deleteConversation(conversationID: conversation.conversationID)
            if conversationID == conversation.conversationID {
                newConversation()
            }
            await refreshConversations()
        } catch {
            handle(error)
        }
    }

    // MARK: - 发送

    func send() {
        let prompt = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty, !isSending else { return }

        // 开着工具但还没看过说明时，先弹一次性提示，确认后再发
        if enableTools, !toolsNoticeAcknowledged {
            pendingPrompt = prompt
            showingToolsNotice = true
            return
        }

        startSending(prompt: prompt)
    }

    /// 真正把用户消息塞进列表并提交任务
    private func startSending(prompt: String) {
        guard !isSending else { return }

        // 开关 & provider 在发送这一刻定下来，中途改动只影响下一次
        let toolsRequested = enableTools
        let selectedProvider = provider

        messages.append(ChatMessage(role: "user", content: prompt))
        input = ""
        errorMessage = nil
        isSending = true
        phaseText = L("正在提交任务…")
        phaseHint = ""

        sendTask = Task {
            await performSend(
                prompt: prompt,
                toolsRequested: toolsRequested,
                provider: selectedProvider
            )
        }
    }

    func cancelSending() {
        sendTask?.cancel()
        sendTask = nil
        if isSending {
            isSending = false
            phaseText = ""
            phaseHint = ""
            errorMessage = L("已取消本次请求")
        }
    }

    private func performSend(prompt: String, toolsRequested: Bool, provider: String) async {
        defer {
            isSending = false
            sendTask = nil
        }

        do {
            let payload = ChatRequestPayload(
                prompt: prompt,
                conversationID: conversationID,
                aiProvider: provider,
                systemPrompt: nil,
                webhookURL: nil,
                enableTools: toolsRequested
            )

            let started = try await session.api.startChat(payload: payload)
            if let newID = started.conversationID {
                conversationID = newID
                selectedConversationID = newID
            }
            phaseText = L("任务已受理（{0}…），等待结果…", String(started.instanceID.prefix(8)))

            let outcome = try await pollResult(instanceID: started.instanceID)
            let logs = outcome.toolCallsLog
            messages.append(
                ChatMessage(
                    role: "assistant",
                    content: outcome.content,
                    toolCallsLog: logs,
                    iterationsUsed: outcome.iterationsUsed,
                    toolsEnabled: outcome.toolsEnabled,
                    toolsRequested: toolsRequested
                )
            )
            phaseText = ""
            phaseHint = ""
            await refreshConversations()
        } catch is CancellationError {
            phaseText = ""
            phaseHint = ""
            errorMessage = L("已取消本次请求")
        } catch ChatError.cancelled {
            phaseText = ""
            phaseHint = ""
            errorMessage = L("已取消本次请求")
        } catch {
            phaseText = ""
            phaseHint = ""
            handle(error)
        }
    }

    /// 每 2 秒查询一次任务状态，直到 completed / failed
    private func pollResult(instanceID: String) async throws -> ChatOutcome {
        let startedAt = Date()

        for attempt in 1...Self.maxPollAttempts {
            try Task.checkCancellation()

            let status = try await session.api.taskStatus(instanceID: instanceID)

            switch status.status {
            case "completed":
                let result = status.result
                let content = (result?.response?.isEmpty == false)
                    ? result?.response ?? ""
                    : L("（后端已完成任务，但返回内容为空）")
                let logs = result?.toolCallsLog
                return ChatOutcome(
                    content: content,
                    toolCallsLog: (logs?.isEmpty == false) ? logs : nil,
                    iterationsUsed: result?.iterationsUsed,
                    toolsEnabled: result?.toolsEnabled
                )
            case "failed":
                throw ChatError.taskFailed(status.error ?? status.message ?? L("任务执行失败"))
            default:
                let elapsed = Date().timeIntervalSince(startedAt)
                phaseText = L("第 {0} 次轮询…（已等待 {1}s）", String(attempt), String(Int(elapsed)))
                phaseHint = elapsed >= Self.slowPollThreshold
                    ? L("AI 正在执行工具调用，可能需要 1-2 分钟")
                    : ""
                try await Task.sleep(nanoseconds: Self.pollIntervalNanoseconds)
            }
        }

        throw ChatError.taskFailed(
            L("轮询超时：超过 {0} 次（约 10 分钟）仍未拿到结果", String(Self.maxPollAttempts))
        )
    }

    private func handle(_ error: Error) {
        if let apiError = error as? APIError, case .unauthorized = apiError {
            session.handleUnauthorized()
            return
        }

        // 连不上但后端本来就没起来（内嵌后端首次启动要解包 6~8 秒）：
        // 这不是错误，不该甩一行红字让用户以为坏了，改成「启动中」并自动重试。
        if let apiError = error as? APIError, case .transport = apiError,
           !session.backend.status.isRunning {
            errorMessage = nil
            backendHint = L("本地后端正在启动，连上后会自动刷新…")
            scheduleRetryWhenBackendReady()
            return
        }

        backendHint = nil
        errorMessage = error.localizedDescription
    }

    /// 后端就绪后自动补一次会话列表 / 健康状态，不需要用户乱点或打字才会刷新
    private func scheduleRetryWhenBackendReady() {
        guard retryTask == nil else { return }

        retryTask = Task { [weak self] in
            defer { self?.retryTask = nil }

            for _ in 0..<40 {
                if Task.isCancelled { return }
                guard let self else { return }

                if self.session.backend.status.isRunning {
                    await self.refreshConversations()
                    await self.refreshHealth()
                    self.backendHint = nil
                    return
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
    }
}
