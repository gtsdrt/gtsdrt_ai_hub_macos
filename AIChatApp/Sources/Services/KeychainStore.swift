import Foundation
import Security

/// 极简 Keychain 封装：token 与 API Key 都存成 generic password，不落 UserDefaults
final class KeychainStore {
    static let shared = KeychainStore()

    enum Keys {
        static let token = "backend_token"
        static let loginPassword = "login_password"
        static let deepseekKey = "deepseek_api_key"
        static let kimiKey = "kimi_api_key"
        static let openaiKey = "openai_api_key"
        static let azureClientSecret = "azure_client_secret"
        static let merakiAPIKey = "meraki_api_key"
        static let ndAPIKey = "nd_api_key"
        static let ndPassword = "nd_password"
        static let adminPassword = "backend_admin_password"
        static let jwtSecret = "backend_jwt_secret"
        static let ansibleExecutorAPIKey = "ansible_executor_api_key"
        static let googleClientSecret = "google_client_secret"

        /// 兼容迁移未在设置界面中单独管理的 .env 凭据
        static func environmentSecret(_ name: String) -> String {
            "env_secret_" + name.lowercased()
        }

        /// 每个 Serverless 容器一把密钥：container_api_key_<name>（名字按容器名动态生成）
        static func containerAPIKey(_ containerName: String) -> String {
            "container_api_key_" + containerName.trimmingCharacters(in: .whitespaces).lowercased()
        }
    }

    enum KeychainError: LocalizedError {
        case unexpectedStatus(OSStatus)
        case invalidData

        var errorDescription: String? {
            switch self {
            case .unexpectedStatus(let status):
                if let message = SecCopyErrorMessageString(status, nil) as String? {
                    return L("Keychain 操作失败：{0}（{1}）", message, String(status))
                }
                return L("Keychain 操作失败，状态码 {0}", String(status))
            case .invalidData:
                return L("Keychain 中的数据无法解析")
            }
        }
    }

    private let service = "com.example.AIChatApp"

    private init() {}

    func save(_ value: String, for key: String) throws {
        let baseQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]

        // 空值代表清除；不要在 Keychain 中留下一个“成功读取但为空”的条目。
        if value.isEmpty {
            delete(key)
            return
        }

        let update: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        let updateStatus = SecItemUpdate(baseQuery as CFDictionary, update as CFDictionary)
        if updateStatus == errSecSuccess {
            return
        }
        guard updateStatus == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(updateStatus)
        }

        var attributes = baseQuery
        attributes[kSecValueData as String] = Data(value.utf8)
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(attributes as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainError.unexpectedStatus(status)
        }
    }

    func read(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8) else {
            return nil
        }
        return value
    }

    func delete(_ key: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
    }

    /// 保存失败也不希望打断主流程（例如钥匙串被锁定）
    func saveQuietly(_ value: String, for key: String) {
        try? save(value, for: key)
    }
}
