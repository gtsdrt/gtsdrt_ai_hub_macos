import CryptoKit
import Foundation
import Security

/// 应用设置：非敏感项存 UserDefaults，密钥类存 Keychain
@MainActor
final class AppSettings: ObservableObject {
    enum DefaultsKey {
        static let backendBaseURL = "backendBaseURL"
        static let projectDirectory = "projectDirectory"
        static let pythonPath = "pythonPath"
        static let autoStartBackend = "autoStartBackend"
        static let defaultProvider = "defaultProvider"
        static let username = "username"
        static let backendAdminUsername = "backendAdminUsername"
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
        /// 仅存环境变量名（不是凭据值），用于重启时从 Keychain 注入旧版自定义密钥。
        static let keychainEnvironmentSecretNames = "keychainEnvironmentSecretNames"
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
    @Published var defaultProvider: String
    /// 登录页预填用户名；和后端实际接受的管理员用户名分开保存
    @Published var username: String
    @Published var backendAdminUsername: String
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
    /// 本地后端管理员口令（与“记住登录密码”区分）
    @Published var backendAdminPassword: String

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
    /// 注册表 JSON 文本（落盘到 App Support/containers.json；仅非敏感元数据热加载）
    @Published var containerRegistryJSON: String

    @Published var lastStatusMessage: String?

    private let defaults = UserDefaults.standard
    private let keychain = KeychainStore.shared

    init() {
        let detectedProject = Self.detectProjectDirectory()
        let projectPath = defaults.string(forKey: DefaultsKey.projectDirectory) ?? detectedProject
        // 首次升级时迁移 .env 凭据到 Keychain；只有迁移成功才会删除文件中的秘密行。
        let envMigration = Self.migrateDotEnvSecrets(
            projectDirectory: projectPath,
            keychain: keychain
        )
        let dotEnv = Self.loadDotEnv(projectDirectory: projectPath)

        backendBaseURL = defaults.string(forKey: DefaultsKey.backendBaseURL) ?? "http://127.0.0.1:8000"
        projectDirectory = projectPath
        pythonPath = defaults.string(forKey: DefaultsKey.pythonPath) ?? ""
        autoStartBackend = defaults.object(forKey: DefaultsKey.autoStartBackend) as? Bool ?? true
        defaultProvider = defaults.string(forKey: DefaultsKey.defaultProvider) ?? "deepseek"
        username = defaults.string(forKey: DefaultsKey.username) ?? "admin"
        backendAdminUsername = defaults.string(forKey: DefaultsKey.backendAdminUsername)
            ?? dotEnv["ADMIN_USERNAME"] ?? "admin"
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

        deepseekKey = keychain.read(KeychainStore.Keys.deepseekKey) ?? ""
        kimiKey = keychain.read(KeychainStore.Keys.kimiKey) ?? ""
        openaiKey = keychain.read(KeychainStore.Keys.openaiKey) ?? ""
        openaiBaseURL = defaults.string(forKey: DefaultsKey.openaiBaseURL)
            ?? dotEnv["OPENAI_BASE_URL"] ?? "https://api.openai.com/v1"
        openaiModel = defaults.string(forKey: DefaultsKey.openaiModel)
            ?? dotEnv["OPENAI_MODEL_NAME"] ?? "gpt-4o"
        openaiAuthMode = defaults.string(forKey: DefaultsKey.openaiAuthMode)
            ?? dotEnv["OPENAI_AUTH_MODE"] ?? "auto"
        let storedAdminPassword = keychain.read(KeychainStore.Keys.adminPassword) ?? ""
        backendAdminPassword = storedAdminPassword
        loginPassword = keychain.read(KeychainStore.Keys.loginPassword)
            ?? storedAdminPassword

        azureTenantID = defaults.string(forKey: DefaultsKey.azureTenantID) ?? dotEnv["AZURE_TENANT_ID"] ?? ""
        azureClientID = defaults.string(forKey: DefaultsKey.azureClientID) ?? dotEnv["AZURE_CLIENT_ID"] ?? ""
        azureClientSecret = keychain.read(KeychainStore.Keys.azureClientSecret) ?? ""
        azureSubscriptionID = defaults.string(forKey: DefaultsKey.azureSubscriptionID)
            ?? dotEnv["AZURE_SUBSCRIPTION_ID"] ?? ""
        merakiAPIKey = keychain.read(KeychainStore.Keys.merakiAPIKey) ?? ""

        ndBaseURL = defaults.string(forKey: DefaultsKey.ndBaseURL)
            ?? dotEnv["ND_BASE_URL"] ?? ""
        ndUsername = defaults.string(forKey: DefaultsKey.ndUsername)
            ?? dotEnv["ND_USERNAME"] ?? ""
        ndAPIKey = keychain.read(KeychainStore.Keys.ndAPIKey) ?? ""
        ndPassword = keychain.read(KeychainStore.Keys.ndPassword) ?? ""
        ndLoginDomain = defaults.string(forKey: DefaultsKey.ndLoginDomain)
            ?? dotEnv["ND_LOGIN_DOMAIN"] ?? "local"
        ndVerifyTLS = defaults.object(forKey: DefaultsKey.ndVerifyTLS) as? Bool
            ?? Self.boolFromEnv(dotEnv["ND_VERIFY_TLS"])

        // 注册表：优先用落盘文件（后端读的也是同一份）；没有落盘文件时用默认值/旧 .env 预填
        let rawContainerRegistry = Self.loadContainerRegistry(dotEnv: dotEnv)
        var containerMigrationError: Error?
        do {
            containerRegistryJSON = try Self.migrateContainerRegistrySecrets(
                rawContainerRegistry,
                keychain: keychain
            )
        } catch {
            containerRegistryJSON = rawContainerRegistry
            containerMigrationError = error
        }

        if pythonPath.isEmpty {
            pythonPath = Self.detectPythonPath(projectDirectory: projectPath)
        }

        // 全部存好之后再同步语言（此时才能读 self.language）
        Localizer.current = Localizer(language: language)
        if let error = envMigration.error {
            lastStatusMessage = L("凭据迁移到 Keychain 失败；.env 未清理：{0}", error.localizedDescription)
        } else if let error = containerMigrationError {
            lastStatusMessage = L("容器密钥迁移到 Keychain 失败：{0}", error.localizedDescription)
        } else if envMigration.migratedSecrets {
            lastStatusMessage = L("旧 .env 凭据已迁移并验证保存在 Keychain")
        }
    }

