import CryptoKit
import Foundation

/// 应用设置：非敏感项存 UserDefaults，密钥类存 Keychain
@MainActor
final class AppSettings: ObservableObject {
    enum DefaultsKey {
        static let backendBaseURL = "backendBaseURL"
        static let projectDirectory = "projectDirectory"
        static let pythonPath = "pythonPath"
        static let autoStartBackend = "autoStartBackend"
        static let writeDotEnvOnLaunch = "writeDotEnvOnLaunch"
        static let defaultProvider = "defaultProvider"
        static let username = "username"
        static let googleClientID = "googleClientID"
        static let googleAllowedEmails = "googleAllowedEmails"
        static let googleAllowedDomains = "googleAllowedDomains"
        static let githubClientID = "githubClientID"
        static let githubAllowedLogins = "githubAllowedLogins"
        static let githubAllowedEmails = "githubAllowedEmails"
        static let openaiBaseURL = "openaiBaseURL"
        static let openaiModel = "openaiModel"
        static let openaiAuthMode = "openaiAuthMode"
        static let azureTenantID = "azureTenantID"
        static let azureClientID = "azureClientID"
        static let azureSubscriptionID = "azureSubscriptionID"
        static let ndBaseURL = "ndBaseURL"
        static let ndUsername = "ndUsername"
        static let ndLoginDomain = "ndLoginDomain"
        static let ndVerifyTLS = "ndVerifyTLS"
        static let containerRegistry = "containerRegistryJSON"
        /// 当前后端进程启动时用的凭据指纹
        static let appliedCredentialsFingerprint = "appliedCredentialsFingerprint"
        static let fontScale = "ui.fontScale"
        static let language = "ui.language"
    }

    static let providerOptions = ["deepseek", "kimi", "openai"]

    /// ⌘+ / ⌘− 的缩放档位（值 = 相对系统标准字号的倍数）
    nonisolated static let fontScaleSteps: [Double] = [0.8, 0.9, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0]
    /// 默认偏大一档：macOS 默认字号（正文 13pt / 说明文字 10pt）偏小
    nonisolated static let defaultFontScale: Double = 1.15

    /// 往上一档 / 往下一档（挡位表里没有的数值取最接近的下一档），纯函数便于验证
    nonisolated static func steppedFontScale(from current: Double, direction: Int) -> Double {
        guard direction != 0 else { return current }
        if direction > 0 {
            return fontScaleSteps.first { $0 > current + 0.0001 } ?? fontScaleSteps.last ?? current
        }
        return fontScaleSteps.last { $0 < current - 0.0001 } ?? fontScaleSteps.first ?? current
    }

    @Published var backendBaseURL: String
    @Published var projectDirectory: String
    @Published var pythonPath: String
    @Published var autoStartBackend: Bool
    @Published var writeDotEnvOnLaunch: Bool
    @Published var defaultProvider: String
    @Published var username: String
    @Published var googleClientID: String
    @Published var googleAllowedEmails: String
    @Published var googleAllowedDomains: String
    @Published var githubClientID: String
    @Published var githubAllowedLogins: String
    @Published var githubAllowedEmails: String
    /// 界面字体缩放（1 = 系统标准）
    @Published var fontScale: Double {
        didSet {
            guard fontScale != oldValue else { return }
            defaults.set(fontScale, forKey: DefaultsKey.fontScale)
        }
    }

    /// 界面语言（中文原文写在源码里，查表换英文 / 挪威语）
    @Published var language: AppLanguage {
        didSet {
            guard language != oldValue else { return }
            defaults.set(language.rawValue, forKey: DefaultsKey.language)
            // 服务层 / ViewModel 里的文案是现算的，同步过去即可；视图靠环境里的 Localizer 重绘
            Localizer.current = Localizer(language: language)
        }
    }

    @Published var deepseekKey: String
    @Published var kimiKey: String
    @Published var openaiKey: String
    /// OpenAI / Azure OpenAI 兼容端点配置
    @Published var openaiBaseURL: String
    @Published var openaiModel: String
    /// auto（识别 Azure 域名自动带 api-key 头）/ bearer / api-key
    @Published var openaiAuthMode: String
    @Published var loginPassword: String

    // Azure / Meraki 工具凭据：Client Secret 与 API Key 进 Keychain，其余进 UserDefaults
    @Published var azureTenantID: String
    @Published var azureClientID: String
    @Published var azureClientSecret: String
    @Published var azureSubscriptionID: String
    @Published var merakiAPIKey: String

