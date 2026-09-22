import Foundation

/// 设置页里可以跳转 / 滚动到的区域
enum SettingsSection: String, Identifiable, Hashable, CaseIterable {
    case backend
    case azure
    case meraki
    case provider
    case login
    case log

    var id: String { rawValue }

    var title: String {
        switch self {
        case .backend: return "本地 Python 后端"
        case .azure: return "Azure 工具凭据"
        case .meraki: return "Meraki 工具凭据"
        case .provider: return "AI Provider"
        case .login: return "登录"
        case .log: return "后端日志"
        }
    }
}
