import Foundation

/// 登录页的首次运行检查清单：靠 /api/health 判断配置齐不齐。
/// 全部就绪（允许有警告）或用户收起时停止轮询。
@MainActor
final class FirstRunChecklist: ObservableObject {
    enum Item: String, CaseIterable, Identifiable {
        case backend
        case python
        case provider
        case azure
        case meraki
        case storage

        var id: String { rawValue }

        var title: String {
            switch self {
            case .backend: return "后端进程"
            case .python: return "Python 环境"
            case .provider: return "AI Provider"
            case .azure: return "Azure 凭据"
            case .meraki: return "Meraki 凭据"
            case .storage: return "存储"
            }
        }

        /// 「去配置」跳到设置页的哪一块
        var section: SettingsSection {
            switch self {
            case .backend, .python, .storage: return .backend
            case .provider: return .provider
            case .azure: return .azure
            case .meraki: return .meraki
            }
        }
    }

    enum Status: Equatable {
        /// ⟳ 灰：检查中 / 等待后端
        case checking(String)
        /// ✓ 绿
        case ok(String)
        /// ✗ 红
        case failed(String)
        /// ⚠ 橙（比如存储是 memory）
        case warning(String)

        var text: String {
            switch self {
            case .checking(let text), .ok(let text), .failed(let text), .warning(let text):
                return text
            }
        }

        var isOK: Bool {
            if case .ok = self { return true }
            return false
        }

        var isChecking: Bool {
            if case .checking = self { return true }
            return false
        }

        var isFailed: Bool {
            if case .failed = self { return true }
            return false
        }

        var isWarning: Bool {
            if case .warning = self { return true }
            return false
        }
    }

    /// 轮询间隔：5 秒
    static let pollIntervalNanoseconds: UInt64 = 5_000_000_000

    @Published private(set) var statuses: [Item: Status] = [:]
    /// 没有 ✗、也没有还在检查的项
    @Published private(set) var isReady = false
    @Published private(set) var hasWarnings = false
    @Published private(set) var lastCheckedAt: Date?

    private let session: SessionStore
    private var pollTask: Task<Void, Never>?

    init(session: SessionStore) {
        self.session = session
    }

    func status(for item: Item) -> Status {
        statuses[item] ?? .checking("检查中…")
    }

    // MARK: - 轮询

    /// 页面出现时调用：先查一次，之后每 5 秒一次，直到全部就绪
    func start() {
        stop()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.checkNow()
                if self.isReady { return }
                try? await Task.sleep(nanoseconds: Self.pollIntervalNanoseconds)
            }
        }
    }

    /// 用户收起 / 离开登录页时停止轮询
    func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// 立刻重查一次（从设置页返回时用）
    func refresh() {
        Task { [weak self] in
            await self?.checkNow()
        }
    }

    // MARK: - 检查

    private func checkNow() async {
        let client = APIClient(baseURLString: session.settings.backendBaseURL, timeout: 5)

        do {
            let health = try await client.health()
            apply(health: health)
        } catch {
            applyBackendOffline()
        }

        lastCheckedAt = Date()
    }

    private func apply(health: HealthResponse) {
        var statuses: [Item: Status] = [:]

        statuses[.backend] = health.status == "ok"
            ? .ok("已连接 \(session.settings.backendBaseURL)")
            : .failed("后端状态异常：\(health.status)")

        // 内嵌后端 → 不需要用户关心 Python；开发模式 → 显示真实版本
        if health.isEmbeddedBackend {
            statuses[.python] = .ok("内嵌后端已就绪")
        } else if let version = health.pythonVersion {
            statuses[.python] = .ok("Python \(version)")
        } else {
            statuses[.python] = .ok("已就绪（后端未上报版本）")
        }

        let providers = health.configuredProviders
        if providers.isEmpty {
            statuses[.provider] = .failed("未配置 API Key（当前是 mock 模式）")
        } else {
            let names = providers.map { AppSettings.displayName(for: $0) }.joined(separator: "、")
            statuses[.provider] = .ok("已配置 \(names)")
        }

        statuses[.azure] = health.azure?.configured == true
            ? .ok("已配置")
            : .failed("未配置")
        statuses[.meraki] = health.meraki?.configured == true
            ? .ok("已配置")
            : .failed("未配置")

        switch health.storageType?.lowercased() {
        case "sqlite":
            statuses[.storage] = .ok("sqlite（重启保留历史）")
        case "memory":
            statuses[.storage] = .warning("memory：重启会丢历史")
        case .some(let other):
            statuses[.storage] = .warning("\(other)：重启可能丢历史")
        case nil:
            statuses[.storage] = .failed("后端未上报存储类型")
        }

        self.statuses = statuses
        self.isReady = statuses.values.allSatisfy { !$0.isFailed && !$0.isChecking }
        self.hasWarnings = statuses.values.contains { $0.isWarning }
    }

    private func applyBackendOffline() {
        statuses = [
            .backend: .failed("后端未运行"),
            .python: .checking("等待后端"),
            .provider: .checking("等待后端"),
            .azure: .checking("等待后端"),
            .meraki: .checking("等待后端"),
            .storage: .checking("等待后端"),
        ]
        isReady = false
        hasWarnings = false
    }
}