    /// Keep the observed object identity while refreshing restored preferences and credentials.
    func reloadAfterBackupRestore() {
        let restored = AppSettings()
        backendBaseURL = restored.backendBaseURL
        projectDirectory = restored.projectDirectory
        pythonPath = restored.pythonPath
        autoStartBackend = restored.autoStartBackend
        defaultProvider = restored.defaultProvider
        username = restored.username
        backendAdminUsername = restored.backendAdminUsername
        googleClientID = restored.googleClientID
        googleAllowedEmails = restored.googleAllowedEmails
        googleAllowedDomains = restored.googleAllowedDomains
        githubClientID = restored.githubClientID
        githubAllowedLogins = restored.githubAllowedLogins
        githubAllowedEmails = restored.githubAllowedEmails
        fontScale = restored.fontScale
        language = restored.language
        deepseekKey = restored.deepseekKey
        kimiKey = restored.kimiKey
        openaiKey = restored.openaiKey
        openaiBaseURL = restored.openaiBaseURL
        openaiModel = restored.openaiModel
        openaiAuthMode = restored.openaiAuthMode
        loginPassword = restored.loginPassword
        backendAdminPassword = restored.backendAdminPassword
        azureTenantID = restored.azureTenantID
        azureClientID = restored.azureClientID
        azureClientSecret = restored.azureClientSecret
        azureSubscriptionID = restored.azureSubscriptionID
        merakiAPIKey = restored.merakiAPIKey
        ndBaseURL = restored.ndBaseURL
        ndUsername = restored.ndUsername
        ndAPIKey = restored.ndAPIKey
        ndPassword = restored.ndPassword
        ndLoginDomain = restored.ndLoginDomain
        ndVerifyTLS = restored.ndVerifyTLS
        containerRegistryJSON = restored.containerRegistryJSON
    }