    // Cisco Nexus Dashboard 工具凭据（Infra API + Manage API）
    @Published var ndBaseURL: String
    @Published var ndUsername: String
    /// API Key 走 Keychain；为空则后端改用用户名密码登录换 token
    @Published var ndAPIKey: String
    @Published var ndPassword: String
    @Published var ndLoginDomain: String
    /// 是否校验 TLS 证书（Nexus Dashboard 多为自签证书，默认关闭）
    @Published var ndVerifyTLS: Bool

    // 多容器注册表（Serverless 容器：代码在 GitHub，跑在 Azure Container Apps）
    /// 注册表 JSON 文本（落盘到 App Support/containers.json，后端每次调用都重读 → 改完立即生效）
    @Published var containerRegistryJSON: String

    @Published var lastStatusMessage: String?

    private let defaults = UserDefaults.standard
    private let keychain = KeychainStore.shared

    init() {
        let detectedProject = Self.detectProjectDirectory()
        let projectPath = defaults.string(forKey: DefaultsKey.projectDirectory) ?? detectedProject
        // 页面上没存过的字段用项目里的 .env 预填，避免「.env 里明明配了，页面却是空的」
        let dotEnv = Self.loadDotEnv(projectDirectory: projectPath)

        backendBaseURL = defaults.string(forKey: DefaultsKey.backendBaseURL) ?? "http://127.0.0.1:8000"
        projectDirectory = projectPath
        pythonPath = defaults.string(forKey: DefaultsKey.pythonPath) ?? ""
        autoStartBackend = defaults.object(forKey: DefaultsKey.autoStartBackend) as? Bool ?? true
        writeDotEnvOnLaunch = defaults.object(forKey: DefaultsKey.writeDotEnvOnLaunch) as? Bool ?? false
        defaultProvider = defaults.string(forKey: DefaultsKey.defaultProvider) ?? "deepseek"
        username = defaults.string(forKey: DefaultsKey.username) ?? "admin"
        googleClientID = defaults.string(forKey: DefaultsKey.googleClientID)
            ?? dotEnv["GOOGLE_CLIENT_ID"] ?? ""
        googleAllowedEmails = defaults.string(forKey: DefaultsKey.googleAllowedEmails)
            ?? dotEnv["GOOGLE_ALLOWED_EMAILS"] ?? ""
        googleAllowedDomains = defaults.string(forKey: DefaultsKey.googleAllowedDomains)
            ?? dotEnv["GOOGLE_ALLOWED_DOMAINS"] ?? ""
        githubClientID = defaults.string(forKey: DefaultsKey.githubClientID)
            ?? dotEnv["GITHUB_CLIENT_ID"] ?? ""
        githubAllowedLogins = defaults.string(forKey: DefaultsKey.githubAllowedLogins)
            ?? dotEnv["GITHUB_ALLOWED_LOGINS"] ?? ""
        githubAllowedEmails = defaults.string(forKey: DefaultsKey.githubAllowedEmails)
            ?? dotEnv["GITHUB_ALLOWED_EMAILS"] ?? ""
        // 存过就沿用，顺手夹一下范围，避免外部写坏的值让界面没法用
        let storedFontScale = defaults.object(forKey: DefaultsKey.fontScale) as? Double
        fontScale = storedFontScale.map { min(max($0, 0.6), 2.5) } ?? Self.defaultFontScale
        // 没存过就是跟随系统（首次启动按系统语言挑一个）
        language = AppLanguage(rawValue: defaults.string(forKey: DefaultsKey.language) ?? "")
            ?? .system

        deepseekKey = keychain.read(KeychainStore.Keys.deepseekKey) ?? dotEnv["DEEPSEEK_API_KEY"] ?? ""
        kimiKey = keychain.read(KeychainStore.Keys.kimiKey) ?? dotEnv["KIMI_API_KEY"] ?? ""
        openaiKey = keychain.read(KeychainStore.Keys.openaiKey) ?? dotEnv["OPENAI_API_KEY"] ?? ""
        openaiBaseURL = defaults.string(forKey: DefaultsKey.openaiBaseURL)
            ?? dotEnv["OPENAI_BASE_URL"] ?? "https://api.openai.com/v1"
        openaiModel = defaults.string(forKey: DefaultsKey.openaiModel)
            ?? dotEnv["OPENAI_MODEL_NAME"] ?? "gpt-4o"
        openaiAuthMode = defaults.string(forKey: DefaultsKey.openaiAuthMode)
            ?? dotEnv["OPENAI_AUTH_MODE"] ?? "auto"
        loginPassword = keychain.read(KeychainStore.Keys.loginPassword) ?? ""

        azureTenantID = defaults.string(forKey: DefaultsKey.azureTenantID) ?? dotEnv["AZURE_TENANT_ID"] ?? ""
        azureClientID = defaults.string(forKey: DefaultsKey.azureClientID) ?? dotEnv["AZURE_CLIENT_ID"] ?? ""
        azureClientSecret = keychain.read(KeychainStore.Keys.azureClientSecret)
            ?? dotEnv["AZURE_CLIENT_SECRET"] ?? ""
        azureSubscriptionID = defaults.string(forKey: DefaultsKey.azureSubscriptionID)
            ?? dotEnv["AZURE_SUBSCRIPTION_ID"] ?? ""
        merakiAPIKey = keychain.read(KeychainStore.Keys.merakiAPIKey) ?? dotEnv["MERAKI_API_KEY"] ?? ""

        ndBaseURL = defaults.string(forKey: DefaultsKey.ndBaseURL)
            ?? dotEnv["ND_BASE_URL"] ?? ""
        ndUsername = defaults.string(forKey: DefaultsKey.ndUsername)
            ?? dotEnv["ND_USERNAME"] ?? ""
        ndAPIKey = keychain.read(KeychainStore.Keys.ndAPIKey) ?? dotEnv["ND_API_KEY"] ?? ""
        ndPassword = keychain.read(KeychainStore.Keys.ndPassword) ?? dotEnv["ND_PASSWORD"] ?? ""
        ndLoginDomain = defaults.string(forKey: DefaultsKey.ndLoginDomain)
            ?? dotEnv["ND_LOGIN_DOMAIN"] ?? "local"
        ndVerifyTLS = defaults.object(forKey: DefaultsKey.ndVerifyTLS) as? Bool
            ?? Self.boolFromEnv(dotEnv["ND_VERIFY_TLS"])

        // 注册表：优先用落盘文件（后端读的也是同一份）；没有落盘文件时用默认值/旧 .env 预填
        containerRegistryJSON = Self.loadContainerRegistry(dotEnv: dotEnv)

        if pythonPath.isEmpty {
            pythonPath = Self.detectPythonPath(projectDirectory: projectPath)
        }

        // 全部存好之后再同步语言（此时才能读 self.language）
        Localizer.current = Localizer(language: language)
    }

