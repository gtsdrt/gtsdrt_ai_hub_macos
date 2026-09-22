import SwiftUI

struct ChatView: View {
    @ObservedObject var session: SessionStore
    /// 观察设置页里的 provider：在设置页切了 provider，这里的简称也要跟着刷新
    @ObservedObject private var settings: AppSettings
    @StateObject private var model: ChatViewModel
    @Environment(\.appFontScale) private var fontScale

    init(session: SessionStore) {
        self.session = session
        self.settings = session.settings
        _model = StateObject(wrappedValue: ChatViewModel(session: session))
    }

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            detail
        }
        .task {
            await model.refreshConversations()
            await model.refreshHealth()
        }
        // 后端状态变化（启动中 → 运行中）时自动补刷新，不需要用户先打字才会更新
        .onChange(of: session.backend.status) { newValue in
            guard newValue.isRunning else { return }
            Task {
                await model.refreshConversations()
                await model.refreshHealth()
            }
        }
    }

    // MARK: - 左侧会话列表

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack {
                Text("会话")
                    .appFont(.headline)
                Spacer()
                Button {
                    model.newConversation()
                } label: {
                    Image(systemName: "square.and.pencil")
                }
                .buttonStyle(.borderless)
                .help("新建对话")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)

            List {
                ForEach(model.conversations) { conversation in
                    Button {
                        Task { await model.open(conversation) }
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(conversation.title)
                                .lineLimit(1)
                            Text("\(conversation.messageCount ?? 0) 条消息")
                                .appFont(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 4)
                        .padding(.horizontal, 6)
                        .background(
                            RoundedRectangle(cornerRadius: 6)
                                .fill(
                                    model.conversationID == conversation.conversationID
                                        ? Color.accentColor.opacity(0.16)
                                        : Color.clear
                                )
                        )
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .contextMenu {
                        Button("删除这个会话", role: .destructive) {
                            Task { await model.delete(conversation) }
                        }
                    }
                }
            }
            .listStyle(.sidebar)
        }
        .navigationSplitViewColumnWidth(
            min: CGFloat(200).appScaled(by: fontScale),
            ideal: CGFloat(240).appScaled(by: fontScale),
            max: CGFloat(320).appScaled(by: fontScale)
        )
    }

    // MARK: - 右侧对话区

    private var detail: some View {
        VStack(spacing: 0) {
            messagesArea
            Divider()
            composer
        }
        .toolbar {
            ToolbarItem {
                StatusBadge(
                    text: model.backendSummary.isEmpty ? session.backend.status.text : model.backendSummary,
                    isActive: session.backend.status.isRunning
                )
            }
            ToolbarItem {
                Button {
                    model.newConversation()
                } label: {
                    Label("新对话", systemImage: "plus.bubble")
                }
            }
            ToolbarItem {
                Button {
                    session.logout()
                } label: {
                    Label("退出登录", systemImage: "rectangle.portrait.and.arrow.right")
                }
            }
        }
        .alert("已启用工具调用", isPresented: $model.showingToolsNotice) {
            Button("继续发送") { model.confirmToolsNotice() }
            Button("取消", role: .cancel) { model.dismissToolsNotice() }
        } message: {
            Text("已启用工具调用：AI 可能会查询你的 Azure 订阅或 Meraki 网络。每次调用都会在气泡中显示。")
        }
    }

    private var messagesArea: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if model.messages.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("开始新的对话")
                                .appFont(.title3)
                                .bold()
                            Text("输入内容后发送，应用会调用本地后端的 /api/chat_async，并每 2 秒轮询 /api/ai_task_status 获取结果。")
                                .appFont(.callout)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.top, 24)
                    }

                    ForEach(model.messages) { message in
                        MessageBubble(message: message)
                            .id(message.id)
                    }

                    if model.isSending {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 8) {
                                ProgressView().controlSize(.small)
                                Text(model.phaseText)
                                    .appFont(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            if !model.phaseHint.isEmpty {
                                Text(model.phaseHint)
                                    .appFont(.caption)
                                    .foregroundStyle(.orange)
                                    .padding(.leading, 22)
                            }
                        }
                        .padding(.leading, 4)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
            }
            .onChange(of: model.messages.count) { _ in
                guard let last = model.messages.last else { return }
                withAnimation {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let errorMessage = model.errorMessage {
                Text(errorMessage)
                    .appFont(.callout)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }

            if let backendHint = model.backendHint {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(backendHint)
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            toolsBar

            ZStack(alignment: .topLeading) {
                TextEditor(text: $model.input)
                    .appFont(.body)
                    .frame(
                        minHeight: CGFloat(68).appScaled(by: fontScale),
                        maxHeight: CGFloat(120).appScaled(by: fontScale)
                    )
                    .padding(4)
                    .scrollContentBackground(.hidden)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(Color.secondary.opacity(0.35))
                    )

                if model.input.isEmpty {
                    Text("输入消息，⌘↩ 发送")
                        .appFont(.body)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 12)
                        .allowsHitTesting(false)
                }
            }

            HStack(spacing: 10) {
                if model.isSending {
                    Button("取消") {
                        model.cancelSending()
                    }
                    Spacer()
                    Text(model.phaseText)
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                } else {
                    Button("发送") {
                        model.send()
                    }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(model.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                    Spacer()
                    Text("走本地后端 \(settings.backendBaseURL)/api")
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
    }

    /// 输入框上方的控制条：工具开关 + Provider 快切
    private var toolsBar: some View {
        HStack(spacing: 10) {
            Button {
                model.toggleTools()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "wrench.and.screwdriver.fill")
                        .appFont(.caption)
                    Text("工具")
                        .appFont(.caption)
                }
                .foregroundStyle(model.enableTools ? Color.white : Color.secondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: 6)
                        .fill(
                            model.enableTools
                                ? Color.accentColor
                                : Color.secondary.opacity(0.12)
                        )
                )
            }
            .buttonStyle(.plain)
            .help(
                model.enableTools
                    ? "工具调用已开启，AI 可执行 Azure/Meraki 查询"
                    : "工具调用已关闭，纯对话模式"
            )

            Menu {
                ForEach(model.providerOptions, id: \.self) { provider in
                    Button {
                        model.selectProvider(provider)
                    } label: {
                        if provider == model.provider {
                            Label(
                                AppSettings.displayName(for: provider),
                                systemImage: "checkmark"
                            )
                        } else {
                            Text(AppSettings.displayName(for: provider))
                        }
                    }
                }
            } label: {
                HStack(spacing: 3) {
                    Text(model.providerDisplayName)
                        .appFont(.caption)
                    Image(systemName: "chevron.down")
                        .appFont(size: 8, weight: .semibold)
                }
                .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("点击切换 AI Provider")

            Spacer()
        }
    }
}