    // MARK: - 保存

    func persist() throws {
        defaults.set(backendBaseURL, forKey: DefaultsKey.backendBaseURL)
        defaults.set(projectDirectory, forKey: DefaultsKey.projectDirectory)
        defaults.set(pythonPath, forKey: DefaultsKey.pythonPath)
        defaults.set(autoStartBackend, forKey: DefaultsKey.autoStartBackend)
        defaults.set(defaultProvider, forKey: DefaultsKey.defaultProvider)
        defaults.set(username, forKey: DefaultsKey.username)
        defaults.set(backendAdminUsername, forKey: DefaultsKey.backendAdminUsername)
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
        // 注册表仅保存非敏感元数据，后端每次调用重读；
        // JSON 非法时抛错、保留旧文件，不阻断其它设置的保存
        try? saveContainerRegistry()

        try keychain.save(deepseekKey, for: KeychainStore.Keys.deepseekKey)
        try keychain.save(kimiKey, for: KeychainStore.Keys.kimiKey)
        try keychain.save(openaiKey, for: KeychainStore.Keys.openaiKey)
        try keychain.save(loginPassword, for: KeychainStore.Keys.loginPassword)
        try keychain.save(backendAdminPassword, for: KeychainStore.Keys.adminPassword)
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
            // App 管理的后端固定只监听本机；不要继承旧 .env 中的 0.0.0.0。
            "HOST": "127.0.0.1",
            "ADMIN_USERNAME": backendAdminUsername.trimmingCharacters(in: .whitespaces),
            "AICHAT_ENV_FILE": URL(fileURLWithPath: projectDirectory, isDirectory: true)
                .appendingPathComponent(".env").path,
        ]
        let migratedSecretNames = defaults.stringArray(
            forKey: DefaultsKey.keychainEnvironmentSecretNames
        ) ?? []
        for name in Set(Self.keychainEnvironmentSecrets + migratedSecretNames) {
            // 清掉父进程继承的旧值，再从 Keychain 注入；空值可阻止 dotenv 回退。
            environment[name] = keychain.read(Self.keychainAccount(forEnvironmentName: name)) ?? ""
        }
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
        for (key, value) in containerKeyEnvironment() {
            environment[key] = value
        }
        if let port = backendPort {
            environment["PORT"] = String(port)
        }
        return environment
    }

    /// 每个容器一把密钥：Keychain → `AICHAT_CONTAINER_KEY_<容器名>` 环境变量。
    /// 注册表里没写 `api_key` 时后端会用它（密钥不落盘）；写了 `api_key` 则以注册表为准。
    func containerKeyEnvironment() -> [String: String] {
        var result: [String: String] = [:]
        for name in Self.containerNames(in: containerRegistryJSON) {
            let key = keychain.read(KeychainStore.Keys.containerAPIKey(name)) ?? ""
            guard !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            let variable = "AICHAT_CONTAINER_KEY_"
                + name.replacingOccurrences(
                    of: "[^A-Za-z0-9]+", with: "_", options: .regularExpression
                ).uppercased()
            result[variable] = key
        }
        return result
    }

    /// 设置页中的 Azure / Meraki / Nexus Dashboard 值；敏感值的权威来源为 Keychain。
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
        case unknownContainer(String)

        var errorDescription: String? {
            switch self {
            case .invalidJSON:
                return L("注册表不是合法的 JSON，未保存（请检查引号/逗号）")
            case .unknownContainer(let name):
                return L("注册表里找不到名为 {0} 的容器，密钥只存进了 Keychain", name)
            }
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
        // 即便用户直接编辑 JSON 放入 api_key，也先迁到 Keychain 并清理明文字段。
        let text = try Self.migrateContainerRegistrySecrets(
            containerRegistryJSON.trimmingCharacters(in: .whitespacesAndNewlines),
            keychain: keychain
        )
        containerRegistryJSON = text
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
        // 收紧权限到仅本人可读写
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        defaults.set(containerRegistryJSON, forKey: DefaultsKey.containerRegistry)
    }