    // MARK: - 保存

    func persist() throws {
        defaults.set(backendBaseURL, forKey: DefaultsKey.backendBaseURL)
        defaults.set(projectDirectory, forKey: DefaultsKey.projectDirectory)
        defaults.set(pythonPath, forKey: DefaultsKey.pythonPath)
        defaults.set(autoStartBackend, forKey: DefaultsKey.autoStartBackend)
        defaults.set(writeDotEnvOnLaunch, forKey: DefaultsKey.writeDotEnvOnLaunch)
        defaults.set(defaultProvider, forKey: DefaultsKey.defaultProvider)
        defaults.set(username, forKey: DefaultsKey.username)
        defaults.set(googleClientID, forKey: DefaultsKey.googleClientID)
        defaults.set(googleAllowedEmails, forKey: DefaultsKey.googleAllowedEmails)
        defaults.set(googleAllowedDomains, forKey: DefaultsKey.googleAllowedDomains)
        defaults.set(githubClientID, forKey: DefaultsKey.githubClientID)
        defaults.set(githubAllowedLogins, forKey: DefaultsKey.githubAllowedLogins)
        defaults.set(githubAllowedEmails, forKey: DefaultsKey.githubAllowedEmails)
        defaults.set(openaiBaseURL, forKey: DefaultsKey.openaiBaseURL)
        defaults.set(openaiModel, forKey: DefaultsKey.openaiModel)
        defaults.set(openaiAuthMode, forKey: DefaultsKey.openaiAuthMode)
        defaults.set(fontScale, forKey: DefaultsKey.fontScale)
        defaults.set(language.rawValue, forKey: DefaultsKey.language)
        defaults.set(azureTenantID, forKey: DefaultsKey.azureTenantID)
        defaults.set(azureClientID, forKey: DefaultsKey.azureClientID)
        defaults.set(azureSubscriptionID, forKey: DefaultsKey.azureSubscriptionID)
        defaults.set(ndBaseURL, forKey: DefaultsKey.ndBaseURL)
        defaults.set(ndUsername, forKey: DefaultsKey.ndUsername)
        defaults.set(ndLoginDomain, forKey: DefaultsKey.ndLoginDomain)
        defaults.set(ndVerifyTLS, forKey: DefaultsKey.ndVerifyTLS)
        // 注册表落盘（后端每次调用都重读文件，所以改完不用重启后端）；
        // JSON 非法时抛错、保留旧文件，不阻断其它设置的保存
        try? saveContainerRegistry()

        try keychain.save(deepseekKey, for: KeychainStore.Keys.deepseekKey)
        try keychain.save(kimiKey, for: KeychainStore.Keys.kimiKey)
        try keychain.save(openaiKey, for: KeychainStore.Keys.openaiKey)
        try keychain.save(loginPassword, for: KeychainStore.Keys.loginPassword)
        try keychain.save(azureClientSecret, for: KeychainStore.Keys.azureClientSecret)
        try keychain.save(merakiAPIKey, for: KeychainStore.Keys.merakiAPIKey)
        try keychain.save(ndAPIKey, for: KeychainStore.Keys.ndAPIKey)
        try keychain.save(ndPassword, for: KeychainStore.Keys.ndPassword)
    }

