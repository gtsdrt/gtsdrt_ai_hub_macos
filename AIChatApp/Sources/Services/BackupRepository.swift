import CoreFoundation
import Foundation

enum BackupPolicy {
    // Device paths, window state, updater state and credential fingerprints stay
    // on this Mac. Never import an arbitrary UserDefaults domain from a file.
    static let stringKeys = ["backendBaseURL", "defaultProvider", "username", "backendAdminUsername",
        "googleClientID", "googleAllowedEmails", "googleAllowedDomains", "githubClientID",
        "githubAllowedLogins", "githubAllowedEmails", "openaiBaseURL", "openaiModel", "openaiAuthMode",
        "azureTenantID", "azureClientID", "azureSubscriptionID", "ndBaseURL", "ndUsername",
        "ndLoginDomain", "ui.language", "containerRegistryJSON"]
    static let boolKeys = ["autoStartBackend", "ndVerifyTLS", "chat.enableTools", "chat.toolsNoticeAcknowledged"]
    static let arrayKey = "keychainEnvironmentSecretNames"
    static let allKeys = stringKeys + boolKeys + ["ui.fontScale", arrayKey]
    static let credentialKeys: Set<String> = ["backend_token", "login_password", "deepseek_api_key",
        "kimi_api_key", "openai_api_key", "azure_client_secret", "meraki_api_key", "nd_api_key",
        "nd_password", "backend_admin_password", "backend_jwt_secret", "ansible_executor_api_key",
        "google_client_secret"]

    /// 允许从备份恢复的环境变量名：只认后端固定注入的密钥，避免备份注入
    /// DYLD_INSERT_LIBRARIES / PATH 等危险变量（AppSettings.keychainEnvironmentSecrets 共用同一份）
    static let allowedEnvironmentSecretNames = [
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

    static func allowedCredential(_ name: String) -> Bool {
        guard name != "env_secret_sparkle_private_key" else { return false }
        return credentialKeys.contains(name) ||
        ((name.hasPrefix("container_api_key_") || name.hasPrefix("env_secret_")) &&
            name.count <= 256 && !name.contains("\0"))
    }

    static func preferences(_ data: Data) throws -> [String: Any] {
        guard data.count < 1024 * 1024,
              let values = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              Set(values.keys).isSubset(of: Set(allKeys)) else { throw BackupError.invalidFile }
        for (key, value) in values {
            if stringKeys.contains(key) {
                guard let text = value as? String, text.utf8.count < 65536 else { throw BackupError.invalidFile }
                if key == "containerRegistryJSON", !text.isEmpty { try validateRegistry(Data(text.utf8)) }
            } else if boolKeys.contains(key) {
                guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
                    throw BackupError.invalidFile
                }
            } else if key == "ui.fontScale" {
                guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      (0.6...2.5).contains(number.doubleValue) else { throw BackupError.invalidFile }
            } else if key == arrayKey {
                // 这里只做格式与体积校验：同一个校验器在「导出备份」时也会跑，
                // 若在此处限制名字白名单，会让存有历史自定义密钥名的机器无法导出备份。
                // 白名单只在恢复路径（validateImportedSettings）里强制。
                guard let names = value as? [String], names.count < 1000,
                      names.allSatisfy({ $0.range(of: "^[A-Z][A-Z0-9_]{0,127}$", options: .regularExpression) != nil }) else {
                    throw BackupError.invalidFile
                }
            }
        }
        return values
    }

    static func validate(_ payload: BackupPayload) throws -> BackupSummary {
        _ = try preferences(payload.preferences)
        if let credentials = payload.credentials {
            guard credentials.count < 1000,
                  credentials.allSatisfy({ allowedCredential($0.key) && $0.value.utf8.count < 65536 }) else {
                throw BackupError.invalidFile
            }
        }
        if let registry = payload.registry { try validateRegistry(registry) }
        return try payload.database.map(BackupDatabase.validate) ?? BackupSummary(conversations: 0, tasks: 0)
    }

    private static func validateRegistry(_ registry: Data) throws {
        guard registry.count < 1024 * 1024 else { throw BackupError.tooLarge }
        let json = try JSONSerialization.jsonObject(with: registry)
        let entries = (json as? [String: Any])?["containers"] as? [[String: Any]] ?? json as? [[String: Any]]
        guard let entries, entries.count < 1000,
              entries.allSatisfy({ $0["name"] is String && $0["url"] is String && $0["api_key"] == nil }) else {
            throw BackupError.invalidFile
        }
    }
}

protocol BackupCredentialStore {
    func exportBackupCredentials() throws -> [String: String]
    func applyBackupCredentials(_ entries: [String: String], removing: Set<String>) throws
}

/// All paths and dependencies come from the current Mac, never from the backup.
/// Used with isolated directories/UserDefaults/credential stores in regression tests.
struct BackupRepository {
    let databaseURL: URL
    let registryURL: URL
    let logURL: URL
    let recoveryDirectory: URL
    let defaults: UserDefaults
    let credentialStore: BackupCredentialStore

    func preferenceData() throws -> Data {
        let values = Dictionary(uniqueKeysWithValues: BackupPolicy.allKeys.compactMap { key in
            defaults.object(forKey: key).map { (key, $0) }
        })
        return try PropertyListSerialization.data(fromPropertyList: values, format: .binary, options: 0)
    }