    // MARK: - 每个容器的 API Key（界面输入 → Keychain）

    /// 界面上的密钥草稿（不进 credentialsFingerprint，避免误触发「需要重启」）
    @Published var containerKeyDrafts: [String: String] = [:]

    /// 注册表里所有容器名（按 JSON 里的顺序；JSON 非法时返回空数组）
    nonisolated static func containerNames(in registryJSON: String) -> [String] {
        guard
            let data = registryJSON.data(using: .utf8),
            let root = try? JSONSerialization.jsonObject(with: data, options: []),
            let containers = containerArray(from: root)
        else { return [] }

        return containers.compactMap { item in
            guard let name = (item["name"] as? String)?.trimmingCharacters(in: .whitespaces), !name.isEmpty
            else { return nil }
            return name
        }
    }

    private nonisolated static func containerArray(from root: Any) -> [[String: Any]]? {
        if let dict = root as? [String: Any] {
            return (dict["containers"] as? [[String: Any]]) ?? (dict["endpoints"] as? [[String: Any]])
        }
        return root as? [[String: Any]]
    }

    /// 用 Keychain 里存的密钥预填界面草稿（只填没填过的，不覆盖用户正在输入的内容）
    func loadContainerKeyDrafts() {
        for name in Self.containerNames(in: containerRegistryJSON) {
            let stored = keychain.read(KeychainStore.Keys.containerAPIKey(name)) ?? ""
            if containerKeyDrafts[name] == nil {
                containerKeyDrafts[name] = stored
            }
        }
    }