    func persistQuietly() {
        do {
            try persist()
        } catch {
            lastStatusMessage = L("保存失败：{0}", error.localizedDescription)
        }
    }

    // MARK: - 传给 Python 后端

    var backendPort: Int? {
        URL(string: backendBaseURL)?.port
    }

    // MARK: - 界面字体大小（⌘+ / ⌘− / ⌘0）

    var fontScalePercent: Int {
        Int((fontScale * 100).rounded())
    }

    func increaseFontScale() {
        fontScale = Self.steppedFontScale(from: fontScale, direction: 1)
    }

    func decreaseFontScale() {
        fontScale = Self.steppedFontScale(from: fontScale, direction: -1)
    }

    func resetFontScale() {
        fontScale = Self.defaultFontScale
    }

    /// 启动子进程时注入的环境变量（优先级高于项目里的 .env）
    func environmentOverrides() -> [String: String] {
        var environment: [String: String] = [
            "DEFAULT_AI_PROVIDER": defaultProvider,
            "AI_MOCK_MODE": "auto",
        ]
        if !deepseekKey.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["DEEPSEEK_API_KEY"] = deepseekKey.trimmingCharacters(in: .whitespaces)
        }
        if !kimiKey.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["KIMI_API_KEY"] = kimiKey.trimmingCharacters(in: .whitespaces)
        }
        if !openaiKey.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["OPENAI_API_KEY"] = openaiKey.trimmingCharacters(in: .whitespaces)
        }
        if !openaiBaseURL.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["OPENAI_BASE_URL"] = openaiBaseURL.trimmingCharacters(in: .whitespaces)
        }
        if !openaiModel.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["OPENAI_MODEL_NAME"] = openaiModel.trimmingCharacters(in: .whitespaces)
        }
        if !openaiAuthMode.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["OPENAI_AUTH_MODE"] = openaiAuthMode.trimmingCharacters(in: .whitespaces)
        }
        if !googleClientID.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["GOOGLE_CLIENT_ID"] = googleClientID.trimmingCharacters(in: .whitespaces)
        }
        if !googleAllowedEmails.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["GOOGLE_ALLOWED_EMAILS"] = googleAllowedEmails.trimmingCharacters(in: .whitespaces)
        }
        if !googleAllowedDomains.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["GOOGLE_ALLOWED_DOMAINS"] = googleAllowedDomains.trimmingCharacters(in: .whitespaces)
        }
        if !githubClientID.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["GITHUB_CLIENT_ID"] = githubClientID.trimmingCharacters(in: .whitespaces)
        }
        if !githubAllowedLogins.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["GITHUB_ALLOWED_LOGINS"] = githubAllowedLogins.trimmingCharacters(in: .whitespaces)
        }
        if !githubAllowedEmails.trimmingCharacters(in: .whitespaces).isEmpty {
            environment["GITHUB_ALLOWED_EMAILS"] = githubAllowedEmails.trimmingCharacters(in: .whitespaces)
        }
        for (key, value) in credentialEnvironment() {
            environment[key] = value
        }
        if let port = backendPort {
            environment["PORT"] = String(port)
        }
        return environment
    }

    /// Azure / Meraki / Nexus Dashboard 凭据对应的环境变量（空值不注入，交给 .env 或环境本身）
    func credentialEnvironment() -> [String: String] {
        var candidates = [
            "AZURE_TENANT_ID": azureTenantID,
            "AZURE_CLIENT_ID": azureClientID,
            "AZURE_CLIENT_SECRET": azureClientSecret,
            "AZURE_SUBSCRIPTION_ID": azureSubscriptionID,
            "MERAKI_API_KEY": merakiAPIKey,
            // 注册表文件路径显式下发给后端，避免 App 与后端各自猜路径
            "AICHAT_CONTAINERS_FILE": Self.containerRegistryPath(),
            "ND_BASE_URL": ndBaseURL,
            "ND_USERNAME": ndUsername,
            "ND_API_KEY": ndAPIKey,
            "ND_PASSWORD": ndPassword,
            "ND_LOGIN_DOMAIN": ndLoginDomain,
        ]
        // 布尔值单独处理：关闭时也要显式下发 false，否则后端会沿用 .env 里的旧值
        candidates["ND_VERIFY_TLS"] = ndVerifyTLS ? "true" : "false"

        var result: [String: String] = [:]
        for (key, value) in candidates {
            let trimmed = value.trimmingCharacters(in: .whitespaces)
            if !trimmed.isEmpty {
                result[key] = trimmed
            }
        }
        return result
    }

    // MARK: - 多容器注册表（Serverless 容器）

    enum ContainerRegistryError: LocalizedError {
        case invalidJSON

        var errorDescription: String? {
            L("注册表不是合法的 JSON，未保存（请检查引号/逗号）")
        }
    }

    /// 注册表文件路径：必须与后端 tools/container_tools.py 的 registry_path() 默认值一致
    nonisolated static func containerRegistryPath() -> String {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Application Support")
        return base.appendingPathComponent("AIChatApp/containers.json").path
    }

    /// 首次使用时的模板：.env 里若还有旧版单容器配置，顺手迁移成一条注册表条目
    nonisolated static func defaultContainerRegistryJSON(dotEnv: [String: String] = [:]) -> String {
        var containers: [[String: Any]] = []

        let legacyURL = (dotEnv["ANSIBLE_EXECUTOR_URL"] ?? "").trimmingCharacters(in: .whitespaces)
        if !legacyURL.isEmpty {
            var entry: [String: Any] = [
                "name": (dotEnv["ANSIBLE_EXECUTOR_NAME"].flatMap { $0.isEmpty ? nil : $0 }) ?? "ansible",
                "url": legacyURL,
                "description": "执行 Ansible playbook 的容器（Azure Container Apps）",
            ]
            // 密钥继续放在环境变量里（不落到注册表文件）
            entry["api_key_env"] = "ANSIBLE_EXECUTOR_API_KEY"
            let allowed = (dotEnv["ANSIBLE_ALLOWED_TASKS"] ?? "").trimmingCharacters(in: .whitespaces)
            if !allowed.isEmpty && allowed != "*" {
                entry["allowed_tasks"] = allowed
            }
            containers.append(entry)
        }

        let payload: [String: Any] = ["containers": containers]
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: payload,
                options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            ),
            let text = String(data: data, encoding: .utf8)
        else {
            return "{\n  \"containers\": []\n}"
        }
        return text
    }

    /// 读注册表：落盘文件 > UserDefaults 草稿 > 由旧 .env 生成的模板
    nonisolated static func loadContainerRegistry(dotEnv: [String: String]) -> String {
        if let existing = try? String(contentsOfFile: containerRegistryPath(), encoding: .utf8),
           !existing.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return existing
        }
        if let draft = UserDefaults.standard.string(forKey: DefaultsKey.containerRegistry),
           !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return draft
        }
        return defaultContainerRegistryJSON(dotEnv: dotEnv)
    }

    /// 校验 + 落盘（JSON 非法时抛错，旧文件保持不动）
    func saveContainerRegistry() throws {
        let text = containerRegistryJSON.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty {
            guard
                let data = text.data(using: .utf8),
                (try? JSONSerialization.jsonObject(with: data, options: [])) != nil
            else {
                throw ContainerRegistryError.invalidJSON
            }
        }

        let url = URL(fileURLWithPath: Self.containerRegistryPath())
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try text.write(to: url, atomically: true, encoding: .utf8)
        // 注册表里可能直接写了 api_key，收紧权限到仅本人可读写
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        defaults.set(containerRegistryJSON, forKey: DefaultsKey.containerRegistry)
    }

    /// 写入/合并项目根目录的 .env，保留文件里其它配置行
    @discardableResult
    func writeDotEnv() throws -> URL {
        let directory = URL(fileURLWithPath: projectDirectory, isDirectory: true)
        let fileURL = directory.appendingPathComponent(".env")

        var updates: [String: String] = ["DEFAULT_AI_PROVIDER": defaultProvider]
        let trimmedDeepseek = deepseekKey.trimmingCharacters(in: .whitespaces)
        let trimmedKimi = kimiKey.trimmingCharacters(in: .whitespaces)
        if !trimmedDeepseek.isEmpty { updates["DEEPSEEK_API_KEY"] = trimmedDeepseek }
        if !trimmedKimi.isEmpty { updates["KIMI_API_KEY"] = trimmedKimi }
        let trimmedOpenAI = openaiKey.trimmingCharacters(in: .whitespaces)
        if !trimmedOpenAI.isEmpty { updates["OPENAI_API_KEY"] = trimmedOpenAI }
        let trimmedBase = openaiBaseURL.trimmingCharacters(in: .whitespaces)
        let trimmedModel = openaiModel.trimmingCharacters(in: .whitespaces)
        if !trimmedBase.isEmpty { updates["OPENAI_BASE_URL"] = trimmedBase }
        if !trimmedModel.isEmpty { updates["OPENAI_MODEL_NAME"] = trimmedModel }
        if !openaiAuthMode.trimmingCharacters(in: .whitespaces).isEmpty {
            updates["OPENAI_AUTH_MODE"] = openaiAuthMode
        }
        let trimmedGoogleClientID = googleClientID.trimmingCharacters(in: .whitespaces)
        let trimmedGoogleEmails = googleAllowedEmails.trimmingCharacters(in: .whitespaces)
        let trimmedGoogleDomains = googleAllowedDomains.trimmingCharacters(in: .whitespaces)
        if !trimmedGoogleClientID.isEmpty { updates["GOOGLE_CLIENT_ID"] = trimmedGoogleClientID }
        if !trimmedGoogleEmails.isEmpty { updates["GOOGLE_ALLOWED_EMAILS"] = trimmedGoogleEmails }
        if !trimmedGoogleDomains.isEmpty { updates["GOOGLE_ALLOWED_DOMAINS"] = trimmedGoogleDomains }
        let trimmedGitHubClientID = githubClientID.trimmingCharacters(in: .whitespaces)
        let trimmedGitHubLogins = githubAllowedLogins.trimmingCharacters(in: .whitespaces)
        let trimmedGitHubEmails = githubAllowedEmails.trimmingCharacters(in: .whitespaces)
        if !trimmedGitHubClientID.isEmpty { updates["GITHUB_CLIENT_ID"] = trimmedGitHubClientID }
        if !trimmedGitHubLogins.isEmpty { updates["GITHUB_ALLOWED_LOGINS"] = trimmedGitHubLogins }
        if !trimmedGitHubEmails.isEmpty { updates["GITHUB_ALLOWED_EMAILS"] = trimmedGitHubEmails }
        for (key, value) in credentialEnvironment() {
            updates[key] = value
        }
        if let port = backendPort { updates["PORT"] = String(port) }

        var lines: [String] = []
        var pending = updates

        if let existing = try? String(contentsOf: fileURL, encoding: .utf8), !existing.isEmpty {
            for raw in existing.split(separator: "\n", omittingEmptySubsequences: false) {
                let line = String(raw)
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                if !trimmed.hasPrefix("#"), let separator = trimmed.firstIndex(of: "=") {
                    let key = String(trimmed[trimmed.startIndex..<separator]).trimmingCharacters(in: .whitespaces)
                    if let value = pending.removeValue(forKey: key) {
                        lines.append("\(key)=\(value)")
                        continue
                    }
                }
                lines.append(line)
            }
        } else {
            lines.append("# 由 AIChatApp 的设置页生成")
        }

        for (key, value) in pending.sorted(by: { $0.key < $1.key }) {
            lines.append("\(key)=\(value)")
        }

        let text = lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines) + "\n"
        try text.write(to: fileURL, atomically: true, encoding: .utf8)
        return fileURL
    }

    // MARK: - 自动探测

    /// 读项目根目录的 .env（只用于预填页面；文件里的其它行原样保留）
    static func loadDotEnv(projectDirectory: String) -> [String: String] {
        let fileURL = URL(fileURLWithPath: projectDirectory, isDirectory: true)
            .appendingPathComponent(".env")
        guard let text = try? String(contentsOf: fileURL, encoding: .utf8) else { return [:] }

        var values: [String: String] = [:]
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else { continue }

            let key = String(line[line.startIndex..<separator]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: separator)...]).trimmingCharacters(in: .whitespaces)
            if value.count >= 2, value.hasPrefix("\""), value.hasSuffix("\"") {
                value = String(value.dropFirst().dropLast())
            }
            if !key.isEmpty, !value.isEmpty {
                values[key] = value
            }
        }
        return values
    }

    /// .env 里的布尔值解析（1 / true / yes / on 视为 true）
    static func boolFromEnv(_ raw: String?) -> Bool {
        guard let value = raw?.trimmingCharacters(in: .whitespaces).lowercased() else { return false }
        return ["1", "true", "yes", "on"].contains(value)
    }

    // MARK: - 凭据是否已经生效

    /// 所有凭据的指纹（只存哈希，不落明文）
    var credentialsFingerprint: String {
        let parts = [
            azureTenantID,
            azureClientID,
            azureClientSecret,
            azureSubscriptionID,
            merakiAPIKey,
            ndBaseURL,
            ndUsername,
            ndAPIKey,
            ndPassword,
            ndLoginDomain,
            ndVerifyTLS ? "tls-verify-on" : "tls-verify-off",
            // 容器注册表是热加载的（后端每次调用都重读文件），故意不进指纹：
            // 否则改一行注册表就会让所有其它凭据都变成「需要重启后端」。
            deepseekKey,
            kimiKey,
            openaiKey,
            openaiBaseURL,
            openaiModel,
            openaiAuthMode,
            googleClientID,
            googleAllowedEmails,
            googleAllowedDomains,
            githubClientID,
            githubAllowedLogins,
            githubAllowedEmails,
        ].map { $0.trimmingCharacters(in: .whitespaces) }

        let digest = SHA256.hash(data: Data(parts.joined(separator: "\u{1}").utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    /// 当前后端进程启动时用的凭据指纹；nil 表示后端不是本应用启动的（未知）
    var appliedCredentialsFingerprint: String? {
        defaults.string(forKey: DefaultsKey.appliedCredentialsFingerprint)
    }

    /// 页面上的凭据是否已经在运行中的后端进程里
    var credentialsAreApplied: Bool {
        appliedCredentialsFingerprint == credentialsFingerprint
    }

    /// 后端进程带着当前凭据启动成功时调用
    func markCredentialsApplied() {
        defaults.set(credentialsFingerprint, forKey: DefaultsKey.appliedCredentialsFingerprint)
    }

    static func displayName(for provider: String) -> String {
        switch provider.lowercased() {
        case "deepseek": return "DeepSeek"
        case "kimi": return "Kimi"
        case "openai": return "OpenAI"
        default: return provider.capitalized
        }
    }

    /// 优先用本文件的编译期路径反推项目根目录（本地开发时最准）
    static func detectProjectDirectory() -> String {
        let fileManager = FileManager.default
        var candidates: [String] = []

        // #filePath = <项目根>/AIChatApp/Sources/Services/AppSettings.swift
        let sourceURL = URL(fileURLWithPath: #filePath)
        candidates.append(
            sourceURL
                .deletingLastPathComponent() // Services
                .deletingLastPathComponent() // Sources
                .deletingLastPathComponent() // AIChatApp
                .deletingLastPathComponent() // 项目根
                .path
        )

        candidates.append(fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Documents/MyMacApp").path)

        for path in candidates where fileManager.fileExists(atPath: path + "/main.py") {
            return path
        }
        return candidates.first ?? fileManager.homeDirectoryForCurrentUser.path
    }

    static func detectPythonPath(projectDirectory: String) -> String {
        let fileManager = FileManager.default
        let candidates = [
            projectDirectory + "/.venv/bin/python",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        for path in candidates where fileManager.isExecutableFile(atPath: path) {
            return path
        }
        return "/usr/bin/python3"
    }
}