private struct MessageBubble: View {
    let message: ChatMessage

    private var isUser: Bool { message.role == "user" }
    private var toolCalls: [ToolCallLog] { message.toolCallsLog ?? [] }

    var body: some View {
        HStack {
            if isUser {
                Spacer(minLength: 60)
            }

            VStack(alignment: .leading, spacing: 5) {
                Text(isUser ? "我" : "AI")
                    .appFont(.caption)
                    .foregroundStyle(.secondary)

                Text(message.content)
                    .appFont(.body)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(10)
                    .background(
                        RoundedRectangle(cornerRadius: 10)
                            .fill(
                                isUser
                                    ? Color.accentColor.opacity(0.15)
                                    : Color(nsColor: .controlBackgroundColor)
                            )
                    )

                if !isUser, !toolCalls.isEmpty {
                    ToolCallLogSection(logs: toolCalls)
                }

                if !isUser, toolsDisabledWarning {
                    Label("后端未启用工具，请检查设置", systemImage: "exclamationmark.triangle.fill")
                        .appFont(.caption)
                        .foregroundStyle(.orange)
                        .padding(.leading, 2)
                } else if !isUser, toolsNotTriggeredNote {
                    Text("本轮未触发工具调用")
                        .appFont(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 2)
                }
            }

            if !isUser {
                Spacer(minLength: 60)
            }
        }
    }