    /// 容器密钥只保存到 Keychain；启动后端时注入环境变量，因此改密钥后需要重启后端。
    func saveContainerAPIKey(_ rawKey: String, for container: String) throws {
        let key = rawKey.trimmingCharacters(in: .whitespacesAndNewlines)

        if key.isEmpty {
            keychain.delete(KeychainStore.Keys.containerAPIKey(container))
        } else {
            try keychain.save(key, for: KeychainStore.Keys.containerAPIKey(container))
        }
        containerKeyDrafts[container] = key

        // 确保旧注册表里的 api_key 也不会继续留在明文 JSON 中。
        guard
            let data = containerRegistryJSON.data(using: .utf8),
            let root = try? JSONSerialization.jsonObject(with: data, options: []),
            var containers = Self.containerArray(from: root)
        else {
            throw ContainerRegistryError.invalidJSON
        }

        var updated = false
        for index in containers.indices {
            guard (containers[index]["name"] as? String)?.trimmingCharacters(in: .whitespaces) == container
            else { continue }
            containers[index].removeValue(forKey: "api_key")
            if !key.isEmpty {
                // 显式设置的 Keychain 密钥优先使用自动注入变量，避免旧 api_key_env 覆盖它。
                containers[index].removeValue(forKey: "api_key_env")
            }
            updated = true
        }
        guard updated else {
            throw ContainerRegistryError.unknownContainer(container)
        }

        let payload: Any = (root is [[String: Any]]) ? containers : ["containers": containers]
        let pretty = try JSONSerialization.data(
            withJSONObject: payload,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        )
        containerRegistryJSON = String(data: pretty, encoding: .utf8) ?? containerRegistryJSON
        try saveContainerRegistry()
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

    /// .env 中需要迁移到 Keychain 的变量名。匹配规则也覆盖后续新增的 *_KEY / *_SECRET 等凭据。
    private static let keychainEnvironmentSecrets = [
        "DEEPSEEK_API_KEY",
        "KIMI_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_CLIENT_SECRET",
        "MERAKI_API_KEY",
        "ND_API_KEY",
        "ND_PASSWORD",
        "ADMIN_PASSWORD",
        "JWT_SECRET",
        "ANSIBLE_EXECUTOR_API_KEY",
        "GOOGLE_CLIENT_SECRET",
        "CONTAINER_ENDPOINTS_JSON",
    ]

    private static func isSecretEnvironmentName(_ rawName: String) -> Bool {
        let name = rawName.uppercased()
        return name == "JWT_SECRET"
            || name == "CONTAINER_ENDPOINTS_JSON"
            || name.contains("SECRET")
            || name.contains("PASSWORD")
            || name.contains("CREDENTIAL")
            || name.contains("PRIVATE_KEY")
            || name.contains("TOKEN")
            || name.hasSuffix("_KEY")
            || name.hasSuffix("_CONNECTION_STRING")
    }

    /// 保留现有 UI 使用的 Keychain account 名称；其它凭据按环境变量名独立存储。
    private static func keychainAccount(forEnvironmentName rawName: String) -> String {
        switch rawName.uppercased() {
        case "DEEPSEEK_API_KEY": return KeychainStore.Keys.deepseekKey
        case "KIMI_API_KEY": return KeychainStore.Keys.kimiKey
        case "OPENAI_API_KEY": return KeychainStore.Keys.openaiKey
        case "AZURE_CLIENT_SECRET": return KeychainStore.Keys.azureClientSecret
        case "MERAKI_API_KEY": return KeychainStore.Keys.merakiAPIKey
        case "ND_API_KEY": return KeychainStore.Keys.ndAPIKey
        case "ND_PASSWORD": return KeychainStore.Keys.ndPassword
        case "ADMIN_PASSWORD": return KeychainStore.Keys.adminPassword
        case "JWT_SECRET": return KeychainStore.Keys.jwtSecret
        case "ANSIBLE_EXECUTOR_API_KEY": return KeychainStore.Keys.ansibleExecutorAPIKey
        case "GOOGLE_CLIENT_SECRET": return KeychainStore.Keys.googleClientSecret
        default: return KeychainStore.Keys.environmentSecret(rawName)
        }
    }

    private struct DotEnvSecretMigrationResult {
        let migratedSecrets: Bool
        let error: Error?
    }

    /// 从 .env 导入秘密到 Keychain。已有 Keychain 值优先，不会被旧文件覆盖；
    /// 只有确认每个凭据都能从 Keychain 读回后才清理 .env。
    private static func migrateDotEnvSecrets(
        projectDirectory: String,
        keychain: KeychainStore
    ) -> DotEnvSecretMigrationResult {
        let fileURL = URL(fileURLWithPath: projectDirectory, isDirectory: true)
            .appendingPathComponent(".env")
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            return DotEnvSecretMigrationResult(migratedSecrets: false, error: nil)
        }

        do {
            let text = try String(contentsOf: fileURL, encoding: .utf8)
            var environmentValues: [String: String] = [:]
            for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard !line.isEmpty, !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else {
                    continue
                }
                let name = String(line[..<separator]).trimmingCharacters(in: .whitespaces)
                guard isSecretEnvironmentName(name) else { continue }
                var value = String(line[line.index(after: separator)...]).trimmingCharacters(in: .whitespaces)
                if value.count >= 2, value.hasPrefix("\""), value.hasSuffix("\"") {
                    value = String(value.dropFirst().dropLast())
                } else if value.count >= 2, value.hasPrefix("'"), value.hasSuffix("'") {
                    value = String(value.dropFirst().dropLast())
                }
                environmentValues[name] = value
            }

            for (name, value) in environmentValues where !value.isEmpty {
                let account = keychainAccount(forEnvironmentName: name)
                if let existing = keychain.read(account), !existing.isEmpty {
                    continue
                }
                try keychain.save(value, for: account)
                guard keychain.read(account) == value else {
                    throw KeychainStore.KeychainError.unexpectedStatus(errSecVerifyFailed)
                }
            }
            let migratedNames = (UserDefaults.standard.stringArray(
                forKey: DefaultsKey.keychainEnvironmentSecretNames
            ) ?? []) + Array(environmentValues.keys)
            UserDefaults.standard.set(
                Array(Set(migratedNames)).sorted(),
                forKey: DefaultsKey.keychainEnvironmentSecretNames
            )

            let lines = text.components(separatedBy: .newlines)
            let cleaned = lines.filter { raw in
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard !line.isEmpty, !line.hasPrefix("#"), let separator = line.firstIndex(of: "=") else {
                    return true
                }
                let name = String(line[..<separator]).trimmingCharacters(in: .whitespaces)
                guard isSecretEnvironmentName(name) else { return true }
                // 空的秘密项也移除；非空值必须已由 Keychain 确认保存。
                let account = keychainAccount(forEnvironmentName: name)
                return !environmentValues[name, default: ""].isEmpty
                    && (keychain.read(account) ?? "").isEmpty
            }.joined(separator: "\n")
            if cleaned != text {
                try cleaned.write(to: fileURL, atomically: true, encoding: .utf8)
                try FileManager.default.setAttributes(
                    [.posixPermissions: 0o600],
                    ofItemAtPath: fileURL.path
                )
            }
            return DotEnvSecretMigrationResult(
                migratedSecrets: environmentValues.values.contains { !$0.isEmpty },
                error: nil
            )
        } catch {
            return DotEnvSecretMigrationResult(migratedSecrets: false, error: error)
        }
    }

    /// 旧版容器注册表曾将 api_key 明文写入 JSON；先存入 Keychain 并验证，再去掉字段。
    private static func migrateContainerRegistrySecrets(
        _ rawJSON: String,
        keychain: KeychainStore
    ) throws -> String {
        guard
            let data = rawJSON.data(using: .utf8),
            let root = try? JSONSerialization.jsonObject(with: data, options: []),
            var containers = containerArray(from: root)
        else { return rawJSON }

        var changed = false
        for index in containers.indices {
            guard let rawKey = containers[index]["api_key"] as? String else { continue }
            guard let name = (containers[index]["name"] as? String)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
                  !name.isEmpty
            else { throw ContainerRegistryError.invalidJSON }

            let account = KeychainStore.Keys.containerAPIKey(name)
            if !rawKey.isEmpty, (keychain.read(account) ?? "").isEmpty {
                try keychain.save(rawKey, for: account)
                guard keychain.read(account) == rawKey else {
                    throw KeychainStore.KeychainError.unexpectedStatus(errSecVerifyFailed)
                }
            }
            // 有旧 Keychain 值时以它为准；不论值是否为空，都不再将 key 留在 JSON。
            containers[index].removeValue(forKey: "api_key")
            changed = true
        }

        guard changed else { return rawJSON }
        let payload: Any = (root is [[String: Any]]) ? containers : ["containers": containers]
        let cleanData = try JSONSerialization.data(
            withJSONObject: payload,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        )
        let cleanText = String(data: cleanData, encoding: .utf8) ?? rawJSON
        let url = URL(fileURLWithPath: containerRegistryPath())
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try cleanText.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        UserDefaults.standard.set(cleanText, forKey: DefaultsKey.containerRegistry)
        return cleanText
    }

    // MARK: - 凭据是否已经生效

    /// 所有凭据的指纹（只存哈希，不落明文）
    var credentialsFingerprint: String {
        var parts = [
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
            // 容器元数据热加载；但容器 Keychain 凭据通过环境变量注入，需重启后端。
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
            backendAdminUsername,
            backendAdminPassword,
        ].map { $0.trimmingCharacters(in: .whitespaces) }

        let migratedSecretNames = defaults.stringArray(
            forKey: DefaultsKey.keychainEnvironmentSecretNames
        ) ?? []
        parts += Set(Self.keychainEnvironmentSecrets + migratedSecretNames)
            .sorted()
            .map { keychain.read(Self.keychainAccount(forEnvironmentName: $0)) ?? "" }
        parts += Self.containerNames(in: containerRegistryJSON)
            .map { keychain.read(KeychainStore.Keys.containerAPIKey($0)) ?? "" }

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
