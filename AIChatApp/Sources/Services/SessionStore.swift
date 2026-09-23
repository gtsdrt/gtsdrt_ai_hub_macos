import AppKit
import Combine
import Foundation

/// 全局会话状态：token、设置、后端进程控制器、API 客户端
@MainActor
final class SessionStore: ObservableObject {
    @Published private(set) var token: String?
    @Published var loginError: String?
    @Published var isLoggingIn = false
    @Published var notice: String?

    let settings: AppSettings
    let backend: BackendController
    let api: APIClient

    private let keychain = KeychainStore.shared
    /// 转发 BackendController 的变化：视图通常只观察 SessionStore，
    /// 后端状态变了（启动中→运行中）如果不转发，界面要等用户下一次操作才会刷新。
    private var cancellables: Set<AnyCancellable> = []
    private var watchdog: Task<Void, Never>?

    init() {
        let settings = AppSettings()
        let token = KeychainStore.shared.read(KeychainStore.Keys.token)

        self.settings = settings
        self.backend = BackendController()
        self.token = token
        self.api = APIClient(baseURLString: settings.backendBaseURL, token: token)

        backend.objectWillChange
            .sink { [weak self] in self?.objectWillChange.send() }
            .store(in: &cancellables)

        // 设置变化（界面语言等）也要让 App 菜单 / 窗口标题跟着刷新
        settings.objectWillChange
            .sink { [weak self] in self?.objectWillChange.send() }
            .store(in: &cancellables)
    }

    var isAuthenticated: Bool {
        !(token ?? "").isEmpty
    }

    // MARK: - 生命周期

    func bootstrap() async {
        syncAPIClient()

        if settings.autoStartBackend, !backend.status.isRunning {
            backend.start(settings: settings)
        } else {
            await backend.refreshExternalStatus(settings: settings)
        }

        startBackendWatchdog()
    }

    /// 空闲时也能发现「后端其实已经起来了」（比如在终端里手动启动的），
    /// 不必等用户打字/点按钮才刷新状态。未运行时才探测，开销可忽略。
    private func startBackendWatchdog() {
        guard watchdog == nil else { return }

        watchdog = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard let self else { return }
                guard !self.backend.status.isRunning else { continue }
                await self.backend.refreshExternalStatus(settings: self.settings)
            }
        }
    }

    func syncAPIClient() {
        api.baseURLString = settings.backendBaseURL
        api.token = token
    }

    // MARK: - 登录 / 登出

    func login(username: String, password: String, rememberPassword: Bool) async {
        isLoggingIn = true
        loginError = nil
        notice = nil
        syncAPIClient()

        do {
            let response = try await api.login(username: username, password: password)
            token = response.token
            api.token = response.token
            keychain.saveQuietly(response.token, for: KeychainStore.Keys.token)

            settings.username = username
            if rememberPassword {
                settings.loginPassword = password
                keychain.saveQuietly(password, for: KeychainStore.Keys.loginPassword)
            } else {
                settings.loginPassword = ""
                keychain.delete(KeychainStore.Keys.loginPassword)
            }
            settings.persistQuietly()
        } catch {
            loginError = error.localizedDescription
        }

        isLoggingIn = false
    }

    /// 使用系统浏览器完成 Google OAuth。回调落到本地后端，App 只轮询一次性 state，
    /// 最终保存的仍是后端签发的 JWT，而不是 Google access token。
    func loginWithGoogle() async {
        isLoggingIn = true
        loginError = nil
        notice = nil
        syncAPIClient()

        do {
            let started = try await api.startGoogleLogin()
            guard let authorizationURL = URL(string: started.authorizationURL),
                  NSWorkspace.shared.open(authorizationURL) else {
                throw APIError.transport(L("无法打开 Google 登录页面"))
            }

            let deadline = Date().addingTimeInterval(TimeInterval(started.expiresIn))
            while Date() < deadline {
                try await Task.sleep(nanoseconds: 1_000_000_000)
                let result = try await api.googleLoginStatus(state: started.state)
                switch result.status {
                case "complete":
                    guard let newToken = result.token, !newToken.isEmpty else {
                        throw APIError.decoding(L("Google 登录成功，但后端没有返回 JWT"))
                    }
                    token = newToken
                    api.token = newToken
                    keychain.saveQuietly(newToken, for: KeychainStore.Keys.token)
                    if let email = result.email, !email.isEmpty {
                        settings.username = email
                        settings.persistQuietly()
                    }
                    notice = L("已使用 Google 账号登录")
                    isLoggingIn = false
                    return
                case "error":
                    throw APIError.http(status: 401, message: result.error ?? L("Google 登录失败"))
                default:
                    continue
                }
            }
            throw APIError.transport(L("Google 登录已超时，请重试"))
        } catch {
            loginError = error.localizedDescription
        }

        isLoggingIn = false
    }

    /// GitHub Device Flow：把短验证码复制到剪贴板并打开 GitHub，后端按官方间隔轮询授权结果。
    func loginWithGitHub() async {
        isLoggingIn = true
        loginError = nil
        notice = nil
        syncAPIClient()

        do {
            let started = try await api.startGitHubLogin()
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(started.userCode, forType: .string)
            notice = L("GitHub 验证码 {0} 已复制，请在浏览器中粘贴", started.userCode)

            guard let verificationURL = URL(string: started.verificationURI),
                  NSWorkspace.shared.open(verificationURL) else {
                throw APIError.transport(L("无法打开 GitHub 登录页面"))
            }

            let deadline = Date().addingTimeInterval(TimeInterval(started.expiresIn))
            let pollNanoseconds = UInt64(max(started.interval, 5)) * 1_000_000_000
            while Date() < deadline {
                try await Task.sleep(nanoseconds: pollNanoseconds)
                let result = try await api.githubLoginStatus(state: started.state)
                switch result.status {
                case "complete":
                    guard let newToken = result.token, !newToken.isEmpty else {
                        throw APIError.decoding(L("GitHub 登录成功，但后端没有返回 JWT"))
                    }
                    token = newToken
                    api.token = newToken
                    keychain.saveQuietly(newToken, for: KeychainStore.Keys.token)
                    if let identity = result.email ?? result.login, !identity.isEmpty {
                        settings.username = identity
                        settings.persistQuietly()
                    }
                    notice = L("已使用 GitHub 账号登录")
                    isLoggingIn = false
                    return
                case "error":
                    throw APIError.http(status: 401, message: result.error ?? L("GitHub 登录失败"))
                default:
                    continue
                }
            }
            throw APIError.transport(L("GitHub 登录已超时，请重试"))
        } catch {
            loginError = error.localizedDescription
        }

        isLoggingIn = false
    }

    func logout(message: String? = nil) {
        token = nil
        api.token = nil
        keychain.delete(KeychainStore.Keys.token)
        notice = message
    }

    /// 请求过程中发现 token 失效时调用
    func handleUnauthorized() {
        logout(message: L("登录状态已失效，请重新登录"))
    }
}
