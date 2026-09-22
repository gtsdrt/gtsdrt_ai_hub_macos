import Foundation
import SwiftUI

/// 设置页「测试连接 / 测试余额」的状态机。
///
/// 关键点：后端进程只在启动时读一次环境变量，所以测试前必须先确认
/// 当前页面上填的凭据已经进了后端进程——否则会出现「填了新 Key，测的是旧 Key」。
@MainActor
final class ConnectionTester: ObservableObject {
    /// 连接测试的目标（provider 直接用名字当 target）
    enum Target {
        static let azure = "azure"
        static let meraki = "meraki"
    }

    enum State: Equatable {
        case idle
        case running
        case success(message: String, details: [String])
        case failure(String)
        /// 后端明确回 not_implemented（比如 Kimi 余额工具）
        case unavailable(String)
        /// 需要重启后端才能用当前凭据测试
        case needsRestart(String)
    }

    /// 结果展示多久后自动淡出
    static let autoHideDelay: TimeInterval = 5

    @Published private(set) var states: [String: State] = [:]

    private let session: SessionStore
    private var autoHideTasks: [String: Task<Void, Never>] = [:]

    init(session: SessionStore) {
        self.session = session
    }

    func state(for target: String) -> State {
        states[target] ?? .idle
    }

    func isRunning(_ target: String) -> Bool {
        state(for: target) == .running
    }

    // MARK: - 入口

    /// 点「测试连接 / 测试余额」：先保存当前输入，再保证后端进程用的是这份凭据
    func test(target: String) {
        guard !isRunning(target) else { return }

        guard persistCurrentCredentials(target: target) else { return }

        set(.running, for: target)
        Task {
            await runTest(target: target, mayRestartBackend: session.settings.autoStartBackend)
        }
    }

    /// 「一键重启并测试」：不管自动启动开关，直接重启后端再测
    func restartBackendAndTest(target: String) {
        guard !isRunning(target) else { return }

        guard persistCurrentCredentials(target: target) else { return }

        set(.running, for: target)
        Task {
            await restartBackend()
            guard session.backend.status.isRunning else {
                set(.failure("后端没起来：\(session.backend.status.text)"), for: target)
                return
            }
            await performRequest(target: target)
        }
    }

    /// 用户改了任一字段 / provider 时清空结果
    func clearAll() {
        for task in autoHideTasks.values {
            task.cancel()
        }
        autoHideTasks.removeAll()
        withAnimation(.easeInOut(duration: 0.25)) {
            states.removeAll()
        }
    }

    // MARK: - 流程

    private func persistCurrentCredentials(target: String) -> Bool {
        do {
            try session.settings.persist()
            return true
        } catch {
            set(.failure("保存凭据失败：\(error.localizedDescription)"), for: target)
            return false
        }
    }

    private func runTest(target: String, mayRestartBackend: Bool) async {
        let backend = session.backend
        let settings = session.settings

        // 外部启动的后端不知道带的是哪份凭据，一律当成「没生效」
        let credentialsApplied = settings.credentialsAreApplied && !backend.isExternallyStarted

        if credentialsApplied {
            guard backend.status.isRunning else {
                set(.needsRestart("后端未运行，需要先启动后端"), for: target)
                return
            }
        } else if mayRestartBackend {
            await restartBackend()
            guard session.backend.status.isRunning else {
                set(.failure("后端重启失败：\(session.backend.status.text)"), for: target)
                return
            }
        } else {
            let suffix = backend.isExternallyStarted
                ? "（后端是外部启动的，建议在终端里重启）"
                : ""
            set(.needsRestart("凭据已更新，需要重启后端生效" + suffix), for: target)
            return
        }

        await performRequest(target: target)
    }

    private func restartBackend() async {
        let settings = session.settings
        session.backend.restart(settings: settings)
        // restart() 内部先 stop、0.5s 后再 start；先等一下再轮询，
        // 否则 BackendController 会因为「进程为 nil」直接判定失败
        try? await Task.sleep(nanoseconds: 800_000_000)
        await session.backend.waitUntilHealthy(settings: settings, timeout: 60)
    }

    private func performRequest(target: String) async {
        do {
            let response = try await session.api.testConnection(target: target)
            let details = (response.details ?? [])
                .prefix(3)
                .map(\.displayLine)
                .filter { !$0.isEmpty }

            switch response.status {
            case "success":
                set(.success(message: response.message ?? "连接成功", details: details), for: target)
            case "not_implemented":
                set(.unavailable(response.message ?? "该目标暂不支持测试"), for: target)
            default:
                set(.failure(response.message ?? "连接失败"), for: target)
            }
        } catch {
            set(.failure(error.localizedDescription), for: target)
        }
    }

    // MARK: - 状态写入

    private func set(_ state: State, for target: String) {
        autoHideTasks[target]?.cancel()
        autoHideTasks[target] = nil

        withAnimation(.easeInOut(duration: 0.25)) {
            states[target] = state
        }

        // 结果 5 秒后自动淡出；running / needsRestart 不自动消失
        switch state {
        case .running, .needsRestart, .idle:
            return
        case .success, .failure, .unavailable:
            break
        }

        autoHideTasks[target] = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.autoHideDelay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            guard let self, self.states[target] == state else { return }
            withAnimation(.easeInOut(duration: 0.4)) {
                self.states[target] = .idle
            }
        }
    }
}
