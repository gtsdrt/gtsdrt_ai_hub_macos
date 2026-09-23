import Foundation

/// 设置页里可以跳转 / 滚动到的区域
enum SettingsSection: String, Identifiable, Hashable, CaseIterable {
    case language
    case backend
    case azure
    case meraki
    case nexusDashboard
    case container
    case provider
    case login
    case log

    var id: String { rawValue }

    var title: String {
        switch self {
        case .language: return L("界面语言")
        case .backend: return L("本地 Python 后端")
        case .azure: return L("Azure 工具凭据")
        case .meraki: return L("Meraki 工具凭据")
        case .nexusDashboard: return L("Nexus Dashboard 工具凭据")
        case .container: return L("Serverless 容器")
        case .provider: return "AI Provider"
        case .login: return L("登录")
        case .log: return L("后端日志")
        }
    }
}