    /// 开关开着，但后端明确回报 tools_enabled == false
    private var toolsDisabledWarning: Bool {
        message.toolsRequested == true && message.toolsEnabled == false
    }

    /// 开了工具、后端也没否认，但本轮没有工具调用记录
    /// （异步结果暂时不带 tools_enabled，所以 nil 也按「开关生效」处理）
    private var toolsNotTriggeredNote: Bool {
        guard message.toolsEnabled != false, toolCalls.isEmpty else { return false }
        return message.toolsRequested == true
    }
}

// MARK: - 工具调用记录

/// assistant 气泡下方的工具调用折叠区。
/// 展开状态放在 @State 里，不写入历史，重新打开会话时默认折叠。
private struct ToolCallLogSection: View {
    let logs: [ToolCallLog]

    @State private var isExpanded = false
    @State private var previewLog: ToolCallLog?

    /// 按 iteration 排序（同轮次内保持后端返回的顺序）
    private var sortedLogs: [ToolCallLog] {
        logs.enumerated()
            .sorted { lhs, rhs in
                lhs.element.iteration == rhs.element.iteration
                    ? lhs.offset < rhs.offset
                    : lhs.element.iteration < rhs.element.iteration
            }
            .map(\.element)
    }

    private var hasError: Bool {
        logs.contains { $0.resultStatus == "error" }
    }

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(sortedLogs) { log in
                    ToolCallCard(log: log) {
                        previewLog = log
                    }
                }
            }
            .padding(.top, 8)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "wrench.and.screwdriver.fill")
                    .appFont(.caption)
                    .foregroundStyle(.secondary)
                Text("调用了 \(logs.count) 个工具")
                    .appFont(.caption)
                Spacer(minLength: 8)
                if hasError {
                    Text("⚠ 有失败")
                        .appFont(.caption2)
                        .foregroundStyle(.red)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.red.opacity(0.12)))
                }
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color.gray.opacity(0.08))
        )
        .sheet(item: $previewLog) { log in
            ToolResultSheet(log: log)
        }
    }
}

/// 一次工具调用的卡片
private struct ToolCallCard: View {
    @Environment(\.appFontScale) private var fontScale

    let log: ToolCallLog
    let onShowResult: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(log.toolName)
                    .appFont(.caption, design: .monospaced)
                    .fontWeight(.semibold)
                    .lineLimit(1)

                Text(log.isSuccess ? "✓" : "✗")
                    .appFont(.caption2)
                    .foregroundStyle(log.isSuccess ? Color.green : Color.red)
                    .frame(
                        width: CGFloat(16).appScaled(by: fontScale),
                        height: CGFloat(16).appScaled(by: fontScale)
                    )
                    .background(
                        Circle().fill((log.isSuccess ? Color.green : Color.red).opacity(0.15))
                    )
                    .help(log.resultStatus)

                Spacer(minLength: 8)

                Text("\(log.durationMs) ms")
                    .appFont(.caption)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }

            Text(log.arguments.isEmpty ? "（无参数）" : log.argumentsJSON)
                .appFont(.caption, design: .monospaced)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(6)
                .frame(maxWidth: .infinity, alignment: .leading)

            Button("查看结果", action: onShowResult)
                .buttonStyle(.link)
                .appFont(.caption)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.gray.opacity(0.12))
        )
    }
}

/// 「查看结果」弹出的完整 result_preview
private struct ToolResultSheet: View {
    let log: ToolCallLog

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text(log.toolName)
                    .appFont(.headline)
                    .fontDesign(.monospaced)
                Text("第 \(log.iteration) 轮 · \(log.durationMs) ms")
                    .appFont(.caption)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 12)
                Button("关闭") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding(12)

            Divider()

            ScrollView {
                Text(displayText)
                    .appFont(.caption, design: .monospaced)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
            }
        }
        .frame(minWidth: 520, idealWidth: 640, minHeight: 320, idealHeight: 420)
    }

    /// 结果是完整 JSON 时顺手格式化；被截断过的内容会退回原文
    private var displayText: String {
        log.resultDisplayText
    }
}