    func capture(includeCredentials: Bool, includeLogs: Bool, appVersion: String) throws -> BackupPayload {
        let registry: Data?
        if FileManager.default.fileExists(atPath: registryURL.path) {
            let data = try BackupArchive.read(registryURL)
            registry = data.isEmpty ? nil : data
        } else { registry = nil }
        let credentials = includeCredentials ? try credentialStore.exportBackupCredentials() : nil
        let log = includeLogs && FileManager.default.fileExists(atPath: logURL.path) ? try BackupArchive.read(logURL) : nil
        let payload = BackupPayload(formatVersion: 1, createdAt: Date(), appVersion: appVersion,
            database: try BackupDatabase.snapshot(databaseURL), preferences: try preferenceData(),
            registry: registry, credentials: credentials, log: log)
        _ = try BackupPolicy.validate(payload)
        return payload
    }

    struct Selection {
        var database = true
        var settings = true
        var credentials = false
        var logs = false
    }

    /// Caller has already stopped the backend. Save an encrypted recovery file
    /// before changing anything. Roll back each selected component on any error.
    func restore(_ payload: BackupPayload, selection: Selection, password: String,
                 appVersion: String) throws -> URL {
        _ = try BackupPolicy.validate(payload)
        let restoreCredentials = selection.credentials && payload.credentials != nil
        let current = try capture(includeCredentials: restoreCredentials, includeLogs: selection.logs,
                                  appVersion: appVersion)
        let recoveryURL = recoveryDirectory.appendingPathComponent("BeforeRestore-\(UUID().uuidString).aichatbackup")
        try BackupArchive.writePrivate(BackupArchive.seal(current, password: password), to: recoveryURL)
        do {
            if selection.database, let database = payload.database { try BackupDatabase.install(database, at: databaseURL) }
            if selection.settings { try installSettings(payload) }
            if selection.logs, let log = payload.log { try BackupArchive.writePrivate(log, to: logURL) }
            if restoreCredentials {
                // Merge named credentials; unrelated entries stay on this Mac.
                // Login tokens are deliberately invalidated after successful restore.
                try credentialStore.applyBackupCredentials(payload.credentials ?? [:], removing: [])
            }
        } catch {
            var rollbackFailed = false
            func attempt(_ operation: () throws -> Void) {
                do { try operation() } catch { rollbackFailed = true }
            }
            if selection.database, payload.database != nil {
                attempt { try BackupDatabase.install(current.database, at: databaseURL) }
            }
            if selection.settings { attempt { try installSettings(current) } }
            if selection.logs, payload.log != nil {
                attempt {
                    if let log = current.log { try BackupArchive.writePrivate(log, to: logURL) }
                    else if FileManager.default.fileExists(atPath: logURL.path) { try FileManager.default.removeItem(at: logURL) }
                }
            }
            if restoreCredentials {
                let original = current.credentials ?? [:]
                let added = Set((payload.credentials ?? [:]).keys).subtracting(original.keys)
                attempt { try credentialStore.applyBackupCredentials(original, removing: added) }
            }
            if rollbackFailed { throw BackupError.rollback(recoveryURL.path) }
            throw error
        }
        return recoveryURL
    }

    /// 本机回环主机名白名单（与 BackupController.localOnly 的判定保持一致）
    private static let loopbackHosts: Set<String> = ["127.0.0.1", "localhost", "::1", "[::1]"]

    private static func isLoopback(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return loopbackHosts.contains(host)
    }

    /// 校验备份里要恢复的设置。只在恢复路径调用（导出侧共用 BackupPolicy.preferences，
    /// 那里不能加白名单，否则存有历史自定义密钥名的机器无法导出备份）。
    ///
    /// 两件事：
    ///   1. 端点必须指向本机或使用 HTTPS，防止备份把凭据引向攻击者主机
    ///   2. 环境变量名必须是应用内置的那几个，防止备份注入 DYLD_INSERT_LIBRARIES / PATH 等
    private func validateImportedSettings(_ values: [String: Any]) throws {
        func endpoint(_ key: String) throws -> URL? {
            guard let raw = values[key] as? String else { return nil }
            let value = raw.trimmingCharacters(in: .whitespaces)
            guard !value.isEmpty else { return nil }
            guard let url = URL(string: value),
                  let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
                  let host = url.host, !host.isEmpty else {
                throw BackupError.invalidFile
            }
            if scheme != "https", !Self.isLoopback(url) {
                throw BackupError.invalidFile
            }
            return url
        }

        // 本机后端地址只允许回环，避免管理员口令与 JWT 被发往别处
        if let backend = try endpoint("backendBaseURL"), !Self.isLoopback(backend) {
            throw BackupError.localOnly
        }
        _ = try endpoint("openaiBaseURL")
        _ = try endpoint("ndBaseURL")

        // 注册表注入的环境变量名只认固定名单
        if let names = values[BackupPolicy.arrayKey] as? [String],
           !names.allSatisfy({ BackupPolicy.allowedEnvironmentSecretNames.contains($0) }) {
            throw BackupError.invalidFile
        }
    }

    private func installSettings(_ payload: BackupPayload) throws {
        let values = try BackupPolicy.preferences(payload.preferences)
        try validateImportedSettings(values)
        if let registry = payload.registry { try BackupArchive.writePrivate(registry, to: registryURL) }
        else if FileManager.default.fileExists(atPath: registryURL.path) { try FileManager.default.removeItem(at: registryURL) }
        for key in BackupPolicy.allKeys {
            if let value = values[key] { defaults.set(value, forKey: key) }
            else { defaults.removeObject(forKey: key) }
        }
        if let registry = payload.registry, let text = String(data: registry, encoding: .utf8) {
            defaults.set(text, forKey: "containerRegistryJSON")
        }
        defaults.synchronize()
    }
}
